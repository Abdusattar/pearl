"""Покупка: строки, деньги, склад — общее для старого входа и экрана «Купили».

Первая половина файла переехала из `routers/expenses.py` (16.09) без изменений
логики: разбор строк формы, защита единиц, проверка цены, проводки по
категориям, приходы на склад. Старый вход импортирует их отсюда, новый — тоже,
чтобы деньги по покупке считались одинаково независимо от экрана.

Вторая половина — новое: подсказка поставщиков, список с прошлого привоза,
фасовка, новый товар вопросом, поиск повтора, запись и снятие покупки одной
шапкой `Purchase` (план `context/revision/07_buy_screen.md`).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    AuditLog, CashFunding, Organization, Product, ProductCategory, Receipt, ReceiptItem,
    ReceiptTransaction, Supplier, Transaction, Purchase, User, WarehouseReceipt, ExpenseCategory,
)
from app.services.price_check import fmt_money, price_anomaly_hint, usual_price
from app.services.products import (
    FUZZY_THRESHOLD, UNITS, find_product, get_or_create_product, rank_candidates,
)

# ── перенесено из routers/expenses.py ────────────────────────────────────────

PRICE_QUESTION_MESSAGE = "Проверь цену в отмеченных строках: поправь или отметь «да, так» и запиши снова"


def audit(db: Session, entity_type: str, entity_id: int, action: str,
          user_id: int, new_data: dict = None):
    db.add(AuditLog(
        entity_type=entity_type, entity_id=entity_id,
        action=action, user_id=user_id, new_data=new_data,
    ))


def create_split_transactions(
    db: Session, resolved_items: list[dict], amount: float, amount_paid_val: float | None,
    due_date_val, organization_id: int, supplier_id: int | None, description: str | None,
    tx_date, user_id: int, receipt_id: int | None = None, paid_directly: bool = False,
) -> dict[int | None, int]:
    """Группирует позиции по expense_category_id их товара, создаёт по Transaction на
    каждую получившуюся категорию с пропорциональным делением суммы/оплаты/долга (последней
    группе — остаток, без ошибок округления). Нет позиций — вся сумма уходит в категорию
    None («без категории»). Общая логика для confirm (фото чека) и add (закуп без фото) —
    не дублировать, деньги в проводке должны считаться одинаково независимо от источника.
    receipt_id=None — proводка без связанной квитанции (закуп без позиций)."""
    group_totals: dict = {}
    for it in resolved_items:
        cat_id = it["product"].expense_category_id
        group_totals[cat_id] = group_totals.get(cat_id, 0) + it["total"]

    if not group_totals:
        group_totals[None] = amount

    items_sum = sum(group_totals.values())
    scale = (amount / items_sum) if items_sum else 1.0

    cat_ids = list(group_totals.keys())
    running_amount = 0.0
    running_paid = 0.0
    tx_by_cat: dict[int | None, int] = {}
    for idx, cat_id in enumerate(cat_ids):
        is_last = idx == len(cat_ids) - 1
        if is_last:
            cat_amount = round(amount - running_amount, 2)
        else:
            cat_amount = round(group_totals[cat_id] * scale, 2)
            running_amount += cat_amount
        if cat_amount <= 0:
            continue

        cat_amount_paid = None
        if amount_paid_val is not None:
            if is_last:
                cat_amount_paid = round(amount_paid_val - running_paid, 2)
            else:
                cat_amount_paid = round(cat_amount / amount * amount_paid_val, 2) if amount else 0.0
                running_paid += cat_amount_paid
            if cat_amount_paid >= cat_amount:
                cat_amount_paid = None

        tx = Transaction(
            organization_id=organization_id, type="expense", amount=cat_amount,
            amount_paid=cat_amount_paid, due_date=due_date_val if cat_amount_paid is not None else None,
            category_id=cat_id, supplier_id=supplier_id, description=description, date=tx_date,
            created_by=user_id, paid_directly=paid_directly,
        )
        db.add(tx)
        db.flush()
        if receipt_id is not None:
            db.add(ReceiptTransaction(receipt_id=receipt_id, transaction_id=tx.id, amount=cat_amount))
        audit(db, "transaction", tx.id, "insert", user_id, {"org_id": organization_id, "amount": cat_amount})
        tx_by_cat[cat_id] = tx.id
    return tx_by_cat


def _num_at(vals, i):
    try:
        v = vals[i].strip() if i < len(vals) else ""
        return float(v.replace(",", ".")) if v else None
    except (ValueError, AttributeError):
        return None


def _str_at(vals, i):
    return vals[i].strip() if i < len(vals) and vals[i] else ""


def _plain(v):
    """1260.0 → 1260 для поля type=number (иначе браузер показывает «1260,0»)."""
    return int(v) if isinstance(v, float) and v == int(v) else v


def resolve_item_product(db: Session, name: str, pid_str: str, unit_val: str) -> tuple[Product | None, str | None]:
    """Карточка для позиции формы + защита единиц (15.09).

    У существующей карточки единица не вводится: она из карточки. Если форма
    всё же прислала другую (имя набрано руками и сервер сам нашёл карточку) —
    это не «поменять единицу», а «человек считает не в том»: возвращаем ошибку,
    ничего не пишем. Единица вводится только для новой карточки.
    Возвращает (product, error_message)."""
    product = db.get(Product, int(pid_str)) if pid_str.isdigit() else None
    if not product:
        product = find_product(db, name)
    if product:
        if unit_val and product.unit and unit_val != product.unit:
            return None, (f"«{product.name}» считается в {product.unit}, а в строке выбрано {unit_val}. "
                          f"Введи количество в {product.unit}; единицу карточки меняет только собственник.")
        return product, None
    if not unit_val:
        return None, f"«{name}» — новый товар, выбери для него единицу"
    return get_or_create_product(db, name, unit=unit_val), None


def submitted_rows(db: Session, item_name, item_unit, item_qty, item_unit_price, item_product_id,
                   item_price_ok, hints: dict | None = None) -> list[dict]:
    """Строки формы как их прислал человек — чтобы при ошибке или вопросе о
    цене форма вернулась с теми же данными, а не с пустой таблицей.
    Количество и цена — строками, как набраны (не «3.0» вместо «3»)."""
    hints = hints or {}
    rows = []
    for i, raw_name in enumerate(item_name):
        qty_val = _num_at(item_qty, i)
        price_val = _num_at(item_unit_price, i)
        pid = _str_at(item_product_id, i)
        product = db.get(Product, int(pid)) if pid.isdigit() else None
        total = round(qty_val * price_val, 2) if qty_val is not None and price_val is not None else None
        rows.append({
            "name": raw_name.strip(),
            # у карточки единица своя, что бы ни прислала форма
            "unit": (product.unit or "") if product else _str_at(item_unit, i),
            "qty": _str_at(item_qty, i),
            "unit_price": _str_at(item_unit_price, i),
            "total_price": f"{total:.2f}".rstrip("0").rstrip(".") if total is not None else None,
            "product_id": product.id if product else None,
            "price_ok": _str_at(item_price_ok, i) == "1",
            "price_hint": hints.get(i),
        })
    return rows


def resolve_manual_items(
    db: Session, item_name: list[str], item_qty: list[str], item_unit_price: list[str],
    item_unit: list[str], item_product_id: list[str],
    item_price_ok: list[str] | None = None, tx_date: date | None = None,
    exclude_tx_ids: list[int] | None = None,
) -> tuple[list[dict], str | None, dict]:
    """Резолвит позиции закупа (add / edit-manual) в формат для create_split_transactions.
    Позиции в целом необязательны, но если названа позиция — количество и цена
    обязательны (иначе её доля денег была бы либо потеряна, либо размазана по
    чужим категориям — см. wiki про 07.07); единица — только для нового товара,
    у существующего она из карточки. Цена сверяется с обычной: аномальная
    строка без «да, так» попадает в price_hints по индексу строки.
    Возвращает (items, error_message, price_hints)."""
    item_price_ok = item_price_ok or []
    resolved_items = []
    price_hints: dict[int, str] = {}
    for i, raw_name in enumerate(item_name):
        name = raw_name.strip()
        if not name:
            continue
        qty_val = _num_at(item_qty, i)
        price_val = _num_at(item_unit_price, i)
        if qty_val is None or price_val is None:
            return [], "Заполни количество и цену для каждой введённой позиции", {}

        product, err = resolve_item_product(db, name, _str_at(item_product_id, i), _str_at(item_unit, i))
        if err:
            return [], err, {}
        if _str_at(item_price_ok, i) != "1":
            hint = price_anomaly_hint(db, product, price_val, tx_date, exclude_tx_ids)
            if hint:
                price_hints[i] = hint

        resolved_items.append({
            "name": name, "product": product, "qty": qty_val,
            "unit_price": price_val, "total": round(qty_val * price_val, 2),
        })
    return resolved_items, None, price_hints


def create_warehouse_receipts(
    db: Session, resolved_items: list[dict], tx_by_cat: dict, organization_id: int,
    tx_date, user_id: int,
) -> None:
    """Кладёт позиции с количеством и ценой на склад, привязывая каждую к своей
    Transaction по категории товара — та же группировка, что и в проводках."""
    main_tx_id = next(iter(tx_by_cat.values()), None)
    for it in resolved_items:
        if it["qty"] and it["qty"] > 0 and it["unit_price"] and it["unit_price"] > 0:
            db.add(WarehouseReceipt(
                date=tx_date,
                product_id=it["product"].id,
                quantity=it["qty"],
                price_per_unit=it["unit_price"],
                total_cost=it["total"],
                organization_id=organization_id,
                transaction_id=tx_by_cat.get(it["product"].expense_category_id, main_tx_id),
                created_by=user_id,
            ))


# ── новый вход: экран «Купили» ───────────────────────────────────────────────

SUPPLIER_CHIPS_DAYS = 60
SUPPLIER_CHIPS_LIMIT = 6
DUPLICATE_WINDOW_DAYS = 10
PAYMENTS = ("debt", "cash", "account", "part", "founder")
# Категории, по которым «для кого» обязателен: стена в садике по квадратуре
# не делится (решение владельца 14.09). Сверяется с именем категории товара.
FOR_ORG_REQUIRED_CATEGORIES = {"ремонт", "стройматериалы"}
# Статья расходов старого входа по категории товара нового — чтобы карточка,
# рождённая в «Купили», не падала в «без категории» у старых отчётов.
_FOOD = "продукты питания"
EXPENSE_CATEGORY_BY_PRODUCT_CATEGORY = {
    "мясо и рыба": _FOOD, "молочное и яйца": _FOOD, "овощи и фрукты": _FOOD,
    "крупы и макароны": _FOOD, "бакалея и масла": _FOOD, "сладости и прочее (еда)": _FOOD,
    "напитки и вода": _FOOD, "зелень": _FOOD, "специи": _FOOD, "хлеб и выпечка": _FOOD,
    "моющее и инвентарь": "хозяйственные материалы", "канцелярия": "канцелярские товары",
    "ремонт": "текущий ремонт имущества",
}


def site_orgs(db: Session, site_org_id: int) -> list[Organization]:
    """Объекты площадки: сама площадка и те, у кого она в site_id (Садик, Школа)."""
    orgs = (
        db.query(Organization)
        .filter((Organization.id == site_org_id) | (Organization.site_id == site_org_id))
        .order_by(Organization.id)
        .all()
    )
    return [o for o in orgs if o.type != "root"]


def suggest_suppliers(db: Session, site_org_id: int, limit: int = SUPPLIER_CHIPS_LIMIT) -> list[Supplier]:
    """Чипы «У кого?»: поставщики по числу покупок площадки за 60 дней."""
    org_ids = [o.id for o in site_orgs(db, site_org_id)]
    since = date.today() - timedelta(days=SUPPLIER_CHIPS_DAYS)
    rows = (
        db.query(Transaction.supplier_id, func.count(func.distinct(Transaction.date)).label("n"))
        .filter(
            Transaction.organization_id.in_(org_ids), Transaction.type == "expense",
            Transaction.supplier_id.isnot(None), Transaction.deleted_at.is_(None),
            Transaction.date >= since,
        )
        .group_by(Transaction.supplier_id)
        .order_by(func.count(func.distinct(Transaction.date)).desc())
        .limit(limit)
        .all()
    )
    ids = [sid for sid, _ in rows]
    if not ids:
        return []
    by_id = {s.id: s for s in db.query(Supplier).filter(Supplier.id.in_(ids)).all()}
    return [by_id[i] for i in ids if i in by_id]


def default_payment(db: Session, supplier_id: int) -> str:
    """У кого брали в долг за 60 дней — «в долг», иначе «из кассы» (на рынке в долг не берут)."""
    since = date.today() - timedelta(days=SUPPLIER_CHIPS_DAYS)
    has_debt = (
        db.query(Transaction.id)
        .filter(
            Transaction.supplier_id == supplier_id, Transaction.type == "expense",
            Transaction.deleted_at.is_(None), Transaction.date >= since,
            Transaction.amount_paid.isnot(None),
        )
        .first()
    )
    return "debt" if has_debt else "cash"


def _last_purchase_tx_ids(db: Session, supplier_id: int) -> tuple[date | None, list[int]]:
    """Проводки последней покупки у поставщика: по шапке нового входа, иначе по
    квитанции старого (один чек режется на несколько проводок по категориям)."""
    last = (
        db.query(Transaction)
        .filter(Transaction.supplier_id == supplier_id, Transaction.type == "expense",
                Transaction.deleted_at.is_(None))
        .order_by(Transaction.date.desc(), Transaction.id.desc())
        .first()
    )
    if last is None:
        return None, []
    if last.purchase_id is not None:
        ids = [t.id for t in db.query(Transaction.id).filter(Transaction.purchase_id == last.purchase_id,
                                                             Transaction.deleted_at.is_(None)).all()]
        return last.date, ids
    rt = db.query(ReceiptTransaction).filter(ReceiptTransaction.transaction_id == last.id).first()
    if rt is None:
        return last.date, [last.id]
    ids = [r.transaction_id for r in db.query(ReceiptTransaction.transaction_id)
           .filter(ReceiptTransaction.receipt_id == rt.receipt_id).all()]
    return last.date, ids


def prefill_from_last(db: Session, supplier_id: int) -> tuple[date | None, list[dict]]:
    """Список с прошлого привоза: товар, количество, цена — как строки формы."""
    last_date, tx_ids = _last_purchase_tx_ids(db, supplier_id)
    if not tx_ids:
        return last_date, []
    receipts = (
        db.query(WarehouseReceipt)
        .filter(WarehouseReceipt.transaction_id.in_(tx_ids), WarehouseReceipt.deleted_at.is_(None))
        .order_by(WarehouseReceipt.id)
        .all()
    )
    rows = []
    for r in receipts:
        product = r.product
        while product.merged_into_id is not None:
            product = product.merged_into
        if product.retired_at is not None:
            continue
        rows.append(_row_dict(product, r.quantity, r.price_per_unit))
    return last_date, rows


def _fmt_num(v) -> str:
    if v is None:
        return ""
    d = Decimal(str(v)).normalize()
    s = format(d, "f")
    return s.replace(".", ",")


def _row_dict(product: Product | None, qty=None, price=None, name: str | None = None,
              chosen_unit: str | None = None) -> dict:
    """Строка формы. У карточки единица своя; `chosen_unit` — что выбрал человек
    (единица карточки, её фасовка или единица нового товара)."""
    total = None
    if qty is not None and price is not None:
        total = round(float(qty) * float(price), 2)
    unit = (product.unit or "") if product else ""
    has_pack = bool(product and product.pack_qty and product.pack_name)
    if product and chosen_unit not in (unit, product.pack_name if has_pack else None):
        chosen_unit = unit
    return {
        "name": product.name if product else (name or ""),
        "product_id": product.id if product else None,
        "unit": unit if product else (chosen_unit or ""),
        "pack_name": product.pack_name if has_pack else None,
        "pack_qty": _fmt_num(product.pack_qty) if has_pack else None,
        "chosen_unit": chosen_unit or unit,
        "qty": _fmt_num(qty), "unit_price": _fmt_num(price),
        "total_price": _fmt_num(total) if total is not None else "",
        "price_ok": False, "price_hint": None,
        "is_new": False, "category_id": None, "question": None,
    }


def buy_rows_as_submitted(db: Session, form: dict, questions: dict | None = None) -> list[dict]:
    """Строки формы «Купили» как их прислал человек, плюс вопросы по индексу."""
    questions = questions or {}
    rows = []
    n = len(form.get("item_name", []))
    for i in range(n):
        name = _str_at(form["item_name"], i)
        pid = _str_at(form.get("item_product_id", []), i)
        product = db.get(Product, int(pid)) if pid.isdigit() else None
        chosen_unit = _str_at(form.get("item_unit", []), i)
        cat = _str_at(form.get("item_category_id", []), i)
        row = _row_dict(product, name=name, chosen_unit=chosen_unit)
        row["qty"] = _str_at(form.get("item_qty", []), i)
        row["unit_price"] = _str_at(form.get("item_unit_price", []), i)
        qty_val = _num_at(form.get("item_qty", []), i)
        price_val = _num_at(form.get("item_unit_price", []), i)
        row["total_price"] = _fmt_num(round(qty_val * price_val, 2)) if qty_val is not None and price_val is not None else ""
        row["price_ok"] = _str_at(form.get("item_price_ok", []), i) == "1"
        row["is_new"] = _str_at(form.get("item_new", []), i) == "1"
        row["category_id"] = int(cat) if cat.isdigit() else None
        row["question"] = questions.get(i)
        rows.append(row)
    return rows


def _expense_category_for(db: Session, category: ProductCategory | None) -> int | None:
    if category is None:
        return None
    wanted = EXPENSE_CATEGORY_BY_PRODUCT_CATEGORY.get(category.name.lower())
    if not wanted:
        return None
    cat = db.query(ExpenseCategory).filter(func.lower(ExpenseCategory.name) == wanted).first()
    return cat.id if cat else None


def resolve_buy_rows(db: Session, form: dict, tx_date: date) -> tuple[list[dict], dict, str | None]:
    """Строки формы «Купили» → позиции для проводок.

    Отличия от старой формы: единица не вводится вовсе, но можно ввести в
    фасовке карточки («2 лотка» → 60 шт, цена за лоток → за шт); незнакомое
    имя не создаёт карточку молча — сначала вопрос «это X?», потом «новый
    товар: категория, единица». Вопросы возвращаются по индексу строки
    (`questions`), ничего не пишется, пока человек не ответил.
    Возвращает (items, questions, error)."""
    items: list[dict] = []
    questions: dict[int, dict] = {}
    names = form.get("item_name", [])
    for i in range(len(names)):
        name = _str_at(names, i)
        if not name:
            continue
        qty_val = _num_at(form.get("item_qty", []), i)
        price_val = _num_at(form.get("item_unit_price", []), i)
        pid = _str_at(form.get("item_product_id", []), i)
        chosen_unit = _str_at(form.get("item_unit", []), i)
        is_new = _str_at(form.get("item_new", []), i) == "1"
        cat_str = _str_at(form.get("item_category_id", []), i)
        price_ok = _str_at(form.get("item_price_ok", []), i) == "1"

        product = db.get(Product, int(pid)) if pid.isdigit() else None
        if product is not None:
            while product.merged_into_id is not None:
                product = product.merged_into
            if product.retired_at is not None:
                product = None
        if product is None and not is_new:
            product = _exact_product(db, name)

        if product is None:
            if is_new:
                if not cat_str.isdigit() or chosen_unit not in UNITS:
                    questions[i] = {"kind": "new", "text": f"«{name}» — новый товар. Выберите категорию и единицу."}
                    continue
                # создаётся позже, при записи — пока только описание
                if qty_val is None or price_val is None:
                    return [], {}, "Заполни количество и цену для каждой строки"
                items.append({
                    "name": name, "product": None, "new_category_id": int(cat_str), "new_unit": chosen_unit,
                    "qty": qty_val, "unit_price": price_val, "total": round(qty_val * price_val, 2),
                    "index": i,
                })
                continue
            cands = [c for c in rank_candidates(db, name, limit=1, standard_only=False) if c["score"] >= FUZZY_THRESHOLD]
            if cands:
                cand = db.get(Product, cands[0]["id"])
                questions[i] = {"kind": "similar", "text": f"Товара «{name}» нет. Это {cand.name}?",
                                "candidate": {"id": cand.id, "name": cand.name, "unit": cand.unit or ""}}
            else:
                questions[i] = {"kind": "new", "text": f"Товара «{name}» нет. Новый товар: категория и единица."}
            continue

        if qty_val is None or price_val is None:
            return [], {}, "Заполни количество и цену для каждой строки"

        # единица: карточки или её фасовка, иначе человек считает не в том
        if chosen_unit and chosen_unit != (product.unit or ""):
            if product.pack_name and product.pack_qty and chosen_unit == product.pack_name:
                factor = float(product.pack_qty)
                qty_val = round(qty_val * factor, 3)
                price_val = round(price_val / factor, 4)
            else:
                return [], {}, (f"«{product.name}» считается в {product.unit}, а в строке выбрано {chosen_unit}. "
                                f"Введи количество в {product.unit}; единицу карточки меняет только собственник.")

        item = {
            "name": product.name, "product": product, "qty": qty_val,
            "unit_price": price_val, "total": round(qty_val * price_val, 2), "index": i,
        }
        if not price_ok:
            hint = price_anomaly_hint(db, product, price_val, tx_date)
            if hint:
                questions[i] = {"kind": "price", "text": hint}
        items.append(item)
    return items, questions, None


def _exact_product(db: Session, name: str) -> Product | None:
    """Точное имя или алиас — без fuzzy: похожее имя в «Купили» идёт вопросом."""
    product = db.query(Product).filter(func.lower(Product.name) == name.lower()).first()
    if product is None:
        from app.services.products import match_product
        product = match_product(db, name)
    if product is None:
        return None
    while product.merged_into_id is not None:
        product = product.merged_into
    return None if product.retired_at is not None else product


def needs_for_org(db: Session, items: list[dict]) -> bool:
    """Все строки — ремонт или стройматериалы: «для кого» обязателен."""
    if not items:
        return False
    cat_ids = set()
    for it in items:
        cid = it["product"].category_id if it.get("product") else it.get("new_category_id")
        if cid is None:
            return False
        cat_ids.add(cid)
    names = {c.name.lower() for c in db.query(ProductCategory).filter(ProductCategory.id.in_(cat_ids)).all()}
    return bool(names) and names <= FOR_ORG_REQUIRED_CATEGORIES


def find_duplicate(db: Session, supplier_id: int, tx_date: date, total: float,
                   product_ids: set[int], file_hash: str | None = None) -> dict | None:
    """Такая же покупка за 10 дней: та же сумма или тот же набор товаров, либо то
    же фото (решение владельца 15.09). Смотрит и покупки старого входа —
    один чек там лежит несколькими проводками, группируем по квитанции."""
    if file_hash:
        r = db.query(Receipt).filter(Receipt.file_hash == file_hash).first()
        if r is not None:
            return {"kind": "photo", "date": r.created_at.date() if r.created_at else None,
                    "total": float(r.amount_confirmed or r.amount_detected or 0), "count": None}
    since, until = tx_date - timedelta(days=DUPLICATE_WINDOW_DAYS), tx_date + timedelta(days=DUPLICATE_WINDOW_DAYS)
    txs = (
        db.query(Transaction)
        .filter(Transaction.supplier_id == supplier_id, Transaction.type == "expense",
                Transaction.deleted_at.is_(None), Transaction.date >= since, Transaction.date <= until)
        .all()
    )
    if not txs:
        return None
    tx_ids = [t.id for t in txs]
    receipt_by_tx = dict(db.query(ReceiptTransaction.transaction_id, ReceiptTransaction.receipt_id)
                         .filter(ReceiptTransaction.transaction_id.in_(tx_ids)).all())
    groups: dict = {}
    for t in txs:
        key = ("p", t.purchase_id) if t.purchase_id else (("r", receipt_by_tx[t.id]) if t.id in receipt_by_tx else ("t", t.id))
        g = groups.setdefault(key, {"date": t.date, "total": 0.0, "tx_ids": []})
        g["total"] += float(t.amount)
        g["tx_ids"].append(t.id)
    wr = (
        db.query(WarehouseReceipt.transaction_id, WarehouseReceipt.product_id)
        .filter(WarehouseReceipt.transaction_id.in_(tx_ids), WarehouseReceipt.deleted_at.is_(None))
        .all()
    )
    products_by_tx: dict[int, set] = {}
    for tid, pid in wr:
        products_by_tx.setdefault(tid, set()).add(pid)
    for key, g in sorted(groups.items(), key=lambda kv: kv[1]["date"], reverse=True):
        g_products = set().union(*(products_by_tx.get(tid, set()) for tid in g["tx_ids"]))
        same_total = abs(g["total"] - total) < 1
        same_items = bool(product_ids) and g_products == product_ids
        if same_total or same_items:
            return {"kind": "same", "date": g["date"], "total": round(g["total"], 2),
                    "count": len(g_products) or None,
                    "purchase_id": key[1] if key[0] == "p" else None}
    return None


def pocket_users(db: Session, site_org_id: int) -> list[User]:
    """Чьи карманы: люди площадки, кроме учредителей."""
    org_ids = [o.id for o in site_orgs(db, site_org_id)]
    return (
        db.query(User)
        .filter(User.deleted_at.is_(None), User.organization_id.in_(org_ids),
                User.role.in_(("staff", "manager", "director")))
        .order_by(User.name)
        .all()
    )


def founders(db: Session) -> list[User]:
    return (
        db.query(User)
        .filter(User.deleted_at.is_(None), User.role.in_(("founder", "owner")))
        .order_by(User.id)
        .all()
    )


def record_purchase(
    db: Session, *, user: User, site_org_id: int, supplier_id: int, tx_date: date,
    items: list[dict], payment: str, paid_amount: float | None = None,
    payer_id: int | None = None, account_org_id: int | None = None, founder_id: int | None = None,
    for_org_id: int | None = None, receipt_id: int | None = None, note: str | None = None,
    dup_confirmed: bool = False,
) -> Purchase:
    """Записывает покупку: шапка, проводки по категориям, приход на склад,
    строки чека, при «заплатил учредитель» — взнос. Не коммитит."""
    assert payment in PAYMENTS
    # новые карточки рождаются только здесь, после всех вопросов
    for it in items:
        if it.get("product") is None:
            cat = db.get(ProductCategory, it["new_category_id"])
            product = Product(
                name=it["name"], unit=it["new_unit"], category_id=cat.id if cat else None,
                category=cat.name if cat else None, is_standard=True,
                expense_category_id=_expense_category_for(db, cat),
            )
            db.add(product)
            db.flush()
            audit(db, "product", product.id, "insert", user.id, {"from": "new/buy", "unit": product.unit})
            it["product"] = product

    total = round(sum(it["total"] for it in items), 2)
    if payment == "debt":
        amount_paid_val: float | None = 0.0
    elif payment == "part":
        amount_paid_val = float(paid_amount or 0)
        if amount_paid_val >= total:
            amount_paid_val = None
    else:
        amount_paid_val = None
    paid_directly = payment == "account"
    if payment not in ("cash", "part", "founder"):
        payer_id = None
    if payment == "founder":
        payer_id = founder_id
    if payment != "account":
        account_org_id = None

    if receipt_id is None:
        receipt = Receipt(
            organization_id=site_org_id, file_path="manual", ocr_status="manual",
            amount_confirmed=total, confirmed_by=user.id, confirmed_at=datetime.now(), created_by=user.id,
        )
        db.add(receipt)
        db.flush()
        receipt_id = receipt.id
    else:
        receipt = db.get(Receipt, receipt_id)
        receipt.amount_confirmed = total
        receipt.confirmed_by = user.id
        receipt.confirmed_at = datetime.now()
        receipt.ocr_status = "confirmed"

    supplier = db.get(Supplier, supplier_id)
    purchase = Purchase(
        site_org_id=site_org_id, supplier_id=supplier_id, date=tx_date, total=total,
        payment=payment, paid_amount=(total if amount_paid_val is None else amount_paid_val),
        paid_from_user_id=payer_id, account_org_id=account_org_id, founder_id=founder_id if payment == "founder" else None,
        for_org_id=for_org_id, receipt_id=receipt_id, note=note, dup_confirmed=dup_confirmed, created_by=user.id,
    )
    db.add(purchase)
    db.flush()

    tx_by_cat = create_split_transactions(
        db, items, total, amount_paid_val, None, site_org_id, supplier_id, note, tx_date, user.id,
        receipt_id=receipt_id, paid_directly=paid_directly,
    )
    for tx_id in tx_by_cat.values():
        tx = db.get(Transaction, tx_id)
        tx.purchase_id = purchase.id
        tx.paid_from_user_id = payer_id
        tx.account_org_id = account_org_id
    create_warehouse_receipts(db, items, tx_by_cat, site_org_id, tx_date, user.id)
    for it in items:
        db.add(ReceiptItem(receipt_id=receipt_id, name=it["name"], product_id=it["product"].id,
                           qty=it["qty"], unit_price=it["unit_price"], total_price=it["total"]))

    if payment == "founder":
        funding = CashFunding(
            organization_id=site_org_id, source_type="direct_cash", amount=total, date=tx_date,
            taken_by=founder_id, accountable_user_id=founder_id, source_founder_id=founder_id,
            comment=f"Заплатил учредитель: {supplier.name if supplier else ''}", created_by=user.id,
        )
        db.add(funding)
        db.flush()
        purchase.funding_id = funding.id

    audit(db, "purchase", purchase.id, "insert", user.id,
          {"site": site_org_id, "supplier": supplier_id, "total": total, "payment": payment})
    return purchase


def remove_purchase(db: Session, purchase: Purchase, user: User) -> None:
    """Убрать покупку: мягко, всё остаётся в истории с пометкой. Склад и долг
    откатываются сами, потому что считаются по живым записям."""
    now = datetime.now()
    purchase.deleted_at = now
    purchase.deleted_by = user.id
    txs = db.query(Transaction).filter(Transaction.purchase_id == purchase.id, Transaction.deleted_at.is_(None)).all()
    tx_ids = [t.id for t in txs]
    for t in txs:
        t.deleted_at = now
    if tx_ids:
        for wr in db.query(WarehouseReceipt).filter(WarehouseReceipt.transaction_id.in_(tx_ids),
                                                    WarehouseReceipt.deleted_at.is_(None)).all():
            wr.deleted_at = now
    if purchase.funding_id:
        funding = db.get(CashFunding, purchase.funding_id)
        if funding and funding.deleted_at is None:
            funding.deleted_at = now
    audit(db, "purchase", purchase.id, "delete", user.id, {"tx_ids": tx_ids})


def purchase_lines(db: Session, purchase: Purchase) -> list[dict]:
    """Строки карточки покупки: товар, количество × цена, сумма."""
    tx_ids = [t.id for t in purchase.transactions]
    if not tx_ids:
        return []
    rows = (
        db.query(WarehouseReceipt)
        .filter(WarehouseReceipt.transaction_id.in_(tx_ids))
        .order_by(WarehouseReceipt.id)
        .all()
    )
    return [{
        "name": r.product.name, "qty": _fmt_num(r.quantity), "unit": r.product.unit or "",
        "price": fmt_money(float(r.price_per_unit)), "total": fmt_money(float(r.total_cost)),
    } for r in rows]


def usual_prices(db: Session, product_ids: list[int]) -> dict[int, float]:
    return {pid: p for pid in product_ids if (p := usual_price(db, pid)) is not None}
