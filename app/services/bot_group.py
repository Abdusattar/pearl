"""Бот в группе «Жемчужина», день 1: слушает и отвечает, ничего не записывает.

Проект `context/revision/09_bot.md`, решение владельца 21.09: сначала бот
только говорит, как понял сообщение и есть ли это в системе; записывать сам
начнёт, когда на живых чеках станет видно, как он распознаёт.

Фото: один вызов модели — вид бумаги (закуп, лист кухни, пересчёт, оплата
поставщику, услуга, банк, зарплата), поставщик, дата, итог. Вид важен до
сверки: чек за услугу на склад не идёт, оплата старого долга — не новый закуп.
Потом сверка по трём ключам (09_bot.md): тот же снимок → поставщик + дата +
сумма → сумма + дата.

Текст: без цифр — молчим, не о деньгах. С цифрами — модель: снятие, передача,
оплата поставщику, остаток банка; бот повторяет, как понял, и говорит, есть ли
такое в системе. Суммы по людям (карманы, зарплата) в группу не пишет.
"""
from __future__ import annotations

import base64
import os
import re
from datetime import date, timedelta
from decimal import Decimal

import httpx
from rapidfuzz import fuzz, process
from sqlalchemy.orm import Session

from app.models import (BotMessage, CashFunding, CashTransfer, KitchenSheet, Organization, Purchase, Receipt,
                        ReceiptTransaction, StockCount, Supplier, SupplierPayment, Transaction, User)
from app.services import cash
from app.services.ocr import _normalize_orientation
from app.services.price_check import fmt_money as _fmt
from app.services.purchases import site_orgs
from app.services.recognize import MODEL, OR_URL, _num, _parse_json

PURCHASE, KITCHEN, COUNT, SUPPLIER_PAY, SERVICE, BANK, SALARY, OTHER = (
    "purchase", "kitchen", "count", "supplier_payment", "service", "bank", "salary", "other")
WITHDRAWAL, TRANSFER, BALANCE = "withdrawal", "transfer", "balance"

MATCH_DAYS = 3          # чек и запись о нём расходятся по дате на день-два: внесли назавтра
OLD_DAYS = 3            # чек старше — говорим дату вслух (09_bot.md: «чек от 7 сентября»)
FAR_DAYS = 60           # дата старше — модель её выдумала или перепутала день с месяцем
AMOUNT_TOL = 0.01       # 1 %: модель читает итог почти всегда верно, но «705» и «706» — одно
SUPPLIER_SCORE = 80

MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]


def fmt_money(v) -> str:
    """В группе копейки не нужны: «13 501,65» читается хуже «13 502»."""
    return _fmt(float(round(Decimal(str(v)))))


def _signed(v) -> str:
    return ("+" if v > 0 else "−") + fmt_money(abs(v))


