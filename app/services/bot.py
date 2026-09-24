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
# Копии владельцу, которые днём не шлём, а копим до вечерней строки (24.09)
NOTE_KINDS = ("owner_copy", "group_reply_owner", "group_voice_owner")
OWNER_EVENING_HOUR = 18
COUNT_GRACE_DAYS = 3                   # пересчёт за столько дней до дня пересчёта засчитан

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
    if user_id == OWNER_USER_ID and kind in NOTE_KINDS:
        # Владельцу днём не пишем (24.09: «голова пойдёт кругом»): заметка ждёт вечерней строки
        # и видна на /new/settings/bot/chat
        msg.status = "noted"
        return msg
    if not token() or chat_id is None:
        msg.status = "logged"
        return msg
    if user_id != OWNER_USER_ID and rules.bot_paused(db):
        # Надзор (владелец 24.09): пока Claude в сессии, бот людям сам не пишет — сообщение ждёт
        # в журнале со статусом «paused», Claude выпускает его (scripts/bot_queue.py) или гасит
        owner = db.get(User, OWNER_USER_ID)
        if owner is None or chat_id != owner.tg_id:
            msg.status = "paused"
            if reply_to:
                msg.payload = {**(payload or {}), "reply_to": reply_to}
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
    sent += _week_praise(db, site, now)
    sent += _purchases_ask(db, site, now)
    sent += _morning_checks(db, site, now)
    from app.services import bot_money
    sent += bot_money.settle_deferred(db, site, now.date())
    sent += _owner_evening(db, site, now)
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
    # Владелец 24.09: не переспрашивать — второго напоминания днём нет, незаписанное
    # всплывёт в вечернем сообщении 17:00 вместе с закупками
    slot_m = "first" if _at(now, rules.get(db, "meal_ask_time")) else None
    if meals.missing_today(db, site.id) and slot_m:
        key = f"meal_ask:{d.isoformat()}:{slot_m}"
        if not _done(db, key):
            first = slot_m == "first"
            # 15:00 — не упрёк, а как проще (владелец 23.09: «мягко, но твёрдо»)
            text = (f"{hi}сколько сегодня едят? Одной строкой, например:\n{meals.EXAMPLE}" if first else
                    f"{hi}если сейчас некогда — можно и завтра утром одной строкой: "
                    "«вчера: школа …, садик …, персонал …». Главное, чтобы день не пропал.")
            send(db, group, text, "meal_ask", job_key=key)
            out.append(key)
    # Пересчёт (владелец 23.09): в день пересчёта после кухни — 15:00, напоминание в 18:00,
    # запасной — утром следующего дня до поваров. Вместе с ним — остаток в банке по
    # счетам, где его давно не вносили: расход со счёта без чека всплывает за неделю.
    keys = rules.key_products(db)
    cw = rules.count_weekday(db)
    slot = {(cw, 15): "first", ((cw + 1) % 7, 8): "last"}.get((d.weekday(), now.hour))   # 18:00 убрано (24.09)
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
                lines.append((f"{who}, " if who else "") + ("и " if lines else "") + "остаток в банке на конец вчерашнего дня, одной цифрой: "
                             + ", ".join(a["org"].name for a in bank) + ". Касса → Остаток в банке.")
            send(db, group, "\n\n".join(lines), "count_ask", job_key=key)
            out.append(key)
    return out


def _at(now: datetime, hhmm: str) -> bool:
    """Окно «в этот час, начиная с минуты»: тик раз в минуту, ключ раз в день — сработает
    первый тик после 11:20 и не повторится."""
    h, m = (int(x) for x in str(hhmm).split(":"))
    return now.hour == h and now.minute >= m


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
    who = _counter_name(db, site) or "учётчик"
    # едоки: бот спрашивал сегодня и в прошлый рабочий день, записи нет ни за один
    prev = d - timedelta(days=1)
    while not meals.expected_today(db, site.id, prev) and prev > d - timedelta(days=7):
        prev -= timedelta(days=1)
    if (now.hour == 16 and all(_done(db, f"meal_ask:{x.isoformat()}:first") and meals.get(db, site.id, x) is None
                               for x in (prev, d))):
        out.append(f"{who} второй день не присылает, сколько едят ({_d(prev)} и сегодня)")
    # пересчёт: утренний запасной вопрос был, а пересчёта так и нет
    if now.hour != 12:
        return out   # остальное — один раз в день, в 12
    if rules.key_products(db) and _done(db, f"count_ask:{d.isoformat()}:8") and not key_count_done(db, site.id, d):
        out.append(f"{who} не успела пересчитать склад за {WEEKDAY_ACC[rules.count_weekday(db)]} — неделю не с чем сравнить")
    # остаток в банке: спрашивали в день пересчёта, до сих пор не внесён
    start = count_week_start(db, d)
    if d != start and _done(db, f"count_ask:{start.isoformat()}:15"):
        for a in cash.bank_due(db, site.id, start):
            out.append(f"{_bank_holder(db, site) or 'управляющая'}: остаток в банке по счёту {a['org'].name} на этой неделе не пришёл")
    # чеки из чата ждут проверки дольше 3 дней
    edge = datetime.combine(d - timedelta(days=RECEIPTS_STUCK_DAYS), datetime.min.time())
    old = [r for r in today.unchecked_receipts(db, site.id) if r.created_at and r.created_at < edge]
    if old:
        out.append(f"{who}: {len(old)} черновиков из чата ждут проверки больше {RECEIPTS_STUCK_DAYS} дней")
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
    # Не жалоба, а просьба помочь (владелец 23.09): сотрудник этого не видит, учредитель
    # спрашивает «что мешает?» — если неудобно, узнаём и чиним систему, а не человека.
    body = ("Похоже, не успевают:\n" + "\n".join(f"• {x}" for x in items)
            + "\n\nБот уже мягко напоминал в чате. Может, спросите, что мешает — вдруг неудобно или нужна помощь.")
    start = rules.escalate_from(db)
    if now.date() >= start:
        for f in db.query(User).filter(User.role == "founder", User.deleted_at.is_(None), User.tg_id.isnot(None)).all():
            send(db, f.tg_id, f"{f.name}, это бот Жемчужины. {body}", "escalate", user_id=f.id, job_key=f"{key}:{f.id}")
        head = "Копия учредителям. "
    else:
        head = f"Привыкание: до {_d(start)} учредителям не пишу, только вам. "
    owner = db.get(User, OWNER_USER_ID)
    send(db, owner.tg_id if owner else None, head + body, "escalate", user_id=OWNER_USER_ID, job_key=key)
    return [key]


