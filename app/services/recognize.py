"""Единый конвейер распознавания фото (решение владельца 16.09): чек поставщика,
лист кухни, лист пересчёта — один разбор, разница только в контексте.

Что изменилось против старого `ocr.py` + `normalize.py` (два вызова, второй без
картинки и без цен, никакой проверки чисел):

1. Один вызов модели, и ей даётся список ожидаемых товаров с единицей и
   обычной ценой: у чека — что этот поставщик привозил, у листа кухни — что
   есть на складе и вчерашний лист, у пересчёта — весь основной склад. Чтение
   почерка превращается в выбор из 20–60 вариантов: «камуста» → капуста,
   «duyo по 12» → яйцо.
2. Детерминированная проверка чисел: количество × цена = сумма; при
   расхождении пробуется сдвиг запятой и обмен местами, выбирается вариант,
   который сходится и ближе к обычной цене.
3. Единицы: «200 г» у товара в кг переводится в 0,2; «2 лотка» у яйца в
   штуки по фасовке карточки.
4. Нечёткий поиск по каталогу как запасной путь, если модель не выбрала
   товар из списка.

Ничего не проводится само: результат — строки формы с вопросами, человек
проверяет и записывает.
"""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import date, timedelta
from decimal import Decimal

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import KitchenSheet, Product, ProductCategory, Transaction, WarehouseReceipt, WriteOff
from app.services.ocr import _normalize_orientation
from app.services.price_check import usual_price
from app.services.products import FUZZY_THRESHOLD, rank_candidates
from app.services.purchases import site_orgs
from app.services.warehouse import get_balance_map

RECEIPT, KITCHEN, COUNT, TRANSFER = "receipt", "kitchen", "count", "transfer"
SURE_SCORE = 90   # нечёткое совпадение ниже — вопрос человеку, не подстановка

OR_URL = "https://openrouter.ai/api/v1/chat/completions"
# 16.09: на 28 чеках прода flash даёт 81 % товаров и ~72 % чисел против 76 % и
# ~65 % у flash-lite (старый разбор — 51 % и ~45 %); на листе кухни lite читал
# латиницу и мусор, flash — все 17 строк. Цена вопроса — доли цента за фото.
DEFAULT_MODEL = "google/gemini-2.5-flash"
MODEL = os.getenv("OCR_MODEL", DEFAULT_MODEL)

SUB_UNITS = {"кг": {"г": 1000, "гр": 1000, "грамм": 1000, "г.": 1000}, "л": {"мл": 1000}}
UNIT_ALIASES = {"kg": "кг", "кг.": "кг", "килограмм": "кг", "литр": "л", "л.": "л", "шт.": "шт", "штук": "шт",
                "пач": "пач", "пачка": "пач", "уп.": "уп", "упак": "уп", "пуч": "пучок", "пучок": "пучок",
                "бут.": "бут", "бутылка": "бут", "меш": "мешок", "мешок": "мешок", "лоток": "лоток", "лотка": "лоток"}


# ── контекст: ожидаемые товары ────────────────────────────────────────────

def _cand(db: Session, p: Product, usual: float | None = None, balance: float | None = None) -> dict:
    # слова, которыми этот товар уже называли и человек подтвердил (23.09)
    words = [a.raw_text for a in (p.aliases or [])][:4]
    return {"id": p.id, "name": p.name, "unit": p.unit or "", "usual": usual, "aliases": words,
            "pack_name": p.pack_name if p.pack_qty else None, "pack_qty": float(p.pack_qty) if p.pack_qty else None,
            "minor": bool(p.product_category and p.product_category.is_minor), "balance": balance}


def _active(db: Session, ids: list[int]) -> dict[int, Product]:
    if not ids:
        return {}
    rows = db.query(Product).filter(Product.id.in_(ids)).all()
    out = {}
    for p in rows:
        while p.merged_into_id is not None:
            p = p.merged_into
        if p.retired_at is None:
            out[p.id] = p
    return out