def _d(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def _dd(d: date) -> str:
    return d.strftime("%d.%m")


# ── модель ───────────────────────────────────────────────────────────────

PHOTO_PROMPT = (
    "The image was posted to the work chat of a private kindergarten in Kyrgyzstan (Russian/Kyrgyz, Cyrillic). "
    "Say what kind of paper it is and read its header. Kinds:\n"
    "- purchase: a receipt or invoice for GOODS bought (food, household goods, stationery) — lines with products, "
    "quantities AND prices or line sums; printed, handwritten товарный чек, накладная. A list with no prices and "
    "no sums is never a purchase.\n"
    "- kitchen: a handwritten kitchen sheet — what the cooks took for ONE day: small quantities (grams, a few kg, "
    "a few pieces, eggs by the dozen), no prices, usually a date on top.\n"
    "- count: a stock-count sheet — what is LEFT in the store room: large quantities (tens of kg, sacks like "
    "3×50кг, 16 шт × 5л), no prices, often check marks next to lines, may be titled остаток/пересчёт.\n"
    "- supplier_payment: proof of PAYING a supplier for earlier deliveries — a bank transfer screenshot "
    "(Optima, MBank, Элсом) or a handwritten расписка «получил(а) N сом», no product lines.\n"
    "- service: a receipt for a SERVICE or utility, not goods: repair, master, electricity, gas, water, internet, "
    "transport, rent.\n"
    "- bank: a banking app screenshot about the school's own account: account balance, cash withdrawal, "
    "tax/social fund payment (УГНС, Соцфонд), list of payments.\n"
    "- salary: a list of people with amounts paid to them (зарплата, аванс, ведомость).\n"
    "- other: anything else (people, food photos, documents without money).\n\n"
    "Return ONLY JSON: {\"kind\": one of the kinds above, "
    "\"supplier\": the SELLER / payee name as written or null — never «Жемчужина», that is the buyer (us); "
    "a generic printed header like «Товарный чек» is not a name, "
    "\"date\": \"YYYY-MM-DD\" or null (only if a date is written; if the year is missing, leave it as \"MM-DD\"), "
    "\"amount\": the grand total / payment / withdrawal amount as a number or null, "
    "\"balance\": account balance shown on a bank screenshot or null, "
    "\"bank_op\": for kind=bank: \"balance\" | \"withdrawal\" | \"tax\" | \"payments\" | null, "
    "\"payments\": for kind=bank with several payments: [numbers] or [], "
    "\"sure\": true if you are confident about kind and amount, else false}"
)

TEXT_PROMPT = (
    "A message from the work chat of a kindergarten in Kyrgyzstan (Russian, may mix Kyrgyz). People there: "
    "{people}. Suppliers: {suppliers}.\n"
    "Message from {author}: «{text}»\n\n"
    "Is it a report about money? Kinds:\n"
    "- withdrawal: someone took cash from the bank account (сняла, сняли с карты/счёта).\n"
    "- transfer: cash passed from one person to another (передала, отдала, получила от).\n"
    "- supplier_payment: a supplier was paid for goods (оплатила Халиме, отдала долг Кириллу).\n"
    "- balance: the bank account balance now (на счету, остаток).\n"
    "- none: anything else, including questions, greetings, plans, children, food.\n"
    "Names: return them as written. If the person is not named, null — do not guess.\n"
    "Return ONLY JSON: {{\"kind\": ..., \"amount\": number or null, \"who\": person who did it or null, "
    "\"to\": receiving person for transfer or null, \"supplier\": supplier or null, "
    "\"date\": \"today\" | \"yesterday\" | \"YYYY-MM-DD\" | null, \"account\": \"садик\" | \"школа\" | null, "
    "\"sure\": true|false}}"
)


VOICE_PROMPT = (
    "Это голосовое сообщение из рабочего чата садика и школы в Кыргызстане. Говорят по-русски, "
    "могут вставлять кыргызские фразы. Расшифруй дословно на русском; кыргызские фразы оставь как "
    "сказаны и дай перевод в квадратных скобках. Верни только текст расшифровки, без заголовков и выводов."
)


def _call(content: list[dict], max_tokens: int = 1024, timeout: int = 60) -> str:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY не задан")
    resp = httpx.post(OR_URL, json={"model": MODEL, "messages": [{"role": "user", "content": content}],
                                    "max_tokens": max_tokens, "temperature": 0.1},
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                               "HTTP-Referer": "https://pearl.local", "X-Title": "Pearl group bot"},
                      timeout=timeout)
    resp.raise_for_status()
    return (resp.json()["choices"][0]["message"]["content"] or "").strip()


def ask_model(prompt: str, image: bytes | None = None, mime: str = "image/jpeg", pdf: bytes | None = None) -> dict:
    content: list[dict] = []
    if image is not None:
        b64 = base64.b64encode(_normalize_orientation(image)).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    if pdf is not None:
        # PDF из чата (платёжки Айжан из банка, счета-фактуры, 23.09) — модель читает файл целиком
        b64 = base64.b64encode(pdf).decode()
        content.append({"type": "file", "file": {"filename": "doc.pdf", "file_data": f"data:application/pdf;base64,{b64}"}})
    content.append({"type": "text", "text": prompt})
    return _parse_json(_call(content))