def _week_praise(db: Session, site: Organization, now: datetime) -> list[str]:
    """Пятница 16:00 — похвала в группу, только за сделанное; не за что — молчим.
    Хвалим при всех, вопросы — лично (владелец 23.09)."""
    from app.services import meals
    d = now.date()
    if d.weekday() != 4 or now.hour != 16:
        return []
    key = f"praise:{d.isoformat()}"
    if _done(db, key):
        return []
    monday = d - timedelta(days=d.weekday())
    asked = [x for x in (monday + timedelta(days=i) for i in range(5))
             if meals.expected_today(db, site.id, x) and _done(db, f"meal_ask:{x.isoformat()}:first")]
    good = []
    if asked and all(meals.get(db, site.id, x) is not None for x in asked):
        good.append(f"сколько едят — каждый день, {len(asked)} из {len(asked)}")
    if rules.key_products(db) and _done(db, f"count_ask:{count_week_start(db, d).isoformat()}:15") \
            and key_count_done(db, site.id, d):
        good.append("пересчёт склада сделан")
    if not good:
        db.add(BotMessage(kind="praise", job_key=key, status="skipped"))
        return []
    who = _counter_name(db, site)
    send(db, group_chat_id(), "Неделя: " + ", ".join(good) + "." + (f" {who}, спасибо!" if who else " Спасибо!"),
         "praise", job_key=key)
    return [key]


