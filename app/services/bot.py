"""Бот «Жемчужина» (макет блок 7, решение владельца 15.09).

Группа получает только сигналы, без сумм по людям. Личка: держателю
кармана его карман по пятницам («верно? ответьте «да» или своей цифрой»),
учредителям сводка по понедельникам — сначала Абдусаттару на проверку,
дальше по его «ок». Фото в личку от сотрудника — чек или лист кухни в
«Ждёт вас» на ноутбуке.

Отправка — через Bot API напрямую (httpx), без библиотек. Без токена
(`TELEGRAM_TOKEN`) всё считается и пишется в журнал, но никуда не уходит —
так работают тесты и локальная копия.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import BotMessage, Organization, Receipt, User
from app.services import cash, children, today
from app.services import rules
from app.services.kitchen import missing_days
from app.services.ocr import compute_hash
from app.services.price_check import fmt_money
from app.services.purchases import OPERATIONAL_ROLES, audit, site_orgs

TOKEN_ENV = "TELEGRAM_TOKEN"
GROUP_ENV = "TELEGRAM_GROUP_CHAT_ID"
REVIEW_ENV = "BOT_REVIEW_SUMMARY"      # «1» — сводка учредителям через проверку владельца (по умолчанию да)
# «1» — бот говорит в группе. По умолчанию молчит (владелец 21.09): группу он
# слушает, а разбор каждого сообщения шлёт владельцу в личку, сигналы в группу
# только в журнал — пока на живых сообщениях не станет видно, что понимает верно.
GROUP_TALK_ENV = "BOT_GROUP_TALK"
OWNER_USER_ID = 1                      # Абдусаттар: проверяет сводку

MEDIA_ROOT = Path(__file__).parent.parent.parent / "media"

WEEKDAY_ACC = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]


def _d(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def token() -> str | None:
    return os.getenv(TOKEN_ENV) or None


def group_chat_id() -> int | None:
    v = os.getenv(GROUP_ENV)
    return int(v) if v and v.lstrip("-").isdigit() else None


def group_talks() -> bool:
    return os.getenv(GROUP_TALK_ENV, "0") == "1"


def group_out() -> int | None:
    """Куда слать в группу: молчащий бот — никуда, только в журнал."""
    return group_chat_id() if group_talks() else None


def webhook_secret() -> str:
    t = token() or "no-token"
    return hashlib.sha256(f"pearl-bot:{t}".encode()).hexdigest()[:24]


# ── отправка ─────────────────────────────────────────────────────────────

def send(db: Session, chat_id: int | None, text: str, kind: str, *, user_id: int | None = None,
         job_key: str | None = None, status: str = "sent", payload: dict | None = None,
         reply_to: int | None = None) -> BotMessage:
    """Шлёт в Telegram и пишет в журнал. Без токена или chat_id — только журнал."""
    msg = BotMessage(kind=kind, job_key=job_key, chat_id=chat_id, user_id=user_id, direction="out",
                     text=text, status=status, payload=payload)
    db.add(msg)
    db.flush()
    if status != "sent":
        return msg
    if not token() or chat_id is None:
        msg.status = "logged"
        return msg
    try:
        body = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_to:
            # ответ под сообщением; если его успели удалить — всё равно отправить
            body["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        r = httpx.post(f"https://api.telegram.org/bot{token()}/sendMessage", json=body, timeout=20)
        if r.status_code != 200:
            msg.status = "failed"
            msg.payload = {**(payload or {}), "error": r.text[:300]}
    except httpx.HTTPError as e:
        msg.status = "failed"
        msg.payload = {**(payload or {}), "error": str(e)[:300]}
    return msg


# ── тексты ───────────────────────────────────────────────────────────────

def group_signals_text(db: Session, site_org_id: int) -> str | None:
    """Сигналы в группу: без сумм по людям. None — сигналов нет."""
    lines = []
    miss = missing_days(db, site_org_id)
    if len(miss) >= 3:
        lines.append(f"Лист кухни не вносился {len(miss)} рабочих дн. — последний пропуск {_d(miss[-1])}.")
    for s in today.supplier_debts(db, site_org_id):
        if s["since"] and (date.today() - s["since"]).days >= rules.debt_old_days(db):
            lines.append(f"Долг {s['name']} {fmt_money(float(s['debt']))} старше месяца, с {_d(s['since'])}.")
    for it in today.todo(db, site_org_id):
        if it["title"].startswith("Пересчёт склада") or "чек" in it["title"]:
            lines.append(it["title"] + ".")
    # расходы из кассы за неделю при нулевых снятиях — деньги откуда-то взялись
    week_ago = date.today() - timedelta(days=7)
    spent = today.week_cash_spent(db, site_org_id, week_ago) if hasattr(today, "week_cash_spent") else None
    if spent and spent["spent"] > 0 and spent["withdrawn"] == 0:
        lines.append(f"За неделю расходов из кассы на {fmt_money(float(spent['spent']))}, а снятий со счёта не записано. "
                     "Кто снимал? Ответьте суммой и датой мне в личку.")
    for p in cash.pockets(db, site_org_id)["rows"]:
        if p["balance"] < -1:
            lines.append(f"Карман {p['user'].name} в минусе: записей о деньгах меньше, чем расходов.")
    return "\n".join(lines) if lines else None


def pocket_text(db: Session, site_org_id: int, user: User) -> tuple[str, Decimal]:
    bal = cash.pocket_balance(db, site_org_id, user.id)
    recent = [r for r in cash.recent(db, site_org_id, limit=20)
              if user.name in (r["title"] or "") or user.name in (r.get("sub") or "")][:2]
    tail = ""
    if recent:
        tail = ": " + ", ".join(f"{r['title'].lower()} {fmt_money(float(abs(r['amount'])))} {_d(r['date'])}" for r in recent if r["amount"] is not None)
    return (f"У вас на руках по записям {fmt_money(float(bal))}{tail}. Верно? "
            f"Ответьте «да» или своей цифрой (можно с причиной: «60000 отдала за хлеб»)."), bal


def founders_summary_text(db: Session, site_org_id: int) -> str:
    pk = cash.pockets(db, site_org_id)
    accounts = cash.accounts(db, site_org_id)
    debts = today.supplier_debts(db, site_org_id)
    parents = Decimal("0")
    for o in site_orgs(db, site_org_id):
        d = children.children_list(db, o)
        if d["tariff"]:
            parents += Decimal(str(d["debt_total"]))
    pockets = ", ".join(f"{p['user'].name} {fmt_money(float(p['balance']))}" for p in pk["rows"])
    return (f"Сводка за неделю. Наличных {fmt_money(float(pk['total']))}: {pockets}. "
            f"На счетах {fmt_money(float(sum((a['expected'] for a in accounts), Decimal('0'))))}. "
            f"Должны поставщикам {fmt_money(float(sum((d['debt'] for d in debts), Decimal('0'))))}. "
            f"Родители должны {fmt_money(float(parents))}. Подробнее в Обзоре.")


# ── расписание ───────────────────────────────────────────────────────────

def _done(db: Session, job_key: str) -> bool:
    return db.query(BotMessage.id).filter(BotMessage.job_key == job_key).first() is not None


def site_for_bot(db: Session) -> Organization | None:
    """Площадка, о которой говорит бот: пока одна — Сокулук."""
    org = (db.query(Organization).filter(Organization.site_id.isnot(None)).first())
    return db.get(Organization, org.site_id) if org else db.query(Organization).filter(Organization.type == "kindergarten").first()


def run_scheduled(db: Session, now: datetime | None = None) -> list[str]:
    """Что пора отправить прямо сейчас. Идемпотентно по job_key."""
    now = now or datetime.now()
    site = site_for_bot(db)
    if site is None:
        return []
    sent = []
    d = now.date()
    # понедельник 9:00 — сигналы в группу и сводка учредителям (через проверку)
    if now.weekday() == 0 and now.hour >= 9:
        key = f"group_signals:{d.isoformat()}"
        if not _done(db, key):
            text = group_signals_text(db, site.id)
            send(db, group_out(), text or "Неделя началась. Сигналов нет: листы внесены, долги свежие.",
                 "group_signals", job_key=key)
            sent.append(key)
        key = f"founders:{d.isoformat()}"
        if not _done(db, key):
            text = founders_summary_text(db, site.id)
            owner = db.get(User, OWNER_USER_ID)
            if os.getenv(REVIEW_ENV, "1") == "1" and owner and owner.tg_id:
                send(db, owner.tg_id, text + "\n\nОтправить Айдай и Таласу? Ответьте «ок» или «не так».",
                     "founders_review", user_id=owner.id, job_key=key, payload={"summary": text})
                # ждём «ок»: сама сводка лежит в payload
                db.query(BotMessage).filter(BotMessage.job_key == key).update({"status": "pending"})
            else:
                _send_founders(db, text, key)
            sent.append(key)
    sent += _meal_and_count_asks(db, site, now)
    sent += _escalate(db, site, now)
    # Пятничный «у вас на руках X, верно?» с записью ответа выключен 21.09: это ритуал
    # пересчёта (владелец: пересчёт — признак слабости), и бот не пишет в деньги мимо
    # проверки Махабат. Сумму на руках человек присылает сам — она станет черновиком.
    # каждый день 9:00 — пороги в группу (лист не вносился 3 дня, пересчёт висит)
    if now.hour >= 9 and now.weekday() != 0:
        key = f"group_threshold:{d.isoformat()}"
        if not _done(db, key):
            text = group_signals_text(db, site.id)
            if text:
                send(db, group_out(), text, "group_threshold", job_key=key)
            else:
                db.add(BotMessage(kind="group_threshold", job_key=key, status="skipped"))
            sent.append(key)
    return sent


def _counter_name(db: Session, site: Organization) -> str | None:
    """Кому адресовать вопросы про едоков и пересчёт: учётчик площадки (роль staff)."""
    from app.services.purchases import site_orgs as _orgs
    org_ids = [o.id for o in _orgs(db, site.id)]
    u = (db.query(User).filter(User.role == "staff", User.deleted_at.is_(None), User.organization_id.in_(org_ids))
         .order_by(User.id).first())
    return u.name if u else None


def _meal_and_count_asks(db: Session, site: Organization, now: datetime) -> list[str]:
    """Вопросы в группу с образцом ответа (23.09, владелец: «пример, чтобы не парились»).
    Спрашиваем в своё окно и один раз: едоки в 12, напоминание в 15, если не записано;
    пересчёт ключевых в день пересчёта в 14 и один раз в 18. Вопрос — не распознавание,
    поэтому идёт в группу и при молчащем боте. Окно в час, а не «после 12»: перезапуск
    сервера вечером не должен задавать утренний вопрос."""
    from app.services import meals, stock_count as sc
    d, out = now.date(), []
    name = _counter_name(db, site)
    hi = f"{name}, " if name else ""
    group = group_chat_id()
    if meals.missing_today(db, site.id) and now.hour in (12, 15):
        key = f"meal_ask:{d.isoformat()}:{now.hour}"
        if not _done(db, key):
            first = now.hour == 12
            text = ((f"{hi}сколько сегодня едят? " if first else f"{hi}сколько сегодня ели, ещё не записано. ")
                    + f"Одной строкой, например:\n{meals.EXAMPLE}")
            send(db, group, text, "meal_ask", job_key=key)
            out.append(key)
    # Пересчёт (владелец 23.09): в день пересчёта после кухни — 15:00, напоминание в 18:00,
    # запасной — утром следующего дня до поваров. Вместе с ним — остаток в банке по
    # счетам, где его давно не вносили: расход со счёта без чека всплывает за неделю.
    keys = rules.key_products(db)
    cw = rules.count_weekday(db)
    slot = {(cw, 15): "first", (cw, 18): "again", ((cw + 1) % 7, 8): "last"}.get((d.weekday(), now.hour))
    if keys and slot:
        start = count_week_start(db, d)
        bank = cash.bank_due(db, site.id, start)
        need_count = not key_count_done(db, site.id, d)
        key = f"count_ask:{d.isoformat()}:{now.hour}"
        if (need_count or (bank and slot == "first")) and not _done(db, key):
            lines = []
            if need_count and slot == "first":
                lines.append(f"{hi}сегодня пересчёт ключевых продуктов: {len(keys)} позиций, минут 15. "
                             "Лучше сейчас, пока повара не взяли продукты на завтра.\n"
                             "Склад → Пересчитать → «Ключевые». Пустая строка = не считали.")
            elif need_count and slot == "again":
                lines.append(f"{hi}пересчёт ключевых сегодня не записан. Если считали на бумаге — пришлите фото листа сюда.")
            elif need_count:
                lines.append(f"{hi}пересчёт за {WEEKDAY_ACC[cw]} не записан. Сегодня утром, до поваров, — "
                             "потом неделя смажется. Склад → Пересчитать → «Ключевые».")
            if bank and slot == "first":
                who = _bank_holder(db, site)
                lines.append((f"{who}, " if who else "") + "и остаток в банке одной цифрой: "
                             + ", ".join(a["org"].name for a in bank) + ". Касса → Остаток в банке.")
            send(db, group, "\n\n".join(lines), "count_ask", job_key=key)
            out.append(key)
    return out


def count_week_start(db: Session, d: date) -> date:
    """Последний день пересчёта не позже d — начало «недели пересчёта»."""
    return d - timedelta(days=(d.weekday() - rules.count_weekday(db)) % 7)


def _bank_holder(db: Session, site: Organization) -> str | None:
    """Кто вносит остаток в банке: управляющая площадки (Мунара)."""
    from app.services.purchases import site_orgs as _orgs
    org_ids = [o.id for o in _orgs(db, site.id)]
    u = (db.query(User).filter(User.role == "manager", User.deleted_at.is_(None), User.organization_id.in_(org_ids))
         .order_by(User.id).first())
    return u.name if u else None


# ── застряло → учредителю (23.09) ───────────────────────────────────────
# Бот сам спрашивает исполнителя. Если спросил и ответа нет — одно сообщение в день
# учредителю в личку (дисциплина — её слово, владелец 23.09) и копия владельцу.
# Только то, о чём бот уже спрашивал: без вопроса нет и жалобы.

ESCALATE_HOURS = (12, 16)       # 12 — после пропущенного пересчёта, 16 — после напоминания про едоков
RECEIPTS_STUCK_DAYS = 3


def stuck_items(db: Session, site: Organization, now: datetime) -> list[str]:
    from app.services import meals
    d, out = now.date(), []
    # едоки: бот спрашивал сегодня и в прошлый рабочий день, записи нет ни за один
    prev = d - timedelta(days=1)
    while not meals.expected_today(db, site.id, prev) and prev > d - timedelta(days=7):
        prev -= timedelta(days=1)
    if (now.hour == 16 and all(_done(db, f"meal_ask:{x.isoformat()}:12") and meals.get(db, site.id, x) is None
                               for x in (prev, d))):
        out.append(f"сколько едят — не записано 2 рабочих дня ({_d(prev)} и сегодня)")
    # пересчёт: утренний запасной вопрос был, а пересчёта так и нет
    if now.hour != 12:
        return out   # остальное — один раз в день, в 12
    if rules.key_products(db) and _done(db, f"count_ask:{d.isoformat()}:8") and not key_count_done(db, site.id, d):
        out.append(f"пересчёт склада за {WEEKDAY_ACC[rules.count_weekday(db)]} не сделан — неделя без точки")
    # остаток в банке: спрашивали в день пересчёта, до сих пор не внесён
    start = count_week_start(db, d)
    if d != start and _done(db, f"count_ask:{start.isoformat()}:15"):
        for a in cash.bank_due(db, site.id, start):
            out.append(f"остаток в банке по счёту {a['org'].name} на этой неделе не внесён")
    # чеки из чата ждут проверки дольше 3 дней
    edge = datetime.combine(d - timedelta(days=RECEIPTS_STUCK_DAYS), datetime.min.time())
    old = [r for r in today.unchecked_receipts(db, site.id) if r.created_at and r.created_at < edge]
    if old:
        out.append(f"чеки из чата: {len(old)} ждут проверки больше {RECEIPTS_STUCK_DAYS} дней")
    return out


def _escalate(db: Session, site: Organization, now: datetime) -> list[str]:
    if now.hour not in ESCALATE_HOURS:
        return []
    key = f"escalate:{now.date().isoformat()}:{now.hour}"
    if _done(db, key):
        return []
    items = stuck_items(db, site, now)
    if not items:
        return []
    body = "Застряло:\n" + "\n".join(f"• {x}" for x in items) + "\n\nБот уже спрашивал в чате. Нужно ваше слово."
    for f in db.query(User).filter(User.role == "founder", User.deleted_at.is_(None), User.tg_id.isnot(None)).all():
        send(db, f.tg_id, f"{f.name}, это бот Жемчужины. {body}", "escalate", user_id=f.id, job_key=f"{key}:{f.id}")
    owner = db.get(User, OWNER_USER_ID)
    send(db, owner.tg_id if owner else None, "Копия учредителям. " + body, "escalate", user_id=OWNER_USER_ID, job_key=key)
    return [key]


def key_count_done(db: Session, site_org_id: int, d: date) -> bool:
    """Ключевые пересчитаны с начала этой «недели пересчёта» (с последнего дня пересчёта)."""
    from app.models import StockCount, StockCountLine
    keys = rules.key_products(db)
    if not keys:
        return True
    since = count_week_start(db, d)
    got = {pid for (pid,) in db.query(StockCountLine.product_id).join(StockCount, StockCount.id == StockCountLine.count_id)
           .filter(StockCount.organization_id == site_org_id, StockCount.status == "applied",
                   StockCount.count_date >= since, StockCountLine.product_id.in_(keys)).distinct().all()}
    return len(got) >= max(1, len(keys) // 2)   # половина ключевых — считаем, что пересчёт был


def meal_reply(db: Session, site: Organization, user: User | None, text: str) -> str | None:
    """Строка про едоков → запись. None — это не про едоков."""
    from app.services import meals
    p = meals.parse(text)
    if p is None:
        return None
    if user is None or user.role not in ("owner", *OPERATIONAL_ROLES):
        return None
    row = meals.record(db, site_org_id=site.id, d=p["date"], values=p, menu=p.get("menu"), user=user, source="chat")
    when = "сегодня" if p["date"] == date.today() else f"на {_d(p['date'])}"
    reply = f"Записал {when}: {meals.text(row)}." + (f" Меню: {row.menu}." if p.get("menu") else "")
    doubt = meals.doubts(db, site.id, p, p["date"])
    if doubt:
        reply += " Проверьте: " + "; ".join(doubt) + ". Если верно — ничего не делайте; ошиблись — пришлите строку заново."
    return reply


def _send_founders(db: Session, text: str, key: str | None = None) -> None:
    for f in db.query(User).filter(User.role == "founder", User.deleted_at.is_(None)).all():
        if f.tg_id:
            send(db, f.tg_id, text, "founders_summary", user_id=f.id,
                 job_key=f"{key}:{f.id}" if key else None)


# ── входящие ─────────────────────────────────────────────────────────────

_NUM = re.compile(r"^\s*(\d[\d\s]*(?:[.,]\d+)?)\s*(.*)$", re.S)


def handle_update(db: Session, update: dict) -> str | None:
    """Обрабатывает одно обновление Telegram, возвращает текст ответа (уже отправлен)."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return None
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    from_id = (msg.get("from") or {}).get("id")
    is_private = chat.get("type") == "private"
    text = (msg.get("text") or msg.get("caption") or "").strip()
    user = db.query(User).filter(User.tg_id == from_id, User.deleted_at.is_(None)).first() if from_id else None
    sender = msg.get("from") or {}
    media = _media(msg)
    # Кто написал — в журнал всегда (23.09): непривязанного человека владелец потом
    # сопоставляет одной кнопкой в Настройках бота, не спрашивая его номер.
    db.add(BotMessage(kind="inbound", chat_id=chat_id, user_id=user.id if user else None, direction="in",
                      text=text[:2000], status="received",
                      payload={"has_photo": bool(msg.get("photo")), "media": media["kind"] if media else None,
                               "from_id": from_id, "from_name": _sender_name(sender)}))
    db.flush()
    if not is_private:
        group = group_chat_id()
        if group is None or chat_id != group:
            return None  # чужие группы бот не слушает
        return _handle_group(db, msg, user, text)
    if media and media["kind"] == "voice" and user:
        site = site_for_bot(db)
        transcript = _voice_text(db, msg, media, user, chat_id)
        if transcript is None:
            reply = "Голосовое не смог разобрать. Напишите текстом, пожалуйста."
            send(db, chat_id, reply, "reply", user_id=user.id)
            return reply
        text, msg = transcript, {**msg, "text": transcript, "_voice": True}   # дальше как обычный текст
    if media and media["kind"] == "document" and user:
        msg = {**msg, "_doc": media}
    if not user:
        # 23.09: номер человеку ни к чему — владелец привязывает кнопкой «Это он(а)»
        reply = "Здравствуйте. Я бот Жемчужины. Абдусаттар подключит вас, передавать ничего не нужно."
        send(db, chat_id, reply, "reply")
        owner = db.get(User, OWNER_USER_ID)
        if owner and owner.tg_id and from_id != owner.tg_id:
            send(db, owner.tg_id, f"Боту написал(а) {_sender_name(sender) or from_id}. Привязать: Настройки бота → "
                 "«Писали, но не привязаны» → «Это он(а)».", "reply", user_id=OWNER_USER_ID)
        return reply
    site = site_for_bot(db)
    if (msg.get("photo") or msg.get("_doc")) and site is not None:
        reply = _handle_photo(db, msg, user, site, text)
        send(db, chat_id, reply, "reply", user_id=user.id)
        return reply
    if site is not None and (meal := meal_reply(db, site, user, text)):
        send(db, chat_id, meal, "reply", user_id=user.id)
        return meal
    low = text.lower()
    if low in ("/start", "start") and user.role == "founder":
        reply = (f"Здравствуйте, {user.name}. Я буду писать вам, только когда что-то застряло: "
                 "бот спросил в чате, а ответа нет. Остальное люди и бот решают сами.")
    elif low in ("/start", "start"):
        reply = f"Здравствуйте, {user.name}. Фото чека или листа кухни можно отправить сюда или в чат «Жемчужина»: я положу его Махабат черновиком на проверку."
    elif low in ("ок", "ok", "да, отправляй") and user.id == OWNER_USER_ID:
        reply = _approve_summary(db)
    elif low in ("не так", "нет") and user.id == OWNER_USER_ID and _pending_summary(db):
        p = _pending_summary(db)
        p.status = "rejected"
        reply = "Сводка не ушла. Поправьте в приложении и напишите «сводка», пришлю заново."
    elif low in ("сводка", "касса") and user.role in ("founder", "owner") and site is not None:
        reply = founders_summary_text(db, site.id)
        if user.id == OWNER_USER_ID and low == "сводка":
            m = send(db, chat_id, reply + "\n\nОтправить Айдай и Таласу? Ответьте «ок» или «не так».",
                     "founders_review", user_id=user.id, payload={"summary": reply}, status="pending")
            return m.text
    elif user.role in OPERATIONAL_ROLES and site is not None and _NUM.match(text):
        reply = ("Сумму на руках запишет Махабат в Кассе («Пересчитать наличные»). "
                 "Скоро такие сообщения будут сами становиться черновиком ей на проверку.")
    elif msg.get("_voice"):
        reply = f"Записал: «{text[:600]}»"
    else:
        reply = "Понял. Сигналы и вопросы приходят сюда сами; фото чека или листа кухни можно прислать в любой момент."
    send(db, chat_id, reply, "reply", user_id=user.id)
    return reply