def transcribe(audio: bytes, fmt: str = "ogg") -> str:
    """Голосовое из чата → текст (23.09, владелец: «бот должен сохранять голосовые»).
    Та же модель, что читает чеки; проверено на голосовом Айжан 1:20 — читает и кыргызский."""
    b64 = base64.b64encode(audio).decode()
    return _call([{"type": "input_audio", "input_audio": {"data": b64, "format": fmt}},
                  {"type": "text", "text": VOICE_PROMPT}], max_tokens=2048, timeout=180)


def _date(v, today: date) -> date | None:
    """«2026-09-17», «09-17» (год не написан), «today», «yesterday»."""
    if not v:
        return None
    s = str(v).strip().lower()
    if s == "today":
        return today
    if s == "yesterday":
        return today - timedelta(days=1)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", s)
        if m:
            d = date(today.year, int(m[1]), int(m[2]))
            return d if d <= today + timedelta(days=1) else date(today.year - 1, d.month, d.day)
    except ValueError:
        return None
    return None


def read_photo(image: bytes | None, today: date, mime: str = "image/jpeg", pdf: bytes | None = None) -> dict:
    data = ask_model(PHOTO_PROMPT, image, mime) if pdf is None else ask_model(PHOTO_PROMPT, None, mime, pdf=pdf)
    kind = data.get("kind") if data.get("kind") in (PURCHASE, KITCHEN, COUNT, SUPPLIER_PAY, SERVICE, BANK,
                                                     SALARY, OTHER) else OTHER
    d = _date(data.get("date"), today)
    if d is not None and not (today - timedelta(days=FAR_DAYS) <= d <= today + timedelta(days=1)):
        d = None   # «1 января» на сентябрьском чеке, 09.11 вместо 11.09: дату не выдумываем
    return {"kind": kind, "supplier": (data.get("supplier") or "").strip() or None, "date": d,
            "amount": _num(data.get("amount")), "balance": _num(data.get("balance")),
            "bank_op": data.get("bank_op"), "payments": [x for x in (_num(p) for p in data.get("payments") or []) if x],
            "sure": bool(data.get("sure"))}


_HAS_NUMBER = re.compile(r"\d[\d\s]{2,}|\d+\s*(тыс|к\b|000)")


def worth_reading(text: str) -> bool:
    """Про деньги пишут цифрами. Без числа от трёх знаков — не наше, модель не зовём."""
    return bool(_HAS_NUMBER.search(text or ""))


def read_text(db: Session, text: str, author: User | None, today: date) -> dict:
    people = [u.name for u in db.query(User).filter(User.deleted_at.is_(None)).all()]
    suppliers = [s.name for s in db.query(Supplier).all()]
    data = ask_model(TEXT_PROMPT.format(people=", ".join(people), suppliers=", ".join(suppliers[:80]),
                                        author=author.name if author else "неизвестный", text=text[:600]))
    kind = data.get("kind") if data.get("kind") in (WITHDRAWAL, TRANSFER, SUPPLIER_PAY, BALANCE) else None
    return {"kind": kind, "amount": _num(data.get("amount")), "who": data.get("who"), "to": data.get("to"),
            "supplier": data.get("supplier"), "date": _date(data.get("date"), today) or today,
            "account": data.get("account"), "sure": bool(data.get("sure"))}


# ── справочники ──────────────────────────────────────────────────────────

def find_supplier(db: Session, name: str | None) -> Supplier | None:
    if not name:
        return None
    rows = db.query(Supplier).all()
    choices = {s.id: s.name for s in rows}
    hit = process.extractOne(name, choices, scorer=fuzz.WRatio)
    if hit and hit[1] >= SUPPLIER_SCORE:
        return next(s for s in rows if s.id == hit[2])
    return None