def receipt_context(db: Session, site_org_id: int, supplier_id: int | None, limit: int = 60) -> list[dict]:
    """Что этот поставщик привозил (90 дней), потом что покупали вообще."""
    org_ids = [o.id for o in site_orgs(db, site_org_id)] + [site_org_id]
    since = date.today() - timedelta(days=90)
    q = (db.query(WarehouseReceipt.product_id, func.count(WarehouseReceipt.id))
         .join(Transaction, Transaction.id == WarehouseReceipt.transaction_id)
         .filter(WarehouseReceipt.deleted_at.is_(None), WarehouseReceipt.date >= since,
                 WarehouseReceipt.organization_id.in_(org_ids)))
    ordered: list[int] = []
    if supplier_id:
        for pid, _ in (q.filter(Transaction.supplier_id == supplier_id)
                       .group_by(WarehouseReceipt.product_id).order_by(func.count(WarehouseReceipt.id).desc()).all()):
            ordered.append(pid)
    for pid, _ in q.group_by(WarehouseReceipt.product_id).order_by(func.count(WarehouseReceipt.id).desc()).limit(limit).all():
        if pid not in ordered:
            ordered.append(pid)
    products = _active(db, ordered[:limit])
    out, seen = [], set()
    for pid in ordered:
        p = products.get(pid)
        if p is None or p.id in seen:
            continue
        seen.add(p.id)
        out.append(_cand(db, p, usual_price(db, p.id)))
    return out


def kitchen_context(db: Session, site_org_id: int, limit: int = 80) -> list[dict]:
    """Вчерашний лист + основные товары с остатком."""
    ordered: list[int] = []
    last = (db.query(KitchenSheet).filter(KitchenSheet.site_org_id == site_org_id, KitchenSheet.deleted_at.is_(None))
            .order_by(KitchenSheet.date.desc()).first())
    if last:
        ordered += [w.product_id for w in db.query(WriteOff).filter(WriteOff.sheet_id == last.id, WriteOff.deleted_at.is_(None)).all()]
    org_ids = {o.id for o in site_orgs(db, site_org_id)} | {site_org_id}
    balances = get_balance_map(db, org_ids)
    stock_ids = [pid for pid, v in sorted(balances.items(), key=lambda kv: -kv[1]["balance"]) if v["balance"] > 0]
    ordered += [pid for pid in stock_ids if pid not in ordered]
    products = _active(db, ordered[:limit * 2])
    out, seen = [], set()
    for pid in ordered:
        p = products.get(pid)
        if p is None or p.id in seen:
            continue
        seen.add(p.id)
        out.append(_cand(db, p, None, balances.get(p.id, {}).get("balance")))
        if len(out) >= limit:
            break
    # мелочь, которую пишут на листе (зелень, специи), тоже нужна в списке
    minor = (db.query(Product).join(ProductCategory, ProductCategory.id == Product.category_id)
             .filter(ProductCategory.level == "minor", Product.merged_into_id.is_(None), Product.retired_at.is_(None),
                     ProductCategory.name.in_(("зелень", "специи"))).all())
    for p in minor:
        if p.id not in seen:
            out.append(_cand(db, p))
    return out


def count_context(db: Session, site_org_id: int) -> list[dict]:
    org_ids = {o.id for o in site_orgs(db, site_org_id)} | {site_org_id}
    balances = get_balance_map(db, org_ids)
    rows = (db.query(Product).join(ProductCategory, ProductCategory.id == Product.category_id)
            .filter(ProductCategory.level == "stock", Product.merged_into_id.is_(None), Product.retired_at.is_(None))
            .order_by(Product.name).all())
    return [_cand(db, p, None, balances.get(p.id, {}).get("balance")) for p in rows]


# ── вызов модели ─────────────────────────────────────────────────────────