def _handle_group(db: Session, msg: dict, user: User | None, text: str) -> str | None:
    """Группа «Жемчужина», день 1 (21.09): понять и ответить под сообщением,
    ничего не записывая. Всё, что бот понял, — в журнале (payload), по нему
    владелец смотрит, как бот распознаёт, прежде чем разрешить запись."""
    from app.services import bot_group as grp
    site = site_for_bot(db)
    if site is None:
        return None
    chat_id, message_id = msg["chat"]["id"], msg.get("message_id")
    today_d = date.today()
    reply, payload, kind = None, {"message_id": message_id}, None
    media = _media(msg)
    voice_note = None
    try:
        if media and media["kind"] == "voice":
            transcript = _voice_text(db, msg, media, user, chat_id)
            if transcript is None:
                return None
            text, voice_note = transcript, f"голосовое {media.get('duration') or '?'} с"
        if not (media and media["kind"] in ("photo", "document")):
            # Едоки за день (23.09): разбор без модели, запись сразу. Отвечаем в группе
            # и при молчащем боте — человек должен видеть «Записал», иначе пришлёт ещё раз.
            meal = meal_reply(db, site, user, text)
            if meal:
                db.add(BotMessage(kind="meal_count", chat_id=chat_id, user_id=user.id if user else None, direction="in",
                                  text=text[:2000], status="understood", payload={"message_id": message_id, "reply": meal}))
                send(db, chat_id, meal, "group_reply", user_id=user.id if user else None, reply_to=message_id)
                return meal
        if voice_note:
            # Голосовое (23.09): расшифровка в журнал всегда, дальше как текст о деньгах
            if grp.worth_reading(text):
                kind = "group_text"
                info = grp.read_text(db, text, user, today_d)
                payload.update({k: (v.isoformat() if isinstance(v, date) else v) for k, v in info.items()})
                reply = grp.text_reply(db, site, user, info, today_d)
        elif media and media["kind"] in ("photo", "document"):
            kind = "group_photo" if media["kind"] == "photo" else "group_document"
            payload["file_unique_id"] = media.get("file_unique_id")
            data = download_file(media["file_id"])
            if data is None:
                return None
            reply, info, draft = intake_photo(db, site, user, data, media.get("file_unique_id"), text, "chat",
                                              mime=media.get("mime"), file_name=media.get("file_name"))
            payload.update({k: (v.isoformat() if isinstance(v, date) else v) for k, v in info.items()})
            if draft is not None:
                payload["draft_id"] = draft.id
        elif grp.worth_reading(text):
            kind = "group_text"
            info = grp.read_text(db, text, user, today_d)
            payload.update({k: (v.isoformat() if isinstance(v, date) else v) for k, v in info.items()})
            reply = grp.text_reply(db, site, user, info, today_d)
    except Exception as e:  # noqa: BLE001 — модель или сеть упали: в группе молчим, в журнал
        db.add(BotMessage(kind="group_error", chat_id=chat_id, user_id=user.id if user else None, direction="in",
                          status="failed", text=str(e)[:500], payload=payload))
        return None
    who = user.name if user else (_sender_name(msg.get("from") or {}) or "не привязан")
    if voice_note and not reply:
        # голосовое не о деньгах: владельцу расшифровку всё равно (23.09 — «собираем информацию»)
        owner = db.get(User, OWNER_USER_ID)
        send(db, owner.tg_id if owner else None, f"Группа, {who}, {voice_note}:\n{text[:1500]}", "group_voice_owner",
             user_id=OWNER_USER_ID)
        return None
    if kind is None:
        return None
    payload["reply"] = reply
    db.add(BotMessage(kind=kind, chat_id=chat_id, user_id=user.id if user else None, direction="in",
                      text=text[:2000], status="understood" if reply else "silent", payload=payload))
    if reply:
        if group_talks():
            send(db, chat_id, reply, "group_reply", user_id=user.id if user else None, reply_to=message_id)
        else:
            # бот молчит в группе: владелец видит, что бот ответил бы, у себя в личке
            owner = db.get(User, OWNER_USER_ID)
            if kind == "group_photo":
                what = "фото"
            elif kind == "group_document":
                what = f"файл {(media or {}).get('file_name') or ''}".strip()
            else:
                what = (f"{voice_note}: " if voice_note else "") + f"«{text[:80]}»"
            send(db, owner.tg_id if owner else None, f"Группа, {who}, {what}:\n{reply}", "group_reply_owner",
                 user_id=OWNER_USER_ID)
    return reply