def find_person(db: Session, name: str | None) -> User | None:
    """«Мунара эже», «Махабатка» → человек. Сравниваем с именем без фамилии."""
    if not name:
        return None
    users = db.query(User).filter(User.deleted_at.is_(None)).all()
    first = {u.id: u.name.split()[0] for u in users}
    word = str(name).split()[0]
    hit = process.extractOne(word, first, scorer=fuzz.WRatio)
    if hit and hit[1] >= SUPPLIER_SCORE:
        return next(u for u in users if u.id == hit[2])
    return None


def _first(name: str) -> str:
    return name.split()[0]


# ── сверка с системой ────────────────────────────────────────────────────

def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(1.0, AMOUNT_TOL * abs(b))


def _expense_groups(db: Session, site_org_id: int, d: date, days: int = MATCH_DAYS) -> list[dict]:
    """Расходы площадки около даты, одной записью на покупку: покупка нового входа
    — по шапке, старого — по квитанции, остальное по проводке."""
    org_ids = [o.id for o in site_orgs(db, site_org_id)] + [site_org_id]
    txs = (db.query(Transaction)
           .filter(Transaction.organization_id.in_(org_ids), Transaction.type == "expense",
                   Transaction.deleted_at.is_(None),
                   Transaction.date >= d - timedelta(days=days), Transaction.date <= d + timedelta(days=days))
           .all())
    if not txs:
        return []
    by_receipt = dict(db.query(ReceiptTransaction.transaction_id, ReceiptTransaction.receipt_id)
                      .filter(ReceiptTransaction.transaction_id.in_([t.id for t in txs])).all())
    groups: dict = {}
    for t in txs:
        key = ("p", t.purchase_id) if t.purchase_id else (("r", by_receipt[t.id]) if t.id in by_receipt else ("t", t.id))
        g = groups.setdefault(key, {"date": t.date, "total": 0.0, "supplier_id": t.supplier_id,
                                    "purchase_id": t.purchase_id, "tx_id": t.id, "text": t.description})
        g["total"] += float(t.amount)
    return list(groups.values())


def _supplier_name(db: Session, supplier_id: int | None) -> str | None:
    s = db.get(Supplier, supplier_id) if supplier_id else None
    return s.name if s else None


def match_expense(db: Session, site_org_id: int, info: dict, supplier: Supplier | None, today: date) -> dict:
    """Закуп или услуга: поставщик + дата + сумма, потом сумма + дата."""
    amount = info.get("amount")
    if not amount:
        return {"status": "no_amount"}
    d = info.get("date") or today
    groups = _expense_groups(db, site_org_id, d)
    if supplier is not None:
        for g in groups:
            if g["supplier_id"] == supplier.id and _close(g["total"], amount):
                return {"status": "found", "date": g["date"], "total": g["total"], "supplier": supplier.name}
    near = [g for g in groups if _close(g["total"], amount) and abs((g["date"] - d).days) <= 1]
    if near:
        g = near[0]
        return {"status": "similar", "date": g["date"], "total": g["total"],
                "supplier": _supplier_name(db, g["supplier_id"]) or (g["text"] or "без поставщика")}
    return {"status": "missing"}


def match_supplier_payment(db: Session, site_org_id: int, amount: float | None, supplier: Supplier | None,
                           d: date) -> dict:
    if not amount:
        return {"status": "no_amount"}
    q = db.query(SupplierPayment).filter(SupplierPayment.deleted_at.is_(None),
                                         SupplierPayment.date >= d - timedelta(days=MATCH_DAYS),
                                         SupplierPayment.date <= d + timedelta(days=MATCH_DAYS))
    if supplier is not None:
        q = q.filter(SupplierPayment.supplier_id == supplier.id)
    for p in q.all():
        if _close(float(p.amount), amount):
            return {"status": "found", "date": p.date, "total": float(p.amount), "supplier": p.supplier.name}
    return {"status": "missing"}