def key_count_done(db: Session, site_org_id: int, d: date) -> bool:
    """Ключевые пересчитаны с начала этой «недели пересчёта» (с последнего дня пересчёта).
    Пересчёт за COUNT_GRACE_DAYS до дня пересчёта тоже засчитан (24.09: точку ноль
    записали в среду — в четверг считать заново незачем)."""
    from app.models import StockCount, StockCountLine
    keys = rules.key_products(db)
    if not keys:
        return True
    since = count_week_start(db, d) - timedelta(days=COUNT_GRACE_DAYS)
    got = {pid for (pid,) in db.query(StockCountLine.product_id).join(StockCount, StockCount.id == StockCountLine.count_id)
           .filter(StockCount.organization_id == site_org_id, StockCount.status == "applied",
                   StockCount.count_date >= since, StockCountLine.product_id.in_(keys)).distinct().all()}
    return len(got) >= max(1, len(keys) // 2)   # половина ключевых — считаем, что пересчёт был


def meal_reply(db: Session, site: Organization, user: User | None, text: str) -> str | None:
    """Строка про едоков → запись. None — это не про едоков."""
    from app.services import meals
    if user is None or user.role not in ("owner", *OPERATIONAL_ROLES):
        return None
    p = meals.parse(text)
    if p is None:
        # дописка к начатому дню после переспроса: «садик 97» или «меню: …»
        p = meals.parse(text, min_fields=0)
        row = meals.get(db, site.id, p["date"]) if p else None
        if row is None or not meals.lacks(row) or not any(p.get(k) is not None for k in meals.lacks_keys(row)):
            return None
    row = meals.record(db, site_org_id=site.id, d=p["date"], values=p, menu=p.get("menu"), user=user, source="chat")
    when = "сегодня" if p["date"] == date.today() else f"на {_d(p['date'])}"
    reply = f"Записал {when}: {meals.text(row)}." + (f" Меню: {row.menu}." if row.menu else "")
    doubt = meals.doubts(db, site.id, p, p["date"])
    lack = meals.lacks(row)
    if doubt:
        reply += " Проверьте: " + "; ".join(doubt) + ". Если верно — ничего не делайте; ошиблись — пришлите строку заново."
    if lack:
        # неполное — переспросить сразу, с образцом (владелец 23.09: бот сам дожимает, не я)
        reply += " Не хватает: " + ", ".join(lack) + ". Допишите одной строкой, например: «" + meals.lack_example(row) + "»."
    elif not doubt:
        reply += " Спасибо!"
    return reply


def _send_founders(db: Session, text: str, key: str | None = None) -> None:
    for f in db.query(User).filter(User.role == "founder", User.deleted_at.is_(None)).all():
        if f.tg_id:
            send(db, f.tg_id, text, "founders_summary", user_id=f.id,
                 job_key=f"{key}:{f.id}" if key else None)


# ── входящие ─────────────────────────────────────────────────────────────

_NUM = re.compile(r"^\s*(\d[\d\s]*(?:[.,]\d+)?)\s*(.*)$", re.S)


def parse_amount(s: str) -> Decimal:
    """«90,055» и «12.500» — тысячи (Айжан 24.09: бот прочитал 90), «1,5» и «12.50» — дробь.
    Запятая или точка ровно с тремя цифрами после и без другой дроби — разделитель тысяч."""
    t = re.sub(r"\s+", "", s or "")
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", t):
        return Decimal(re.sub(r"[.,]", "", t))
    return Decimal(t.replace(",", "."))
# Ответ на утренний вопрос: «да» / «да, хлеб в долг» — подтверждение (хвост — причина);
# «нет» / «нет, 3500 …» — дальше ждём цифру. Хвост «да» без цифр, иначе это не «да».
_YES = re.compile(r"^\s*(?:да|верно|\+)(?![а-яё\w])[\s,.!—-]*([^\d]*)$", re.I | re.S)
# на «записываю …, верно?» цифры в хвосте — только причина («да, Optima 18.09 в пути»)
_YES_REASON = re.compile(r"^\s*(?:да|верно|\+)(?![а-яё\w])[\s,.!—-]*(.*)$", re.I | re.S)
_NO = re.compile(r"^\s*(?:нет|неверно|не верно)(?![а-яё\w])[\s,.!—-]*(.*)$", re.I | re.S)


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
    if user is None and from_id:
        user = auto_link(db, from_id, _sender_name(sender))
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
    if media and media["kind"] == "file" and user:
        reply = _save_file(db, msg, media, user, chat_id)
        send(db, chat_id, reply, "reply", user_id=user.id)
        return reply
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
        if reply is None:
            return _last_out(db, user.id)   # вопрос «Верно?» уже ушёл в этот чат
        send(db, chat_id, reply, "reply", user_id=user.id)
        return reply
    if site is not None and (meal := meal_reply(db, site, user, text)):
        send(db, chat_id, meal, "reply", user_id=user.id)
        return meal
    if site is not None and (morning := morning_answer(db, site, user, text)) is not None:
        if morning:
            send(db, chat_id, morning, "reply", user_id=user.id)
        return morning or None
    if site is not None and (buy := _private_buy(db, site, user, text)):
        send(db, chat_id, buy, "reply", user_id=user.id)
        return buy
    if site is not None and (stock_reply := stock_text_reply(db, site, user, text, "private")):
        send(db, chat_id, stock_reply, "reply", user_id=user.id)
        return stock_reply
    low = text.lower()
    if low in ("/start", "start") and user.role == "founder":
        reply = (f"Здравствуйте, {user.name}. Я буду писать вам, только когда что-то застряло: "
                 "бот спросил в чате, а ответа нет. Остальное люди и бот решают сами.")
    elif low in ("/start", "start"):
        who = "вам" if user.role == "staff" else "Махабат"
        reply = (f"Здравствуйте, {user.name}. Фото чека, остаток или сколько едят можно присылать сюда или в чат "
                 f"«Жемчужина»: я разберу и дам {who} проверить одной кнопкой. Иногда буду писать сюда лично.")
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
    elif site is not None and (money := _private_money(db, site, user, text)) is not None:
        return money or None
    elif _answer_to_question(db, user, text):
        return None
    elif user.role in OPERATIONAL_ROLES and site is not None and _NUM.match(text):
        reply = ("Сумму на руках запишет Махабат в Кассе («Пересчитать наличные»). "
                 "Скоро такие сообщения будут сами становиться черновиком ей на проверку.")
    elif msg.get("_voice"):
        reply = f"Записал: «{text[:600]}»"
    else:
        # Непонятный текст (24.09, владелец: дежурная фраза сбивает людей) — молчим, владельцу копия
        if user.id != OWNER_USER_ID:
            owner_copy(db, f"{user.name} написал(а) боту, не понял: «{text[:200]}»")
        return None
    send(db, chat_id, reply, "reply", user_id=user.id)
    return reply


# «на руках 15 000», «наличных 0», «Нал остаток 51090-12700=38 390» (Мунара 24.09): берём последнее число
_ON_HAND = re.compile(r"(?:на\s*руках|налич\w*|\bнал\b\.?)\D{0,25}?(\d[\d\s]*(?:[.,]\d+)?)"
                      r"(?:[^\n]*?=\s*(\d[\d\s]*(?:[.,]\d+)?))?"
                      r"|(\d[\d\s]*(?:[.,]\d+)?)\D{0,25}?(?:на\s*руках|налич|\bнал\b)", re.I)
# «Комиссия», «камисса», «да комиссия» — ответ на подсказку «ответьте „да, комиссия“» (Мунара 24.09)
_COMMISSION = re.compile(r"^\s*(?:да[\s,.!—-]*)?к[ао]м+[иеы]с+\w*\s*(?:банк\w*)?\s*[.!]*\s*$", re.I)
_BUY = re.compile(r"^\s*(?:ещ[её]\s+)?закуп\w*\s*[-—:]?\s*(\d[\d\s]*(?:[.,]\d+)?)", re.I)


def on_hand_amount(text: str):
    """Сумма «на руках» из текста; при «51090-12700=38 390» — результат после «=»."""
    m = _ON_HAND.search(text or "")
    if not m:
        return None
    raw = m.group(2) or m.group(1) or m.group(3)
    return parse_amount(raw) if raw else None


def _last_out(db: Session, user_id: int) -> str | None:
    m = (db.query(BotMessage).filter(BotMessage.user_id == user_id, BotMessage.direction == "out")
         .order_by(BotMessage.id.desc()).first())
    return m.text if m else None


def _private_buy(db: Session, site: Organization, user: User, text: str) -> str | None:
    """«Ещё закуп 12770» в личку (Мунара 24.09): если такой чек уже в черновиках — сказать это,
    иначе — попросить фото чека. Суммы без чека не записываем."""
    from app.services import bot_group as grp
    m = _BUY.match(text or "")
    if not m or user.role not in OPERATIONAL_ROLES:
        return None
    amount = float(parse_amount(m.group(1)))
    since = datetime.combine(date.today(), datetime.min.time())
    for r in db.query(Receipt).filter(Receipt.kind == "receipt", Receipt.ocr_status == "pending",
                                      Receipt.created_at >= since).all():
        if abs(float((r.payload or {}).get("amount") or 0) - amount) < 1:
            return f"Чек на {fmt_money(amount)} уже у Махабат на проверке."
    return (f"{grp._first(user.name)}, закуп {fmt_money(amount)} — пришлите фото чека сюда: "
            "я подготовлю запись из ваших наличных, Махабат проверит и подтвердит.")


def _answer_to_question(db: Session, user: User, text: str) -> bool:
    """Короткий текст при открытом вопросе бота «ответьте одним словом» (третье фото, 24.09:
    «Часть чека») — это ответ, не новое сообщение: вопрос закрываем, владельцу — что ответили."""
    since = datetime.combine(date.today(), datetime.min.time())
    q = (db.query(BotMessage).filter(BotMessage.user_id == user.id, BotMessage.direction == "out",
                                     BotMessage.kind == "group_question", BotMessage.status.in_(("sent", "logged")),
                                     BotMessage.created_at >= since).order_by(BotMessage.id.desc()).first())
    if q is None or len((text or "").split()) > 4:
        return False
    q.status = "answered"
    q.payload = {**(q.payload or {}), "answer": text[:200]}
    owner_copy(db, f"{user.name} на «{q.text[:100]}…» ответил(а): «{text[:200]}»")
    return True


def _private_money(db: Session, site: Organization, user: User, text: str) -> str | None:
    """«сняла 25 000», «передала Махабат 20 000» в личку (шаг 2, 24.09): вопрос «Верно?»
    тому, чьи деньги. Себе — он и есть ответ; про другого — говорим, кого спросили."""
    from app.services import bot_group as grp, bot_money
    if not grp.worth_reading(text):
        return None
    if (amount := on_hand_amount(text)) is not None:
        # «на руках 15 000», «наличных 0» — точка кармана автора (Айжан, старт школы 24.09)
        info = {"kind": "pocket", "amount": float(amount), "date": date.today()}
        asked = bot_money.offer(db, site, user, info, date.today())
        return asked.text if asked is not None else None
    try:
        info = grp.read_text(db, text, user, date.today())
    except Exception:  # noqa: BLE001 — модель недоступна: обычный ответ ниже
        return None
    if info.get("kind") is None:
        return None
    asked = bot_money.offer(db, site, user, info, date.today())
    if asked is not None and asked.user_id == user.id:
        return asked.text
    if asked is None and info.get("kind") in (grp.BANK, grp.BALANCE):
        # Не спросили (сомнение → владельцу, или уже записано): остаток счёта человеку не
        # озвучиваем (24.09: «51090 остаток у меня наличка» ушло Мунаре как остаток счёта)
        return ""   # молчание, без «не понял» владельцу
    reply = (grp.text_reply(db, site, user, info, date.today()) or "Понял.") + bot_money.asked_note(db, asked)
    send(db, user.tg_id, reply, "reply", user_id=user.id)
    return reply


def _handle_group(db: Session, msg: dict, user: User | None, text: str) -> str | None:
    """Группа «Жемчужина», день 1 (21.09): понять и ответить под сообщением,
    ничего не записывая. Всё, что бот понял, — в журнале (payload), по нему
    владелец смотрит, как бот распознаёт, прежде чем разрешить запись."""
    from app.services import bot_group as grp, bot_money
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
        if media and media["kind"] == "file" and user is not None:
            # Excel/Word в группу (Махабат 24.09: ответы по детям): сохраняем как из лички, в группе молчим
            _save_file(db, msg, media, user, chat_id)
            return None
        if not (media and media["kind"] in ("photo", "document")):
            # Едоки за день (23.09): разбор без модели, запись сразу. Отвечаем в группе
            # и при молчащем боте — человек должен видеть «Записал», иначе пришлёт ещё раз.
            meal = meal_reply(db, site, user, text)
            if meal:
                db.add(BotMessage(kind="meal_count", chat_id=chat_id, user_id=user.id if user else None, direction="in",
                                  text=text[:2000], status="understood", payload={"message_id": message_id, "reply": meal}))
                send(db, chat_id, meal, "group_reply", user_id=user.id if user else None, reply_to=message_id)
                return meal
            if user is not None and NO_BUY.search((text or "").lower()):
                db.add(BotMessage(kind="no_purchases", chat_id=chat_id, user_id=user.id, direction="in",
                                  text=text[:500], status="understood", job_key=f"no_purchases:{today_d.isoformat()}:{message_id}"))
                ok = "Понял, спасибо: сегодня без закупок."
                send(db, chat_id, ok, "group_reply", user_id=user.id, reply_to=message_id)
                return ok
            # «Остаток наличными 51090» (Мунара 24.09): наличные на руках, не счёт — модель путала.
            # Без модели: вопрос «верно?» в личку автору, в группе — молчим.
            if user is not None and user.role in OPERATIONAL_ROLES and (amount := on_hand_amount(text)) is not None:
                asked = bot_money.offer(db, site, user, {"kind": "pocket", "amount": float(amount), "date": today_d}, today_d)
                db.add(BotMessage(kind="pocket_text", chat_id=chat_id, user_id=user.id, direction="in", text=text[:500],
                                  status="understood", payload={"message_id": message_id, "amount": float(amount)}))
                if asked is not None:
                    owner = db.get(User, OWNER_USER_ID)
                    send(db, owner.tg_id if owner else None, f"Группа, {user.name}, «{text[:80]}»: наличные на руках — "
                         + bot_money.asked_note(db, asked).strip(), "group_reply_owner", user_id=OWNER_USER_ID)
                return None
            if user is not None and re.match(r"^\s*закуп\w*\s*[-—:]?\s*\d", text.lower()):
                # «Закуп 13570» — суммы без чека не записываем: закуп идёт строками со склада
                m_buy = re.search(r"\d[\d\s]*(?:[.,]\d+)?", text)
                buy_sum = fmt_money(float(parse_amount(m_buy.group(0)))) if m_buy else ""
                reply_buy = (f"{grp._first(user.name)}, закуп {buy_sum} — пришлите фото чека сюда: "
                             "я подготовлю запись из ваших наличных, Махабат проверит и подтвердит.")
                send(db, chat_id, reply_buy, "group_reply", user_id=user.id, reply_to=message_id)
                return reply_buy
            stock_reply = stock_text_reply(db, site, user, text, "chat")
            if stock_reply:
                db.add(BotMessage(kind="stock_text", chat_id=chat_id, user_id=user.id if user else None, direction="in",
                                  text=text[:2000], status="understood", payload={"message_id": message_id, "reply": stock_reply}))
                send(db, chat_id, stock_reply, "group_reply", user_id=user.id if user else None, reply_to=message_id)
                return stock_reply
        if voice_note:
            # Голосовое (23.09): расшифровка в журнал всегда, дальше как текст о деньгах
            if grp.worth_reading(text):
                kind = "group_text"
                info = grp.read_text(db, text, user, today_d)
                payload.update({k: (v.isoformat() if isinstance(v, date) else v) for k, v in info.items()})
                reply = grp.text_reply(db, site, user, info, today_d)
                if reply:
                    reply += bot_money.asked_note(db, bot_money.offer(db, site, user, info, today_d))
        elif media and media["kind"] in ("photo", "document"):
            kind = "group_photo" if media["kind"] == "photo" else "group_document"
            payload["file_unique_id"] = media.get("file_unique_id")
            data = download_file(media["file_id"])
            if data is None:
                return None
            reply, info, draft = intake_photo(db, site, user, data, media.get("file_unique_id"), text, "chat",
                                              mime=media.get("mime"), file_name=media.get("file_name"))
            payload.update({k: (v.isoformat() if isinstance(v, date) else v) for k, v in info.items()})
            if reply and draft is None and "Подпишите фото" in reply and user is not None and user.tg_id                     and not group_talks():
                # Уточнение по фото (24.09: «кухня или остаток?») в молчащем режиме уходило только
                # владельцу — отправитель его не видел. Вопрос — тому, кто прислал, в личку.
                send(db, user.tg_id, f"{grp._first(user.name)}, фото в группе — {reply[0].lower() + reply[1:]}",
                     "group_question", user_id=user.id)
            if reply and draft is None and info.get("kind") == grp.BANK:
                reply += bot_money.asked_note(db, bot_money.offer(db, site, user, info, today_d))
            if draft is not None:
                payload["draft_id"] = draft.id
                if draft.kind == "count":
                    # лист остатка (23.09): Махабат должна видеть ссылку — отвечаем в группе всегда
                    link = draft_link_text(db, site, user, draft)
                    send(db, chat_id, link, "group_reply", user_id=user.id if user else None, reply_to=message_id)
        elif grp.worth_reading(text):
            kind = "group_text"
            info = grp.read_text(db, text, user, today_d)
            payload.update({k: (v.isoformat() if isinstance(v, date) else v) for k, v in info.items()})
            reply = grp.text_reply(db, site, user, info, today_d)
            if reply:
                reply += bot_money.asked_note(db, bot_money.offer(db, site, user, info, today_d))
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
    # Остаток счёта в общий чат не озвучиваем, даже если бот в группе говорит (24.09: скрин
    # банка, брошенный в группу по ошибке, — вопрос «верно?» ушёл в личку, в чате — тишина)
    confidential = payload.get("kind") in (grp.BANK, grp.BALANCE)
    if reply:
        if group_talks() and not confidential:
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


def auto_link(db: Session, from_id: int, name: str | None) -> User | None:
    """Незнакомый в Telegram, но имя совпадает ровно с одним человеком без привязки
    («Махабат Керимкуловна» → Махабат): привязываем сами и говорим владельцу (23.09 —
    Махабат ответила в группу, а бот молчал, пока владелец не привяжет руками)."""
    first = (name or "").split()[0].lower().replace("ё", "е") if (name or "").split() else ""
    if len(first) < 3:
        return None
    hits = [u for u in db.query(User).filter(User.tg_id.is_(None), User.deleted_at.is_(None)).all()
            if u.name and u.name.split()[0].lower().replace("ё", "е") == first]
    if len(hits) != 1:
        return None
    u = hits[0]
    u.tg_id = from_id
    db.query(BotMessage).filter(BotMessage.user_id.is_(None), BotMessage.direction == "in",
                                BotMessage.payload["from_id"].as_string() == str(from_id)).update(
        {BotMessage.user_id: u.id}, synchronize_session=False)
    audit(db, "user", u.id, "update", None, {"tg_id": from_id, "auto_link": name})
    owner = db.get(User, OWNER_USER_ID)
    if owner and owner.tg_id:
        send(db, owner.tg_id, f"Узнал по имени и привязал: {name} → {u.name}. Если не так — Настройки бота.",
             "reply", user_id=OWNER_USER_ID)
    return u


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
    if d and d.get("file_id"):
        # Excel/Word/csv (таблица сумм по детям от Айжан, 24.09): сохранить и отдать владельцу
        return {"kind": "file", "file_id": d["file_id"], "file_unique_id": d.get("file_unique_id"),
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


def _save_file(db: Session, msg: dict, media: dict, user: User, chat_id: int | None) -> str:
    """Файл не картинка и не PDF (Excel по детям, список сотрудников): в media/bot/files,
    владельцу — что пришло и где лежит. Разбирать такие файлы бот не пробует."""
    data = download_file(media["file_id"])
    if data is None:
        return "Файл не смог скачать. Попробуйте ещё раз."
    month = datetime.now().strftime("%Y-%m")
    folder = MEDIA_ROOT / "bot" / "files" / month
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-а-яА-ЯёЁ ]", "_", media.get("file_name") or "file")
    fname = f"{(media.get('file_unique_id') or compute_hash(data)[:12])}_{safe}"
    (folder / fname).write_bytes(data)
    rel = f"bot/files/{month}/{fname}"
    db.add(BotMessage(kind="file", chat_id=chat_id, user_id=user.id, direction="in", status="saved",
                      text=media.get("file_name") or "", payload={"file": rel, "message_id": msg.get("message_id")}))
    if user.id != OWNER_USER_ID:
        owner_copy(db, f"Файл от {user.name}: {media.get('file_name') or 'без имени'} — media/{rel}")
    return "Файл получил, передал Абдусаттару. Спасибо!"


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
    yes, no = _YES.match(text), _NO.match(text)
    if yes:
        actual, reason = expected, yes.group(1).strip() or "подтверждено в боте"
    else:
        m = _NUM.match(no.group(1) if no else text)
        if not m:
            # «нет, хлеб взяли в долг» (Махабат 24.09): «нет» без цифры — не угадываем, что
            # она имела в виду, а просим одну цифру; вопрос остаётся открытым
            return ("Сколько у вас на руках сейчас? Напишите цифрой, например «0» "
                    "или «3500, отдала за хлеб».")
        actual = parse_amount(m.group(1))
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


def _handle_photo(db: Session, msg: dict, user: User, site: Organization, caption: str) -> str | None:
    """Фото или файл в личку: тот же путь, что из группы — черновик на проверку Махабат."""
    media = msg.get("_doc") or _media(msg)
    data = download_file(media["file_id"])
    if data is None:
        return "Не смог скачать файл. Попробуйте ещё раз."
    try:
        reply, _info, draft = intake_photo(db, site, user, data, media.get("file_unique_id"), caption, "private",
                                           mime=media.get("mime"), file_name=media.get("file_name"))
        if reply and draft is None and _info.get("kind") == "bank":
            # скрин банка в личку (шаг 2): держателю счёта — «записываю остаток, верно?»
            from app.services import bot_money
            asked = bot_money.offer(db, site, user, _info, date.today())
            if asked is not None and asked.user_id == user.id:
                return None
            reply += bot_money.asked_note(db, asked)
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


# ── остаток и передача текстом (23.09) ──────────────────────────────────

_STOCK_WORDS = r"остат|пересч[её]т|на складе"
_TRANSFER_WORDS = r"кожомкул|филиал|передал|передан|отправил|отдали"


def stock_text_kind(text: str) -> str | None:
    """«остаток склада: молоко 80л, рис 30кг, …» → count; «передали в Кожомкул 12 л молока» → transfer.
    Передачу проверяем первой: в ней тоже бывает «из остатка». Остаток — хотя бы три числа."""
    low = (text or "").lower()
    nums = re.findall(r"\d", low)
    if not nums or not re.search(r"[а-я]{3,}", low):
        return None
    # «передала Махабат 20 000» — деньги, не продукты: без единицы веса/объёма и без
    # названия другого садика передачей продуктов не считаем
    units = re.search(r"\d\s*(кг|г|гр|л|литр|шт|пач|мешок|уп)\b", low)
    if re.search(_TRANSFER_WORDS, low) and (units or re.search(r"кожомкул|филиал", low)):
        return "transfer"
    if re.search(_STOCK_WORDS, low) and len(re.findall(r"\d+(?:[.,]\d+)?", low)) >= 3:
        return "count"
    return None


def public_url() -> str:
    return os.getenv("PUBLIC_BASE_URL", "https://pearl-production-eef5.up.railway.app").rstrip("/")


def draft_link_text(db: Session, site: Organization, user: User | None, draft) -> str:
    from app.services import drafts
    try:
        rows = drafts.stock_rows(db, draft, site.id)
    except Exception:  # noqa: BLE001 — модель недоступна: разберётся при открытии
        rows = []
    found = [r for r in rows if r.get("product_id")]
    hi = f"{user.name}, " if user else ""
    # простыми словами, без «черновик» и номеров (владелец 23.09: «даже мне непонятно»)
    what = "остаток" if draft.kind == drafts.COUNT else "что отдали"
    path = "count" if draft.kind == drafts.COUNT else "transfer"
    n = f" ({len(found)} продуктов)" if found else ""
    return (f"{hi}принял {what}{n}. Откройте, проверьте цифры и нажмите «Записать»:\n"
            f"{public_url()}/new/stock/{path}?draft={draft.id}")


def stock_text_reply(db: Session, site: Organization, user: User | None, text: str, source: str) -> str | None:
    """Остаток или передача текстом → черновик (подтверждает человек), ответ со ссылкой.
    Пишут только люди площадки; сам текст сохраняется файлом сразу."""
    from app.services import drafts
    kind = stock_text_kind(text)
    if kind is None or user is None or user.role not in ("owner", *OPERATIONAL_ROLES):
        return None
    # Уточнение к своему открытому остатку за сегодня (Махабат 23.09: «80л до, после −12л, остаток 68л»
    # стало вторым черновиком): короткая строка без списка — дописываем в тот же черновик
    open_same = (db.query(Receipt).filter(Receipt.kind == kind, Receipt.created_by == user.id,
                                          Receipt.ocr_status.in_(drafts.OPEN),
                                          Receipt.created_at >= datetime.combine(date.today(), datetime.min.time()))
                 .order_by(Receipt.id.desc()).first())
    if open_same is not None and len(re.findall(r"[а-яё]{3,}\s*[:\-]?\s*\d", text.lower())) < 4:
        p = dict(open_same.payload or {})
        p["text"] = (p.get("text") or "") + "\nУточнение: " + text
        p.pop("rows", None)          # разобрать заново, уже с уточнением
        open_same.payload = p
        return "Добавил к остатку. " + draft_link_text(db, site, user, open_same)
    draft = drafts.create_text(db, site_org_id=site.id, author=user, text=text, kind=kind, source=source)
    # Доверие (владелец 23.09: «если бот будет гнать, подорвётся доверие к трансформации»):
    # ссылку даём, только если бот правда что-то понял. Остаток — хотя бы 3 товара из каталога,
    # передача — хотя бы один. Иначе черновик тихо закрываем и молчим: пустая ссылка хуже молчания.
    try:
        found = [r for r in drafts.stock_rows(db, draft, site.id) if r.get("product_id")]
    except Exception:  # noqa: BLE001 — модель недоступна: текст сохранён, разберём при открытии
        found = None
    if found is not None and len(found) < (3 if kind == drafts.COUNT else 1):
        draft.ocr_status, draft.reject_reason = "rejected", "бот не узнал товаров — не похоже на остаток/передачу"
        return None
    return draft_link_text(db, site, user, draft)


# ── закупки за день и утренние сверки (владелец 23.09) ──────────────────

NO_BUY = re.compile(r"без закуп|закуп\w* не было|ничего не покуп|не покупали")
PURCHASE_HOUR = 17
MORNING_HOUR = 9


def _bought_today(db: Session, site: Organization, d: date) -> bool:
    from app.models import Transaction
    org_ids = [o.id for o in site_orgs(db, site.id)] + [site.id]
    start = datetime.combine(d, datetime.min.time())
    return db.query(Transaction.id).filter(Transaction.organization_id.in_(org_ids), Transaction.type == "expense",
                                           Transaction.deleted_at.is_(None), Transaction.created_at >= start).first() is not None


def _purchases_ask(db: Session, site: Organization, now: datetime) -> list[str]:
    """17:00: «сегодня что-то покупали?» — только если за день ни одной закупки не внесено и
    никто не сказал «без закупок»; заодно — сколько черновиков ждёт одной кнопки.
    Всё внесено и черновиков нет — молчим."""
    from app.services import meals
    d = now.date()
    if now.hour != PURCHASE_HOUR or not meals.expected_today(db, site.id, d):
        return []
    key = f"purchases_ask:{site.id}:{d.isoformat()}"
    if _done(db, key):
        return []
    said_none = db.query(BotMessage.id).filter(BotMessage.kind == "no_purchases",
                                               BotMessage.job_key.like(f"no_purchases:{d.isoformat()}:%")).first()
    need_buy = not _bought_today(db, site, d) and said_none is None
    waiting = len(today.unchecked_receipts(db, site.id))
    debts_text, crossed = _debts_part(db, site, d)
    if not need_buy and not waiting and not debts_text and not meals.missing_today(db, site.id):
        db.add(BotMessage(kind="purchases_ask", job_key=key, status="skipped"))
        return []
    name = _counter_name(db, site)
    hi = f"{name}, " if name else ""
    parts = []
    if need_buy:
        parts.append(f"{hi}сегодня что-то покупали? Чеки фото сюда, или напишите «сегодня без закупок».")
    if waiting:
        parts.append(("И " if parts else hi) + f"в черновиках ждут {waiting} — проверить и записать: {public_url()}/new/receipts")
    if debts_text:
        parts.append(("" if parts else hi) + debts_text)
    if meals.missing_today(db, site.id):
        parts.append(("" if parts else hi) + "сколько сегодня ели — ещё не записано. Одной строкой: школа …, садик …, персонал …")
    for sid in crossed:
        db.add(BotMessage(kind="debt_old", job_key=f"debt_old:{site.id}:{sid}", status="logged"))
    send(db, group_chat_id(), "\n\n".join(parts), "purchases_ask", job_key=key)
    return [key]


def owner_evening_text(db: Session, site: Organization, d: date) -> str:
    """Одна строка владельцу (24.09: «сошлось / не сошлось: что»): сверки за день с разницей,
    что висит у людей, сколько заметок бот отложил."""
    from app.models import Reconciliation
    from app.services import bot_money
    from app.services.purchases import site_orgs as _orgs
    org_ids = [o.id for o in _orgs(db, site.id)]
    bad = []
    for r in (db.query(Reconciliation).filter(Reconciliation.organization_id.in_(org_ids), Reconciliation.date == d,
                                              Reconciliation.cancelled_at.is_(None)).all()):
        if abs(Decimal(r.delta or 0)) > 1:
            who = db.get(User, r.subject_id) if r.kind == "pocket" else None
            what = f"наличные {_first(who.name)}" if who else f"счёт {db.get(Organization, r.organization_id).name}"
            bad.append(f"{what} {_signed(Decimal(r.delta))}")
    waiting = len(today.unchecked_receipts(db, site.id))
    open_q = (db.query(BotMessage).filter(BotMessage.kind == bot_money.OFFER, BotMessage.status.in_(("sent", "paused")),
                                          BotMessage.created_at >= datetime.combine(d, datetime.min.time())).count())
    deferred = db.query(BotMessage).filter(BotMessage.kind == bot_money.OFFER, BotMessage.status == "deferred").count()
    head = f"{site.name}: " + ("не сошлось — " + "; ".join(bad) if bad else "сошлось")
    tail = []
    if waiting:
        tail.append(f"чеков на проверке {waiting}")
    if open_q:
        tail.append(f"без ответа {open_q}")
    if deferred:
        tail.append(f"ждёт проверки чеков {deferred}")
    notes = (db.query(BotMessage).filter(BotMessage.status == "noted",
                                         BotMessage.created_at >= datetime.combine(d, datetime.min.time())).count())
    if notes:
        tail.append(f"заметок бота {notes}: {public_url()}/new/settings/bot/chat")
    return head + (". " + ", ".join(tail) if tail else ".")


def _first(name: str) -> str:
    return (name or "").split()[0] if name else ""


def _signed(v: Decimal) -> str:
    return ("+" if v > 0 else "−") + fmt_money(float(abs(v)))


def _owner_evening(db: Session, site: Organization, now: datetime) -> list[str]:
    if now.hour != OWNER_EVENING_HOUR:
        return []
    d = now.date()
    key = f"owner_evening:{site.id}:{d.isoformat()}"
    if _done(db, key):
        return []
    owner = db.get(User, OWNER_USER_ID)
    send(db, owner.tg_id if owner else None, owner_evening_text(db, site, d), "owner_evening",
         user_id=OWNER_USER_ID, job_key=key)
    return [key]


def _debts_part(db: Session, site: Organization, d: date) -> tuple[str | None, list[int]]:
    """Долги поставщикам — Махабат раз в неделю (понедельник, в том же вечернем сообщении) и в
    день, когда долг перевалил за месяц (владелец 24.09: «сумма не маленькая»). Ответ
    «оплатила Мясо 30 000» бот превращает в запись сам."""
    debts = [x for x in today.supplier_debts(db, site.id) if x["debt"] > 0]
    if not debts:
        return None, []
    old_days = rules.debt_old_days(db)
    crossed = [x["id"] for x in debts if x["since"] and (d - x["since"]).days >= old_days
               and not _done(db, f"debt_old:{site.id}:{x['id']}")]
    if d.weekday() != 0 and not crossed:
        return None, []
    lines = [f"{x['name']} {fmt_money(float(x['debt']))}" + (f" (с {_d(x['since'])})" if x["since"] else "")
             for x in debts[:5]]
    return ("долги поставщикам: " + ", ".join(lines) + ". Если что-то уже оплатили — напишите «оплатила Мясо 30 000», запишу."), crossed


def _account_holder(db: Session, org: Organization) -> User | None:
    """Кто вносит остаток по счёту: у школы — директор, у садика — управляющая."""
    role = "director" if org.type == "school" else "manager"
    from app.services.purchases import site_orgs as _orgs
    return (db.query(User).filter(User.role == role, User.deleted_at.is_(None), User.tg_id.isnot(None))
            .order_by(User.id).first())


def _morning_checks(db: Session, site: Organization, now: datetime) -> list[str]:
    """Привыкание (до rules.daily_checks_until): в 9:00 лично каждому, у кого карман, —
    «по записям у вас X, верно?», и держателю счёта — «остаток на конец вчера?».
    Ошибку ловим на следующий день, а не ищем задним числом. Только в личку: суммы
    по людям и остатки счетов в общий чат не идут."""
    d = now.date()
    if now.hour != MORNING_HOUR or d > rules.daily_checks_until(db) or d.weekday() >= 5:
        return []
    out = []
    # Владелец 24.09 («слишком много вопросов»): карман — раз в неделю, в понедельник;
    # счёт — только если остаток не вносили неделю. Остальное люди присылают сами.
    for u in (cash.pocket_people(db, site.id) if d.weekday() == 0 else []):
        if not u.tg_id or u.role == "founder":
            continue
        key = f"pocket_ask:{d.isoformat()}:{u.id}"
        if _done(db, key):
            continue
        bal = cash.pocket_balance(db, site.id, u.id)
        send(db, u.tg_id, f"Доброе утро, {u.name}. По записям у вас на руках {fmt_money(float(bal))}. Верно? "
                          "Ответьте «да» или своей цифрой (можно с причиной: «5000, отдала за хлеб»).",
             "pocket_ask", user_id=u.id, job_key=key)
        out.append(key)
    y = d - timedelta(days=1)
    for a in cash.state(db, site.id)["accounts"]:
        holder = _account_holder(db, a["org"])
        if holder is None or (a.get("since") and (d - a["since"]).days < 7):
            continue
        key = f"bank_ask:{d.isoformat()}:{a['org'].id}"
        if _done(db, key):
            continue
        send(db, holder.tg_id, f"{holder.name}, остаток на счёте {a['org'].name} на конец вчерашнего дня ({_d(y)}) — "
                               "одной цифрой из банка, например «125 400».",
             "bank_ask", user_id=holder.id, job_key=key, payload={"org_id": a["org"].id, "date": y.isoformat()})
        out.append(key)
    return out


def morning_answer(db: Session, site: Organization, user: User, text: str) -> str | None:
    """Ответ на вопрос бота в личке: последний открытый вопрос этого человека — утренний
    (за сегодня) или «записываю …, верно?» о деньгах (шаг 2, 24.09)."""
    from app.services import bot_money
    d = date.today()
    ask = (db.query(BotMessage).filter(BotMessage.user_id == user.id, BotMessage.kind.in_(("pocket_ask", "bank_ask")),
                                       BotMessage.status.in_(("sent", "logged")),
                                       BotMessage.created_at >= datetime.combine(d, datetime.min.time()))
           .order_by(BotMessage.id.desc()).first())
    text = text or ""
    offer = bot_money.open_offer(db, user)
    if offer is not None and (ask is None or offer.id > ask.id):
        yes, no = _YES_REASON.match(text), _NO.match(text)
        if not (yes or no) and (offer.payload or {}).get("op") == "bank" and _COMMISSION.match(text):
            # «Комиссия» на подсказку «ответьте „да, комиссия“» (Мунара 24.09) — это «да» с причиной
            return bot_money.answer(db, site, user, offer, True, "комиссия банка")
        if not (yes or no):
            if text.strip().endswith("?"):
                # «По каким записям?» (Айжан 24.09): вопрос при открытом «верно?» — владельцу
                owner_copy(db, f"{user.name} на «{offer.text[:120]}…» спрашивает: «{text[:300]}»")
                return "Передал Абдусаттару, он ответит."
            # Не «да», не «нет», не вопрос (24.09: «Часть чека», «Овощи») — молчим, владельцу копия;
            # подсказка «если верна — да» после «Комиссия» вызвала «Нет» и потерянную запись
            owner_copy(db, f"{user.name} при открытом «{offer.text[:100]}…» пишет: «{text[:300]}»")
            return ""   # молчание: пустой ответ, дальше текст не разбираем
        return bot_money.answer(db, site, user, offer, bool(yes), yes.group(1).strip() if yes else None)
    if ask is None or not (_YES.match(text) or _NO.match(text) or _NUM.match(text)):
        return None
    if ask.kind == "pocket_ask":
        return _handle_pocket_answer(db, user, site, text)
    no = _NO.match(text)
    m = _NUM.match(no.group(1) if no else text)
    if not m:
        return "Нужна цифра из банка, например «125 400»."
    actual = parse_amount(m.group(1))
    reason = m.group(2).strip() or None
    p = ask.payload or {}
    try:
        cash.bank_balance(db, user=user, org_id=int(p["org_id"]), actual=actual,
                          d=date.fromisoformat(p["date"]), reason=reason)
    except ValueError as e:
        return f"{e}. Напишите ту же сумму и что произошло, например «{fmt_money(float(actual))} комиссия банка»."
    ask.status = "answered"
    return f"Записал остаток {fmt_money(float(actual))} на {_d(date.fromisoformat(p['date']))}. Спасибо!"