def unknown_senders(db: Session) -> list[dict]:
    """Кто писал боту или в группу, но в системе не привязан: для кнопки «это Мунара»."""
    rows = (db.query(BotMessage.payload["from_id"].as_string(), BotMessage.payload["from_name"].as_string(),
                     func.max(BotMessage.created_at), func.count(BotMessage.id))
            .filter(BotMessage.direction == "in", BotMessage.user_id.is_(None), BotMessage.kind == "inbound",
                    BotMessage.payload["from_id"].as_string().isnot(None))
            .group_by(BotMessage.payload["from_id"].as_string(), BotMessage.payload["from_name"].as_string())
            .order_by(func.max(BotMessage.created_at).desc()).all())
    linked = {str(u.tg_id) for u in db.query(User).filter(User.tg_id.isnot(None)).all()}
    return [{"from_id": r[0], "name": r[1] or "без имени", "last": r[2], "n": r[3]} for r in rows if r[0] not in linked]


# ── медиа: фото, файлы, голосовые ────────────────────────────────────────

IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp")


def _sender_name(sender: dict) -> str | None:
    name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x).strip()
    return name or sender.get("username") or None


def _media(msg: dict) -> dict | None:
    """Что прислали: фото (самое крупное), файл-картинка или PDF, голосовое/аудио."""
    if msg.get("photo"):
        p = sorted(msg["photo"], key=lambda p: p.get("file_size") or 0)[-1]
        return {"kind": "photo", "file_id": p["file_id"], "file_unique_id": p.get("file_unique_id"), "mime": "image/jpeg"}
    d = msg.get("document")
    if d and ((d.get("mime_type") or "") in IMAGE_MIMES or (d.get("mime_type") or "") == "application/pdf"):
        return {"kind": "document", "file_id": d["file_id"], "file_unique_id": d.get("file_unique_id"),
                "mime": d.get("mime_type"), "file_name": d.get("file_name")}
    v = msg.get("voice") or msg.get("audio")
    if v:
        return {"kind": "voice", "file_id": v["file_id"], "file_unique_id": v.get("file_unique_id"),
                "mime": v.get("mime_type") or "audio/ogg", "duration": v.get("duration")}
    return None