def match_withdrawal(db: Session, site_org_id: int, amount: float | None, d: date) -> dict:
    if not amount:
        return {"status": "no_amount"}
    rows = (db.query(CashFunding)
            .filter(CashFunding.organization_id == site_org_id, CashFunding.source_type == "withdrawal",
                    CashFunding.deleted_at.is_(None),
                    CashFunding.date >= d - timedelta(days=7), CashFunding.date <= d + timedelta(days=7))
            .order_by(CashFunding.date).all())
    for f in rows:
        if _close(float(f.amount), amount):
            return {"status": "found", "date": f.date, "total": float(f.amount)}
    return {"status": "missing"}


def match_transfer(db: Session, site_org_id: int, amount: float | None, d: date) -> dict:
    if not amount:
        return {"status": "no_amount"}
    rows = (db.query(CashTransfer)
            .filter(CashTransfer.site_org_id == site_org_id, CashTransfer.deleted_at.is_(None),
                    CashTransfer.date >= d - timedelta(days=MATCH_DAYS), CashTransfer.date <= d + timedelta(days=MATCH_DAYS))
            .all())
    for t in rows:
        if _close(float(t.amount), amount):
            return {"status": "found", "date": t.date, "total": float(t.amount)}
    return {"status": "missing"}


def match_account_payments(db: Session, site_org_id: int, total: float, d: date) -> dict:
    """Платежи со счёта (налог, соцфонд, перевод): их вносят по человеку или по
    статье, а в банке они одним скрином — сверяем сумму за день, не проводку."""
    org_ids = [o.id for o in site_orgs(db, site_org_id)] + [site_org_id]
    rows = (db.query(Transaction.date, Transaction.amount)
            .filter(Transaction.organization_id.in_(org_ids), Transaction.type == "expense",
                    Transaction.deleted_at.is_(None), Transaction.paid_directly.is_(True),
                    Transaction.date >= d - timedelta(days=MATCH_DAYS), Transaction.date <= d + timedelta(days=MATCH_DAYS))
            .all())
    by_day: dict[date, float] = {}
    for day, amount in rows:
        by_day[day] = by_day.get(day, 0.0) + float(amount)
        if _close(float(amount), total):
            return {"status": "found", "date": day}
    for day, s in sorted(by_day.items()):
        if _close(s, total):
            return {"status": "found", "date": day}
    return {"status": "missing"}


def apply_caption(info: dict, caption: str) -> dict:
    """Подпись под фото сильнее догадки модели: «кухня», «остаток», «оплата»."""
    low = (caption or "").lower()
    for pattern, kind in ((r"кухн|повар", KITCHEN), (r"остат|пересч", COUNT), (r"оплат|расписк|долг", SUPPLIER_PAY)):
        if re.search(pattern, low):
            return {**info, "kind": kind, "sure": True}
    return info


def seen_photo(db: Session, file_unique_id: str | None, file_hash: str | None) -> dict | None:
    """Тот же снимок: уже присылали в группу (Telegram даёт одному фото один
    file_unique_id) или уже есть квитанция с тем же отпечатком."""
    if file_unique_id:
        prev = (db.query(BotMessage).filter(BotMessage.kind == "group_photo",
                                            BotMessage.payload["file_unique_id"].astext == file_unique_id)
                .order_by(BotMessage.id).first())
        if prev is not None:
            return {"date": prev.created_at.date() if prev.created_at else None, "receipt_id": None}
    if file_hash:
        r = db.query(Receipt).filter(Receipt.file_hash == file_hash).first()
        if r is not None:
            return {"date": r.created_at.date() if r.created_at else None, "receipt_id": r.id}
    return None


# ── ответы ───────────────────────────────────────────────────────────────

def _when(d: date | None, today: date) -> str:
    if d is None:
        return ""
    if (today - d).days > OLD_DAYS:
        return f", от {_d(d)}"
    return f", {_dd(d)}"