def _prompt(kind: str, candidates: list[dict], text: str | None = None) -> str:
    lines = []
    for c in candidates:
        extra = []
        if c.get("usual"):
            extra.append(f"обычно {c['usual']:g} сом/{c['unit']}")
        if c.get("pack_name"):
            extra.append(f"{c['pack_name']} = {c['pack_qty']:g} {c['unit']}")
        if c.get("aliases"):
            extra.append("also written as: " + ", ".join(c["aliases"]))
        lines.append(f"{c['id']}|{c['name']}|{c['unit']}" + (("|" + ", ".join(extra)) if extra else ""))
    catalog = "\n".join(lines) or "(список пуст)"
    what = {
        RECEIPT: ("a handwritten or printed purchase receipt from Kyrgyzstan (Russian/Kyrgyz, Cyrillic). "
                  "Each line: product, quantity, unit price, line total; the final total may be written below."),
        KITCHEN: ("a handwritten kitchen sheet: what the cooks took from the store for one day. "
                  "Each line: product and quantity with a unit (кг, г, л, мл, шт, пучок). No prices. "
                  "A line may contain several takes joined by '+' (e.g. '500г + 3,500'): return the sum in one unit."),
        COUNT: ("a stock count: what is on the shelf now, product and counted quantity with a unit. No prices. "
                "A line may hold arithmetic ('25кг + 5кг = 30кг', '750*7=5,250гр', '12 бут × 5л = 60л'): return the final "
                "result with its unit. Numbers like '7,700' or '5,250' with no unit are decimals in the card unit (7.7)."),
        TRANSFER: ("a message: products given away to another kindergarten branch, product and quantity with a unit. "
                   "Ignore words about who or where; one object per product."),
    }[kind]
    numbers = ('"qty": number or null, "unit": unit exactly as written (кг, г, л, мл, шт, лоток, мешок, пучок…) or null, '
               '"price": unit price or null, "total": line total or null')
    src = f"The text below is {what}\n\nTEXT:\n{text}\n\n" if text is not None else f"The image is {what}\n\n"
    return (
        src +
        "EXPECTED PRODUCTS (id|name|unit|notes). The writer almost always means one of these; names may be misspelled, "
        "abbreviated, in Kyrgyz (жумуртка=яйцо, сабиз=морковь, пияз=лук, картошка/картош=картофель) or partly illegible. "
        "Match by meaning and by the unit/price notes. If a line clearly is NOT any of them, use product_id null and copy the text.\n"
        f"{catalog}\n\n"
        "Rules:\n"
        "- One JSON object per written line, in reading order. Names and their numbers may sit in separate columns: pair by row, never shift.\n"
        "- Never put digits or arithmetic into \"raw\". Ignore totals, VAT, phone numbers, dates, bleed-through text.\n"
        "- Copy the written text into \"raw\" in Cyrillic exactly as written (never Latin letters).\n"
        "- If quantity looks like a decimal shift (12.61 for a 126,1 kg potato line whose total is 4161), prefer the reading where qty × price = total.\n"
        "- Do not invent numbers: null when not written.\n\n"
        'Return ONLY JSON: {"amount": <final total or null>, "lines": [{"raw": "...", "product_id": <id or null>, '
        f'{numbers}}}]}}'
    )


def call_model(image_bytes: bytes | None, kind: str, candidates: list[dict], mime: str = "image/jpeg",
               model: str | None = None, text: str | None = None) -> dict:
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY не задан")
    content = []
    if image_bytes is not None:
        b64 = base64.b64encode(_normalize_orientation(image_bytes)).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    content.append({"type": "text", "text": _prompt(kind, candidates, text)})
    payload = {
        "model": model or MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 4096, "temperature": 0.1,
    }
    resp = httpx.post(OR_URL, json=payload, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                                     "HTTP-Referer": "https://pearl.local", "X-Title": "Pearl recognize"},
                      timeout=90)
    resp.raise_for_status()
    text = (resp.json()["choices"][0]["message"]["content"] or "").strip()
    data = _parse_json(text)
    return {"amount": _num(data.get("amount")), "lines": data.get("lines") or [], "raw": text,
            "usage": resp.json().get("usage", {})}