def _voice_text(db: Session, msg: dict, media: dict, user: User | None, chat_id: int | None) -> str | None:
    """Скачать голосовое, сохранить в media/bot/voice, расшифровать; в журнал — расшифровку.
    Файл хранится: спорную запись потом можно переслушать."""
    from app.services import bot_group as grp
    data = download_file(media["file_id"])
    if data is None:
        return None
    month = datetime.now().strftime("%Y-%m")
    folder = MEDIA_ROOT / "bot" / "voice" / month
    folder.mkdir(parents=True, exist_ok=True)
    fname = f"{media.get('file_unique_id') or compute_hash(data)[:12]}.ogg"
    (folder / fname).write_bytes(data)
    try:
        transcript = grp.transcribe(data, "ogg" if "ogg" in (media.get("mime") or "ogg") else "mp3")
    except Exception as e:  # noqa: BLE001 — модель недоступна: файл сохранён, расшифруем позже
        db.add(BotMessage(kind="voice", chat_id=chat_id, user_id=user.id if user else None, direction="in",
                          status="failed", text=str(e)[:300], payload={"file": f"bot/voice/{month}/{fname}",
                                                                       "message_id": msg.get("message_id")}))
        return None
    db.add(BotMessage(kind="voice", chat_id=chat_id, user_id=user.id if user else None, direction="in",
                      status="transcribed", text=transcript[:4000],
                      payload={"file": f"bot/voice/{month}/{fname}", "duration": media.get("duration"),
                               "message_id": msg.get("message_id"), "from_id": (msg.get("from") or {}).get("id"),
                               "from_name": _sender_name(msg.get("from") or {})}))
    return transcript