def photo_reply(db: Session, site: Organization, author: User | None, info: dict, today: date,
                dup: dict | None = None) -> str | None:
    kind = info["kind"]
    if dup is not None:
        when = f" {_d(dup['date'])}" if dup.get("date") else ""
        num = f", чек № {dup['receipt_id']}" if dup.get("receipt_id") else ""
        return f"Это фото уже присылали{when}{num}. Второй раз не вносите."
    if kind == OTHER:
        return None
    amount = info.get("amount")
    money = f", {fmt_money(amount)}" if amount else ""
    when = _when(info.get("date"), today)
    if kind in (PURCHASE, SERVICE):
        supplier = find_supplier(db, info.get("supplier"))
        who = supplier.name if supplier else (info.get("supplier") or "поставщик не читается")
        label = "Закуп" if kind == PURCHASE else "Услуга, не на склад"
        head = f"{label}: {who}{when}{money}"
        if not amount:
            # список без итога чаще лист кухни или остаток, чем чек: не угадываем
            return (head + ". Итог не читается — сверить не могу. Если это лист кухни или остаток — "
                    "подпишите фото словом «кухня» или «остаток».")
        m = match_expense(db, site.id, info, supplier, today)
        if m["status"] == "found":
            return head + " — в системе есть."
        if m["status"] == "similar":
            return head + f" — похоже на запись {m['supplier']} {_dd(m['date'])} на {fmt_money(m['total'])}. Это она?"
        tail = " — в системе нет."
        if supplier is None and info.get("supplier"):
            tail += f" Поставщика «{info['supplier']}» в справочнике нет."
        return head + tail
    if kind == SUPPLIER_PAY:
        supplier = find_supplier(db, info.get("supplier"))
        who = supplier.name if supplier else (info.get("supplier") or "кому — не читается")
        head = f"Оплата поставщику: {who}{when}{money}. Это долг за прошлые привозы, не новый закуп"
        m = match_supplier_payment(db, site.id, amount, supplier, info.get("date") or today)
        if m["status"] == "found":
            return head + " — в системе есть."
        if m["status"] == "no_amount":
            return head + ". Сумма не читается."
        return head + " — в системе нет."
    if kind in (KITCHEN, COUNT) and not info.get("sure"):
        return "Лист без цен. Это лист кухни или пересчёт склада? Подпишите фото словом «кухня» или «остаток»."
    if kind == KITCHEN:
        if info.get("date") is None:
            # Махабат переносит листы пачками — «сегодня» тут догадка, а не факт
            return "Лист кухни, дата на листе не читается. Внести: Лист кухни на ноутбуке, выбрать день."
        d = info["date"]
        have = (db.query(KitchenSheet).filter(KitchenSheet.site_org_id == site.id, KitchenSheet.date == d,
                                              KitchenSheet.deleted_at.is_(None)).first())
        if have is not None:
            return f"Лист кухни за {_dd(d)} — уже внесён."
        return f"Лист кухни за {_dd(d)} — в системе нет. Внести: Лист кухни на ноутбуке."
    if kind == COUNT:
        d = info.get("date") or today
        org_ids = [o.id for o in site_orgs(db, site.id)] + [site.id]
        have = (db.query(StockCount).filter(StockCount.organization_id.in_(org_ids), StockCount.count_date == d,
                                            StockCount.status == "applied").first())
        if have is not None:
            return f"Пересчёт склада за {_dd(d)} — уже внесён."
        return f"Лист пересчёта склада за {_dd(d)}. Внести: Склад → Пересчёт."
    if kind == BANK:
        return bank_reply(db, site, author, info, today)
    if kind == SALARY:
        return "Список зарплаты. Суммы по людям в группе не разбираю — пришлите мне в личку, сверю с ведомостью."
    return None


def _account_org(db: Session, site: Organization, author: User | None) -> Organization:
    """Чей счёт: по объекту человека; Айжан и владелец видят оба — берём площадку."""
    if author is not None and author.organization_id:
        org = db.get(Organization, author.organization_id)
        if org is not None and org.site_org_id == site.id:
            return org
    return site