def _parse_json(text: str) -> dict:
    """JSON из ответа модели: снимает ```-обёртку, берёт первый объект, терпит
    висячие запятые и одинарные кавычки — модели иногда так отвечают, а
    один кривой ответ не должен ронять разбор."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        raise ValueError("В ответе нет JSON")
    body = text[start:end + 1] if end > start else text[start:]
    for attempt in (body, re.sub(r",\s*([}\]])", r"\1", body), re.sub(r",\s*([}\]])", r"\1", body).replace("'", '"')):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue
    data, _ = json.JSONDecoder().raw_decode(text, start)
    return data


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        return None


# ── проверки после модели ────────────────────────────────────────────────

def norm_unit(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip().lower()
    return UNIT_ALIASES.get(u, u)


def to_card_unit(qty: float | None, unit: str | None, price: float | None, cand: dict) -> tuple[float | None, float | None, str | None]:
    """Количество и цена в единице карточки. Возвращает (qty, price, note)."""
    if qty is None:
        return None, price, None
    u = norm_unit(unit)
    card = cand.get("unit") or ""
    if not u or u == card:
        return qty, price, None
    sub = SUB_UNITS.get(card, {})
    if u in sub:
        f = sub[u]
        return round(qty / f, 3), (round(price * f, 4) if price else price), f"на листе {qty:g} {u}, перевели в {card}"
    # обратный случай: карточка в граммах/миллилитрах, на листе килограммы/литры
    for big, subs in SUB_UNITS.items():
        if card in subs and u == big:
            f = subs[card]
            return round(qty * f, 3), (round(price / f, 4) if price else price), f"на листе {qty:g} {u}, перевели в {card}"
    if cand.get("pack_name") and u == norm_unit(cand["pack_name"]):
        f = cand["pack_qty"]
        return round(qty * f, 3), (round(price / f, 4) if price else price), f"на листе {qty:g} {u}, это {qty * f:g} {card}"
    if u in ("шт", "уп", "пач") and card in ("кг", "л"):
        return qty, price, f"на листе «{u}», карточка в {card}: проверьте"
    return qty, price, None


def fix_numbers(qty, price, total, usual: float | None) -> tuple[float | None, float | None, float | None, str | None]:
    """Сводит три числа: qty × price = total. Возвращает (qty, price, total, note)."""
    def ok(q, p, t):
        return q is not None and p is not None and t is not None and abs(q * p - t) <= max(1.0, 0.01 * abs(t))
    if ok(qty, price, total):
        return qty, price, total, None
    if total is not None and price and qty is None:
        return round(total / price, 3), price, total, "количество из суммы"
    if total is not None and qty and price is None:
        return qty, round(total / qty, 4), total, "цена из суммы"
    if qty is not None and price is not None and total is None:
        return qty, price, round(qty * price, 2), None
    if qty is None or price is None or total is None:
        return qty, price, total, None
    options = []
    for k in (-3, -2, -1, 1, 2, 3):
        q2 = qty * (10 ** k)
        if ok(q2, price, total):
            options.append((q2, price, f"количество читалось как {qty:g}, по сумме выходит {q2:g}"))
        p2 = price * (10 ** k)
        if ok(qty, p2, total):
            options.append((qty, p2, f"цена читалась как {price:g}, по сумме выходит {p2:g}"))
    if ok(price, qty, total):
        options.append((price, qty, "количество и цена были местами"))
    if usual and qty:
        # цена не читается («1»), но сумма и количество дают цену рядом с обычной
        derived = total / qty
        if abs(derived - usual) <= 0.25 * usual:
            options.append((qty, round(derived, 4), f"цена читалась как {price:g}, по сумме выходит {derived:g}"))
    if usual and price:
        derived_q = total / price
        if abs(price - usual) <= 0.25 * usual and derived_q > 0:
            options.append((round(derived_q, 3), price, f"количество читалось как {qty:g}, по сумме выходит {derived_q:g}"))
    if options:
        if usual:
            options.sort(key=lambda o: abs(o[1] - usual) / usual)
        q2, p2, note = options[0]
        return q2, p2, total, note
    return qty, price, total, f"не сходится: {qty:g} × {price:g} ≠ {total:g}"


def post_process(db: Session, kind: str, result: dict, candidates: list[dict]) -> list[dict]:
    """Строки модели → строки формы: товар, числа в единице карточки, пометки."""
    by_id = {c["id"]: c for c in candidates}
    rows = []
    for ln in result.get("lines") or []:
        raw = str(ln.get("raw") or "").strip()
        pid = ln.get("product_id")
        cand = by_id.get(int(pid)) if isinstance(pid, (int, float, str)) and str(pid).isdigit() else None
        notes: list[str] = []
        question = None
        if cand is None and not re.search(r"[А-Яа-яЁёA-Za-z]{2,}", raw):
            continue  # строка из одних цифр («6908», «260») — итог или мусор, не товар
        if cand is None and raw:
            fuzzy = [c for c in rank_candidates(db, raw, limit=1, standard_only=False) if c["score"] >= FUZZY_THRESHOLD]
            if fuzzy:
                p = db.get(Product, fuzzy[0]["id"])
                # молча подставляем только точное имя/алиас или почти полное совпадение по
                # длине: «мел» начинает «Мелисса», «Луг» похоже на «Лук» — это вопрос, не ответ
                sure = fuzzy[0]["score"] >= SURE_SCORE and (
                    fuzzy[0]["score"] >= 100 or len(raw.strip()) >= 0.75 * len(p.name if p else raw))
                if p is not None and sure:
                    cand = by_id.get(p.id) or _cand(db, p, usual_price(db, p.id) if kind == RECEIPT else None)
                elif p is not None:
                    # похоже, но не уверены: товар не подставляем, спрашиваем
                    question = {"kind": "similar", "text": f"На листе «{raw}». Это {p.name}?",
                                "candidate": {"id": p.id, "name": p.name, "unit": p.unit or ""}}
            if cand is None and question is None:
                question = {"kind": "new", "text": f"«{raw}» — такого товара нет. Новый товар: категория и единица."}
        qty, price, total = _num(ln.get("qty")), _num(ln.get("price")), _num(ln.get("total"))
        if kind == RECEIPT:
            qty, price, total, note = fix_numbers(qty, price, total, cand.get("usual") if cand else None)
            if note:
                notes.append(note)
        if cand is not None:
            qty, price, unote = to_card_unit(qty, ln.get("unit"), price, cand)
            if unote:
                notes.append(unote)
            if kind == RECEIPT and cand.get("usual") and price:
                ratio = price / cand["usual"]
                if ratio >= 2 or ratio <= 0.5:
                    notes.append(f"по {price:g}, обычно {cand['usual']:g} за {cand['unit']}")
        if not raw and cand is None:
            continue
        rows.append({
            "raw": raw, "product_id": cand["id"] if cand else None, "name": cand["name"] if cand else raw,
            "unit": cand["unit"] if cand else (norm_unit(ln.get("unit")) or ""),
            "qty": qty, "price": price, "total": (round(qty * price, 2) if qty is not None and price is not None else total),
            "minor": bool(cand and cand.get("minor")), "balance": cand.get("balance") if cand else None,
            "notes": notes, "question": question,
        })
    return rows


def recognize(db: Session, image_bytes: bytes, kind: str, site_org_id: int, supplier_id: int | None = None,
              mime: str = "image/jpeg", model: str | None = None) -> dict:
    """Фото → строки формы. Не пишет в базу."""
    if kind == RECEIPT:
        candidates = receipt_context(db, site_org_id, supplier_id)
    elif kind == KITCHEN:
        candidates = kitchen_context(db, site_org_id)
    else:
        candidates = count_context(db, site_org_id)
    result = call_model(image_bytes, kind, candidates, mime=mime, model=model)
    rows = post_process(db, kind, result, candidates)
    return {"rows": rows, "amount": result.get("amount"), "raw": result.get("raw"), "usage": result.get("usage", {}),
            "candidates": len(candidates)}


def recognize_text(db: Session, text: str, kind: str, site_org_id: int, model: str | None = None) -> dict:
    """Текст из чата (остаток, передача) → строки формы тем же конвейером, что фото (23.09)."""
    candidates = count_context(db, site_org_id)
    result = call_model(None, kind, candidates, model=model, text=text)
    rows = post_process(db, kind, result, candidates)
    return {"rows": rows, "raw": result.get("raw"), "usage": result.get("usage", {}), "candidates": len(candidates)}