def _pending_summary(db: Session) -> BotMessage | None:
    return (db.query(BotMessage).filter(BotMessage.kind == "founders_review", BotMessage.status == "pending")
            .order_by(BotMessage.id.desc()).first())


def _approve_summary(db: Session) -> str:
    p = _pending_summary(db)
    if p is None:
        return "Сводки на проверке нет. Напишите «сводка», соберу свежую."
    p.status = "approved"
    _send_founders(db, (p.payload or {}).get("summary") or p.text, p.job_key)
    return "Отправил Айдай и Таласу."


def _handle_pocket_answer(db: Session, user: User, site: Organization, text: str) -> str:
    ask = (db.query(BotMessage).filter(BotMessage.kind == "pocket_ask", BotMessage.user_id == user.id)
           .order_by(BotMessage.id.desc()).first())
    expected = cash.pocket_balance(db, site.id, user.id)
    low = text.lower().strip()
    if low in ("да", "верно", "+"):
        actual, reason = expected, "подтверждено в боте"
    else:
        m = _NUM.match(text)
        if not m:
            return "Не понял. Ответьте «да» или суммой, например «60000 отдала за хлеб»."
        actual = Decimal(m.group(1).replace(" ", "").replace(",", "."))
        reason = m.group(2).strip()
    delta = actual - expected
    if abs(delta) > rules.pocket_delta_threshold(db) and not reason:
        return (f"Записал бы {fmt_money(float(actual))}, но разница с записями {fmt_money(float(delta))}. "
                "Напишите ту же сумму и почему, или поправьте в приложении.")
    rec = cash.recount(db, user=user, site_org_id=site.id, pocket_user_id=user.id, actual=actual, d=date.today(),
                       reason=reason or None)
    if ask is not None:
        ask.status = "answered"
    if abs(delta) < Decimal("0.01"):
        return f"Записано: у {user.name} {fmt_money(float(actual))}, сошлось."
    return f"Записано: у {user.name} {fmt_money(float(actual))}, разница {fmt_money(float(delta))}. " + \
           ("Причина записана." if reason else "Напишите почему, или поправьте в приложении.")