def bank_reply(db: Session, site: Organization, author: User | None, info: dict, today: date) -> str | None:
    op = info.get("bank_op")
    acc = _account_org(db, site, author)
    label = "садика" if acc.type == "kindergarten" else ("школы" if acc.type == "school" else acc.name)
    if op == "balance" or (info.get("balance") and op is None):
        bal = info.get("balance") or info.get("amount")
        if not bal:
            return None
        expected = next((a["expected"] for a in cash.accounts(db, site.id) if a["org"].id == acc.id), None)
        head = f"Остаток счёта {label} {fmt_money(bal)} на {_dd(info.get('date') or today)}"
        if expected is None:
            return head + "."
        delta = Decimal(str(bal)) - Decimal(expected)
        if abs(delta) <= 1:
            return head + " — с записями сходится."
        return head + f". По записям {fmt_money(expected)}, разница {_signed(delta)}."
    if op == "withdrawal":
        m = match_withdrawal(db, site.id, info.get("amount"), info.get("date") or today)
        head = f"Снятие со счёта {label}{_when(info.get('date'), today)}, {fmt_money(info['amount'] or 0)}"
        return head + (f" — в системе есть ({_dd(m['date'])})." if m["status"] == "found" else " — в системе нет.")
    if op in ("tax", "payments"):
        pays = info.get("payments") or ([info["amount"]] if info.get("amount") else [])
        if not pays:
            return None
        total = sum(pays)
        what = "Платёж" if len(pays) == 1 else f"{len(pays)} платежа"
        head = f"{what} со счёта {label} на {fmt_money(total)}" + (" (налог / соцфонд)" if op == "tax" else "")
        m = match_account_payments(db, site.id, total, info.get("date") or today)
        if m["status"] == "found":
            return head + f" — в системе есть ({_dd(m['date'])})."
        return head + " — в системе нет."
    return None


def text_reply(db: Session, site: Organization, author: User | None, info: dict, today: date) -> str | None:
    kind, amount = info.get("kind"), info.get("amount")
    if kind is None or not amount:
        return None
    d = info["date"]
    day = "сегодня" if d == today else ("вчера" if d == today - timedelta(days=1) else _d(d))
    money = fmt_money(amount)
    if kind == WITHDRAWAL:
        who = find_person(db, info.get("who")) or author
        acc = info.get("account")
        acc_txt = f" со счёта {'школы' if acc == 'школа' else 'садика'}" if acc else ""
        head = f"Понял как снятие {money}{acc_txt}, {day}" + (f", карман: {_first(who.name)}" if who else "")
        m = match_withdrawal(db, site.id, amount, d)
        if m["status"] == "found":
            return head + f". Такое снятие уже записано {_dd(m['date'])} — это оно или второе?"
        return head + ". В системе нет."
    if kind == TRANSFER:
        giver = find_person(db, info.get("who"))
        taker = find_person(db, info.get("to"))
        if giver is None and taker is not None and author is not None and author.id != taker.id:
            giver = author
        if taker is None and giver is not None and author is not None and author.id != giver.id:
            taker = author
        pair = f"{_first(giver.name) if giver else '?'} → {_first(taker.name) if taker else '?'}"
        head = f"Понял как передачу {pair} {money}, {day}"
        m = match_transfer(db, site.id, amount, d)
        return head + (" — в системе есть." if m["status"] == "found" else ". В системе нет.")
    if kind == SUPPLIER_PAY:
        supplier = find_supplier(db, info.get("supplier"))
        name = supplier.name if supplier else (info.get("supplier") or "поставщику")
        head = f"Понял как оплату {name} {money}, {day}"
        m = match_supplier_payment(db, site.id, amount, supplier, d)
        tail = " — в системе есть." if m["status"] == "found" else ". В системе нет."
        if supplier is None and info.get("supplier"):
            tail += f" Поставщика «{info['supplier']}» в справочнике нет."
        return head + tail
    if kind == BALANCE:
        return bank_reply(db, site, author, {"bank_op": "balance", "balance": amount, "date": d}, today)
    return None
