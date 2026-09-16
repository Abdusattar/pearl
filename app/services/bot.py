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
from sqlalchemy.orm import Session

from app.models import BotMessage, Organization, Receipt, User
from app.services import cash, children, today
from app.services.kitchen import missing_days
from app.services.ocr import compute_hash
from app.services.price_check import fmt_money
from app.services.purchases import OPERATIONAL_ROLES, audit, site_orgs

TOKEN_ENV = "TELEGRAM_TOKEN"
GROUP_ENV = "TELEGRAM_GROUP_CHAT_ID"
REVIEW_ENV = "BOT_REVIEW_SUMMARY"      # «1» — сводка учредителям через проверку владельца (по умолчанию да)
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


def webhook_secret() -> str:
    t = token() or "no-token"
    return hashlib.sha256(f"pearl-bot:{t}".encode()).hexdigest()[:24]


# ── отправка ─────────────────────────────────────────────────────────────

def send(db: Session, chat_id: int | None, text: str, kind: str, *, user_id: int | None = None,
         job_key: str | None = None, status: str = "sent", payload: dict | None = None) -> BotMessage:
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
        r = httpx.post(f"https://api.telegram.org/bot{token()}/sendMessage",
                       json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, timeout=20)
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
        if s["since"] and (date.today() - s["since"]).days >= today.DEBT_OLD_DAYS:
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
            send(db, group_chat_id(), text or "Неделя началась. Сигналов нет: листы внесены, долги свежие.",
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
    # пятница 17:00 — карман каждому держателю в личку
    if now.weekday() == 4 and now.hour >= 17:
        for u in cash.pocket_people(db, site.id):
            if not u.tg_id:
                continue
            key = f"pocket:{d.isoformat()}:{u.id}"
            if _done(db, key):
                continue
            text, bal = pocket_text(db, site.id, u)
            send(db, u.tg_id, text, "pocket_ask", user_id=u.id, job_key=key, payload={"expected": float(bal)})
            sent.append(key)
    # каждый день 9:00 — пороги в группу (лист не вносился 3 дня, пересчёт висит)
    if now.hour >= 9 and now.weekday() != 0:
        key = f"group_threshold:{d.isoformat()}"
        if not _done(db, key):
            text = group_signals_text(db, site.id)
            if text:
                send(db, group_chat_id(), text, "group_threshold", job_key=key)
            else:
                db.add(BotMessage(kind="group_threshold", job_key=key, status="skipped"))
            sent.append(key)
    return sent


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
    db.add(BotMessage(kind="inbound", chat_id=chat_id, user_id=user.id if user else None, direction="in",
                      text=text[:2000], status="received", payload={"has_photo": bool(msg.get("photo"))}))
    db.flush()
    if not is_private:
        return None  # в группе бот только говорит; ответы — в личку
    if not user:
        reply = (f"Здравствуйте. Ваш номер в Telegram: {from_id}. Передайте его Абдусаттару, "
                 "он привяжет вас в системе, и я буду присылать вам ваши сообщения.")
        send(db, chat_id, reply, "reply")
        return reply
    site = site_for_bot(db)
    if msg.get("photo") and site is not None:
        reply = _handle_photo(db, msg, user, site, text)
        send(db, chat_id, reply, "reply", user_id=user.id)
        return reply
    low = text.lower()
    if low in ("/start", "start"):
        reply = f"Здравствуйте, {user.name}. Я буду присылать вам ваш карман по пятницам и сигналы. Фото чека или листа кухни можно отправить сюда."
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
    elif user.role in OPERATIONAL_ROLES and site is not None and (low in ("да", "верно", "+") or _NUM.match(text)):
        reply = _handle_pocket_answer(db, user, site, text)
    else:
        reply = "Понял. Сигналы и вопросы приходят сюда сами; фото чека или листа кухни можно прислать в любой момент."
    send(db, chat_id, reply, "reply", user_id=user.id)
    return reply


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
    if abs(delta) > cash.POCKET_DELTA_THRESHOLD and not reason:
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


def _handle_photo(db: Session, msg: dict, user: User, site: Organization, caption: str) -> str:
    """Фото в личку: скачать, положить в «Ждёт вас»: чек или лист кухни."""
    photo = sorted(msg["photo"], key=lambda p: p.get("file_size") or 0)[-1]
    data = download_file(photo["file_id"])
    if data is None:
        return "Не смог скачать фото. Попробуйте ещё раз."
    is_kitchen = bool(re.search(r"лист|кухн|повар", caption.lower()))
    h = compute_hash(data)
    month = datetime.now().strftime("%Y-%m")
    if is_kitchen:
        folder = MEDIA_ROOT / "kitchen" / "inbox"
        folder.mkdir(parents=True, exist_ok=True)
        fname = f"{date.today().isoformat()}_{h[:10]}.jpg"
        (folder / fname).write_bytes(data)
        db.add(BotMessage(kind="kitchen_photo", user_id=user.id, direction="in", status="pending",
                          payload={"path": f"kitchen/inbox/{fname}", "date": date.today().isoformat()}))
        return "Принял как лист кухни за сегодня. Разберу, проверите на ноутбуке: Лист кухни → фото от бота."
    existing = db.query(Receipt).filter(Receipt.file_hash == h).first()
    if existing:
        return f"Это фото уже есть: чек №{existing.id}. Проверить можно на ноутбуке в «Ждёт вас»."
    folder = MEDIA_ROOT / "receipts" / month
    folder.mkdir(parents=True, exist_ok=True)
    fname = f"{h[:12]}.jpg"
    (folder / fname).write_bytes(data)
    r = Receipt(organization_id=site.id, file_path=f"receipts/{month}/{fname}", file_hash=h, ocr_status="pending",
                created_by=user.id)
    db.add(r)
    db.flush()
    audit(db, "receipt", r.id, "insert", user.id, {"from": "bot"})
    return f"Принял. На ноутбуке в «Ждёт вас»: чек с фото, {_d(date.today())}, не проверен."


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