def intake_photo(db: Session, site: Organization, author: User | None, data: bytes, file_unique_id: str | None,
                 caption: str, source: str, mime: str | None = None,
                 file_name: str | None = None) -> tuple[str | None, dict, "Receipt | None"]:
    """Фото (или файл-картинка, PDF) из чата или лички → черновик на проверку Махабат
    (11_bot_inbox.md, шаг 1). Чек и лист кухни становятся черновиком; остальное бот
    только понимает и отвечает. Возвращает (ответ, что понято, черновик)."""
    from app.services import bot_group as grp, drafts
    today_d = date.today()
    is_pdf = (mime or "") == "application/pdf"
    same = drafts.find_same(db, compute_hash(data), file_unique_id)
    if same is not None:
        dup = {"date": same.created_at.date() if same.created_at else None, "receipt_id": same.id}
        return grp.photo_reply(db, site, author, {"kind": "dup"}, today_d, dup=dup), {"kind": "dup", "same": same.id}, None
    if is_pdf:
        info = grp.apply_caption(grp.read_photo(None, today_d, pdf=data), caption)
    else:
        info = grp.apply_caption(grp.read_photo(data, today_d, mime or "image/jpeg"), caption)
    if file_name:
        info["file_name"] = file_name
    reply = grp.photo_reply(db, site, author, info, today_d)
    kind = drafts.KIND_FROM_BOT.get(info["kind"])
    if kind == drafts.KITCHEN and not info.get("sure") and "кухн" not in caption.lower():
        kind = None   # «лист кухни или пересчёт?» — сначала ответ человека
    if is_pdf and kind is not None:
        # PDF (платёжка из банка, счёт-фактура): экран проверки показывает картинку,
        # PDF в нём не откроется — пока только понимаем и отвечаем, черновик не заводим
        return (reply or "Документ прочитал.") + " PDF в черновики пока не кладу — отправьте фото или скрин.", info, None
    if kind is None:
        return reply, info, None
    extra = {}
    if kind == drafts.SERVICE:
        supplier = grp.find_supplier(db, info.get("supplier"))
        if supplier is not None:
            extra["supplier_id"], extra["supplier_name"] = supplier.id, supplier.name
    if kind == drafts.RECEIPT:
        supplier = grp.find_supplier(db, info.get("supplier"))
        if supplier is not None:
            extra["supplier_id"], extra["supplier_name"] = supplier.id, supplier.name
        if info.get("amount"):
            m = grp.match_expense(db, site.id, info, supplier, today_d)
            if m["status"] == "found":
                extra["match"] = "похоже, уже внесено"
            elif m["status"] == "similar":
                extra["match"] = f"похоже, уже внесено: {m['supplier']} {grp._dd(m['date'])} на {grp.fmt_money(m['total'])}"
            else:
                extra["match"] = "в системе нет"
    draft = drafts.create(db, site_org_id=site.id, author=author, data=data, kind=kind, source=source,
                          info={**info, **extra}, file_unique_id=file_unique_id,
                          ext={"image/png": ".png", "image/webp": ".webp"}.get(mime or "", ".jpg"))
    tail = " Черновик у Махабат на проверке."
    return ((reply or ({drafts.KITCHEN: "Лист кухни.", drafts.SERVICE: "Услуга."}.get(kind, "Чек."))) + tail), info, draft


def owner_copy(db: Session, text: str) -> None:
    """Копия владельцу в личку (первый этап: видит каждый черновик и каждую проводку).
    Без Start у бота Telegram не даёт писать первым — тогда только журнал."""
    owner = db.get(User, OWNER_USER_ID)
    send(db, owner.tg_id if owner else None, text, "owner_copy", user_id=OWNER_USER_ID)


def _handle_photo(db: Session, msg: dict, user: User, site: Organization, caption: str) -> str:
    """Фото или файл в личку: тот же путь, что из группы — черновик на проверку Махабат."""
    media = msg.get("_doc") or _media(msg)
    data = download_file(media["file_id"])
    if data is None:
        return "Не смог скачать файл. Попробуйте ещё раз."
    try:
        reply, _info, draft = intake_photo(db, site, user, data, media.get("file_unique_id"), caption, "private",
                                           mime=media.get("mime"), file_name=media.get("file_name"))
    except Exception:  # noqa: BLE001 — модель недоступна: фото не теряем, кладём как чек
        from app.services import drafts
        if (media.get("mime") or "") == "application/pdf":
            return "Файл получил, разобрать не смог. Отправьте фото или скрин."
        draft = drafts.create(db, site_org_id=site.id, author=user, data=data, kind=drafts.RECEIPT, source="private",
                              file_unique_id=media.get("file_unique_id"))
        reply = "Фото сохранил, разобрать не смог. Черновик у Махабат на проверке."
    if draft is not None and user.id != OWNER_USER_ID:
        owner_copy(db, f"Черновик от {user.name}: {reply}")
    return reply or "Понял. Это не чек и не лист кухни — в черновики не кладу."


def download_file(file_id: str) -> bytes | None:
    if not token():
        return None
    try:
        info = httpx.get(f"https://api.telegram.org/bot{token()}/getFile", params={"file_id": file_id}, timeout=20).json()
        path = info["result"]["file_path"]
        return httpx.get(f"https://api.telegram.org/file/bot{token()}/{path}", timeout=60).content
    except Exception:  # noqa: BLE001
        return None


def set_webhook(base_url: str) -> dict:
    if not token():
        return {"ok": False, "description": "нет токена"}
    r = httpx.post(f"https://api.telegram.org/bot{token()}/setWebhook",
                   json={"url": f"{base_url.rstrip('/')}/bot/webhook/{webhook_secret()}",
                         "allowed_updates": ["message"]}, timeout=20)
    return r.json()
