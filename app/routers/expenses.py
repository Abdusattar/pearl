import shutil
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    ExpenseCategory, Organization, Product, Receipt, ReceiptItem, ReceiptTransaction,
    Transaction, User, AuditLog, WarehouseReceipt, Supplier, RecurringExpenseTemplate,
)
from app.services.ocr import compute_hash, analyze_receipt
from app.services.products import match_product, rank_candidates, get_or_create_product, ensure_alias
from app.services.normalize import normalize_items
from app.services import recurring_expenses
from app.dependencies import get_current_user

router = APIRouter(prefix="/expenses", tags=["expenses"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

MEDIA_DIR = Path(__file__).parent.parent.parent / "media" / "receipts"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

ORG_KINDERGARTENS = 3
# см. app/dependencies.py:PILOT_ORG_IDS/PILOT_USER_IDS — тот же смысл,
# продублировано, т.к. этот файл держит свою копию get_accessible_orgs/
# resolve_org (известный техдолг, стоило бы удалить дубликат и импортировать
# из dependencies.py, не сегодня). Ограничение по конкретным user_id, не по
# роли целиком — иначе реальный владелец терял бы доступ к Школе/Кожомкулу
# на проде (13.07)
PILOT_ORG_IDS = {4}
PILOT_USER_IDS = {1, 3, 61, 64}  # Абдусаттар, Мунара, Айдай, Талас


def get_accessible_orgs(user: User, db: Session, all_orgs: list[Organization] | None = None) -> list[Organization]:
    """Орги доступные пользователю по роли."""
    if all_orgs is None:
        all_orgs = db.query(Organization).all()
    if user.role == "director":
        return [o for o in all_orgs if o.id == user.organization_id]
    if user.id in PILOT_USER_IDS:
        return [o for o in all_orgs if o.id in PILOT_ORG_IDS]
    if user.role in ("owner", "founder", "manager"):
        return all_orgs
    return [o for o in all_orgs if o.id == user.organization_id]


def get_upload_orgs(user: User, db: Session, all_orgs: list[Organization] | None = None) -> list[Organization]:
    """Только листовые орги (без родительских узлов) — куда можно загружать расходы."""
    if all_orgs is None:
        all_orgs = db.query(Organization).all()
    has_children = {o.parent_id for o in all_orgs if o.parent_id is not None}
    orgs = get_accessible_orgs(user, db, all_orgs)
    return [o for o in orgs if o.id not in has_children]


def resolve_org(org_id: int | None, user: User, db: Session, all_orgs: list[Organization] | None = None) -> Organization:
    accessible = get_accessible_orgs(user, db, all_orgs)
    if org_id:
        org = next((o for o in accessible if o.id == org_id), None)
        if org:
            return org
    # см. комментарий в app/dependencies.py:resolve_org — тот же баг, та же правка (12.07)
    own = next((o for o in accessible if o.id == user.organization_id), None)
    if own:
        return own
    return accessible[0] if accessible else None


def get_categories(db: Session) -> list[ExpenseCategory]:
    return db.query(ExpenseCategory).order_by(
        ExpenseCategory.parent_id.nullsfirst(), ExpenseCategory.name
    ).all()


SERVICE_CAT_NAMES = {"сервисные расходы"}


def get_service_category_id(db: Session) -> int | None:
    cat = (
        db.query(ExpenseCategory)
        .filter(ExpenseCategory.parent_id.is_(None), func.lower(ExpenseCategory.name).in_(SERVICE_CAT_NAMES))
        .first()
    )
    return cat.id if cat else None


def audit(db: Session, entity_type: str, entity_id: int, action: str,
          user_id: int, new_data: dict = None):
    db.add(AuditLog(
        entity_type=entity_type, entity_id=entity_id,
        action=action, user_id=user_id, new_data=new_data,
    ))


def resolve_supplier(db: Session, supplier_id_raw: str, new_name: str, new_phone: str) -> int | None:
    """Возвращает supplier_id: из существующего или создаёт нового."""
    if not supplier_id_raw:
        return None
    if supplier_id_raw != "new":
        try:
            return int(supplier_id_raw)
        except (ValueError, TypeError):
            return None
    name = (new_name or "").strip()
    if not name:
        return None
    existing = db.query(Supplier).filter(Supplier.name == name).first()
    if existing:
        return existing.id
    phone_val = (new_phone or "").strip() or None
    s = Supplier(name=name, phone=phone_val)
    db.add(s)
    db.flush()
    return s.id


def create_split_transactions(
    db: Session, resolved_items: list[dict], amount: float, amount_paid_val: float | None,
    due_date_val, organization_id: int, supplier_id: int | None, description: str | None,
    tx_date, user_id: int, receipt_id: int | None = None,
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
            created_by=user_id,
        )
        db.add(tx)
        db.flush()
        if receipt_id is not None:
            db.add(ReceiptTransaction(receipt_id=receipt_id, transaction_id=tx.id, amount=cat_amount))
        audit(db, "transaction", tx.id, "insert", user_id, {"org_id": organization_id, "amount": cat_amount})
        tx_by_cat[cat_id] = tx.id
    return tx_by_cat


def resolve_manual_items(
    db: Session, item_name: list[str], item_qty: list[str], item_unit_price: list[str],
    item_unit: list[str], item_product_id: list[str],
) -> tuple[list[dict], str | None]:
    """Резолвит позиции закупа (add / edit-manual) в формат для create_split_transactions.
    Позиции в целом необязательны, но если названа позиция — количество, цена и единица
    измерения обязательны все три вместе (иначе её доля денег была бы либо потеряна, либо
    размазана по чужим категориям — см. wiki про 07.07). Возвращает (items, error_message)."""
    def _safe(vals, i):
        try:
            v = vals[i].strip() if i < len(vals) else ""
            return float(v.replace(",", ".")) if v else None
        except (ValueError, AttributeError):
            return None

    resolved_items = []
    for i, raw_name in enumerate(item_name):
        name = raw_name.strip()
        if not name:
            continue
        qty_val = _safe(item_qty, i)
        price_val = _safe(item_unit_price, i)
        unit_val = item_unit[i].strip() if i < len(item_unit) else ""
        if qty_val is None or price_val is None or not unit_val:
            return [], "Заполни количество, цену и единицу измерения для каждой введённой позиции"

        pid_str = item_product_id[i].strip() if i < len(item_product_id) else ""
        product = db.get(Product, int(pid_str)) if pid_str.isdigit() else None
        if not product:
            product = get_or_create_product(db, name)
        if not product.is_standard and product.unit != unit_val:
            product.unit = unit_val

        resolved_items.append({
            "name": name, "product": product, "qty": qty_val,
            "unit_price": price_val, "total": round(qty_val * price_val, 2),
        })
    return resolved_items, None


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


# ── PRODUCT SEARCH API ────────────────────────────────────────────────────────

from fastapi.responses import JSONResponse

@router.get("/products/search")
def search_products(q: str = "", db: Session = Depends(get_db)):
    if not q.strip():
        return JSONResponse([])
    candidates = rank_candidates(db, q.strip(), limit=6)
    return JSONResponse(candidates)


# ── LIST ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def list_expenses(
    request: Request,
    org_id: str | None = None,
    category_id: str | None = None,
    month: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    org_id = int(org_id) if org_id and org_id.isdigit() else None
    category_id = int(category_id) if category_id and category_id.isdigit() else None
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    all_orgs = db.query(Organization).all()
    accessible = get_accessible_orgs(user, db, all_orgs)
    current_org = resolve_org(org_id, user, db, all_orgs)
    current_org_id = current_org.id if current_org else None

    # Доступные org_id для фильтрации
    if user.role == "manager":
        visible_org_ids = [o.id for o in accessible]
    elif current_org_id:
        # Показываем текущую орг и её детей
        visible_org_ids = [current_org_id]
        visible_org_ids += [o.id for o in all_orgs if o.parent_id == current_org_id]
    else:
        visible_org_ids = [o.id for o in accessible]

    # Категория + её дети (для корректного фильтра)
    cat_ids_filter = None
    if category_id:
        children = db.query(ExpenseCategory).filter(
            ExpenseCategory.parent_id == category_id
        ).all()
        cat_ids_filter = [category_id] + [c.id for c in children]

    # --- Квитанции ---
    q = db.query(Receipt).filter(Receipt.organization_id.in_(visible_org_ids))

    if cat_ids_filter:
        tx_receipt_ids = (
            db.query(ReceiptTransaction.receipt_id)
            .join(Transaction, Transaction.id == ReceiptTransaction.transaction_id)
            .filter(Transaction.category_id.in_(cat_ids_filter))
            .subquery()
        )
        q = q.filter(Receipt.id.in_(tx_receipt_ids))

    if month:
        try:
            y, m = month.split("-")
            q = q.filter(
                func.extract("year", Receipt.created_at) == int(y),
                func.extract("month", Receipt.created_at) == int(m),
            )
        except Exception:
            pass

    if status:
        if status == "unconfirmed":
            q = q.filter(Receipt.ocr_status.in_(["pending", "processed"]))
        else:
            q = q.filter(Receipt.ocr_status == status)

    receipts_raw = q.order_by(Receipt.created_at.desc()).limit(200).all()
    pending_count = db.query(Receipt).filter(
        Receipt.organization_id.in_(visible_org_ids),
        Receipt.ocr_status.in_(["pending", "processed"]),
    ).count()

    org_map = {o.id: o.name for o in all_orgs}
    supplier_map = {s.id: s.name for s in db.query(Supplier).all()}
    cat_map = {c.id: c.name for c in db.query(ExpenseCategory).all()}

    def _cat_name(category_id_val):
        return cat_map.get(category_id_val) if category_id_val else None

    # Одним запросом подтягиваем транзакции всех квитанций разом (вместо запроса на каждую строку)
    receipt_ids = [r.id for r in receipts_raw]
    tx_by_receipt = {}
    if receipt_ids:
        rt_tx_rows = (
            db.query(ReceiptTransaction.receipt_id, Transaction)
            .join(Transaction, Transaction.id == ReceiptTransaction.transaction_id)
            .filter(ReceiptTransaction.receipt_id.in_(receipt_ids))
            .order_by(Transaction.id)
            .all()
        )
        for receipt_id, tx in rt_tx_rows:
            # При авторазбивке по категориям у чека может быть несколько транзакций —
            # в списке показываем первую (самую раннюю по id), как и раньше.
            tx_by_receipt.setdefault(receipt_id, tx)

    receipts = []
    for r in receipts_raw:
        tx = tx_by_receipt.get(r.id)
        tx_cat = _cat_name(tx.category_id) if tx else None
        tx_id = tx.id if tx else None
        tx_supplier = supplier_map.get(tx.supplier_id) if tx else None
        tx_debt = (tx.amount - tx.amount_paid) if tx and tx.amount_paid is not None else None
        if r.ocr_status == "manual":
            # Позиции определяют разбивку по категориям — редактируется как целый чек
            # (та же форма ввода), не как одна отдельная Transaction (edit_tx.html).
            href = f"/expenses/{r.id}/edit-manual?org_id={current_org_id}"
        else:
            href = f"/expenses/{r.id}/confirm?org_id={current_org_id}"
        receipts.append({
            "row_type": "receipt",
            "id": r.id,
            "tx_id": tx_id,
            "href": href,
            "is_recurring": False,
            "sort_date": r.created_at.date() if hasattr(r.created_at, 'date') else r.created_at,
            "date_display": r.created_at.strftime('%d.%m.%Y'),
            "org_name": org_map.get(r.organization_id, "—"),
            "category_name": tx_cat,
            "supplier_name": tx_supplier,
            "amount_detected": r.amount_detected,
            "amount_confirmed": r.amount_confirmed,
            "ocr_status": r.ocr_status,
            "debt": tx_debt,
        })

    # --- Ручные транзакции (без квитанции) ---
    if not status:  # ручные не имеют OCR-статуса, скрываем если фильтр по статусу
        receipt_tx_subq = db.query(ReceiptTransaction.transaction_id).subquery()
        manual_q = db.query(Transaction).filter(
            Transaction.type == "expense",
            Transaction.deleted_at.is_(None),
            Transaction.organization_id.in_(visible_org_ids),
            ~Transaction.id.in_(receipt_tx_subq),
        )
        if cat_ids_filter:
            manual_q = manual_q.filter(Transaction.category_id.in_(cat_ids_filter))
        if month:
            try:
                y, m = month.split("-")
                manual_q = manual_q.filter(
                    func.extract("year", Transaction.date) == int(y),
                    func.extract("month", Transaction.date) == int(m),
                )
            except Exception:
                pass
        for tx in manual_q.order_by(Transaction.date.desc()).limit(200).all():
            is_recurring = tx.recurring_template_id is not None
            if is_recurring:
                rec_month = tx.period.isoformat() if tx.period else recurring_expenses.month_start().isoformat()
                href = f"/expenses/recurring?org_id={current_org_id}&month={rec_month}"
            else:
                href = f"/expenses/tx/{tx.id}/edit?org_id={current_org_id}"
            receipts.append({
                "row_type": "manual",
                "id": None,
                "tx_id": tx.id,
                "href": href,
                "is_recurring": is_recurring,
                "sort_date": tx.date,
                "date_display": tx.date.strftime('%d.%m.%Y') if tx.date else "—",
                "org_name": org_map.get(tx.organization_id, "—"),
                "category_name": _cat_name(tx.category_id),
                "description": tx.description,
                "supplier_name": supplier_map.get(tx.supplier_id),
                "amount_detected": None,
                "amount_confirmed": tx.amount,
                "ocr_status": "manual",
                "debt": (tx.amount - tx.amount_paid) if tx.amount_paid is not None else None,
            })
        receipts.sort(key=lambda r: r["sort_date"] or date.min, reverse=True)

    # Totals by org — одним groupby-запросом вместо цикла с 2 запросами на каждую орг
    totals_by_org = dict(
        db.query(Transaction.organization_id, func.sum(Transaction.amount))
        .filter(
            Transaction.organization_id.in_(visible_org_ids),
            Transaction.type == "expense",
            Transaction.deleted_at.is_(None),
        )
        .group_by(Transaction.organization_id)
        .all()
    )
    debt_by_org = dict(
        db.query(Transaction.organization_id, func.sum(Transaction.amount - Transaction.amount_paid))
        .filter(
            Transaction.organization_id.in_(visible_org_ids),
            Transaction.type == "expense",
            Transaction.deleted_at.is_(None),
            Transaction.amount_paid.isnot(None),
        )
        .group_by(Transaction.organization_id)
        .all()
    )
    totals = [
        {"org_name": org_map.get(org_id_t, "?"), "total": totals_by_org[org_id_t]}
        for org_id_t in visible_org_ids if totals_by_org.get(org_id_t)
    ]
    total_debt = sum(debt_by_org.values()) if debt_by_org else 0

    uncategorized_count = db.query(Transaction).filter(
        Transaction.organization_id.in_(visible_org_ids),
        Transaction.type == "expense",
        Transaction.deleted_at.is_(None),
        Transaction.category_id.is_(None),
    ).count()

    selected_month = month or datetime.now().strftime("%Y-%m")

    return templates.TemplateResponse("expenses/list.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org_id,
        "receipts": receipts,
        "categories": get_categories(db),
        "selected_category": category_id,
        "selected_month": selected_month,
        "total_debt": total_debt,
        "uncategorized_count": uncategorized_count,
        "totals": totals,
        "pending_count": pending_count,
    })


# ── UPLOAD FORM ───────────────────────────────────────────────────────────────

@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, org_id: int | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)

    return templates.TemplateResponse("expenses/upload.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "upload_orgs": get_upload_orgs(user, db),
        "error": None,
    })


@router.post("/upload")
async def handle_upload(
    request: Request,
    org_id: int = Form(...),
    receipt_org_id: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)

    file_bytes = await file.read()

    if not file_bytes:
        return templates.TemplateResponse("expenses/upload.html", {
            "request": request, "current_user": user,
            "accessible_orgs": accessible,
            "current_org_id": org_id,
            "upload_orgs": get_upload_orgs(user, db),
            "error": "Файл пустой. Попробуй ещё раз.",
        })

    file_hash = compute_hash(file_bytes)

    # Проверка дубля
    existing = db.query(Receipt).filter(Receipt.file_hash == file_hash).first()
    if existing:
        # Обновить объект если пользователь явно выбрал другой
        if existing.organization_id != receipt_org_id:
            existing.organization_id = receipt_org_id
            audit(db, "receipt", existing.id, "update", user.id, {"org_id": receipt_org_id})
        # Перезапустить OCR только если ещё не проведена
        if existing.ocr_status not in ("confirmed", "rejected"):
            ocr_result = analyze_receipt(str(MEDIA_DIR.parent / existing.file_path))
            if ocr_result:
                existing.ocr_raw = ocr_result.get("raw")
                existing.amount_detected = ocr_result.get("amount")
                existing.ocr_status = "processed"
                db.query(ReceiptItem).filter(ReceiptItem.receipt_id == existing.id).delete()
                for item in ocr_result.get("items") or []:
                    db.add(ReceiptItem(
                        receipt_id=existing.id,
                        name=item["name"],
                        qty=item.get("qty"),
                        unit_price=item.get("unit_price"),
                        total_price=item["total_price"],
                    ))
        db.commit()
        return RedirectResponse(f"/expenses/{existing.id}/confirm?org_id={org_id}", status_code=303)

    # Сохранение файла
    month_dir = MEDIA_DIR / datetime.now().strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename).suffix or ".jpg"
    fname = f"{file_hash[:12]}{suffix}"
    fpath = month_dir / fname
    fpath.write_bytes(file_bytes)
    rel_path = f"receipts/{datetime.now().strftime('%Y-%m')}/{fname}"

    # OCR
    ocr_result = analyze_receipt(str(fpath))

    receipt = Receipt(
        organization_id=receipt_org_id,
        file_path=rel_path,
        file_hash=file_hash,
        ocr_raw=ocr_result.get("raw") if ocr_result else None,
        amount_detected=ocr_result.get("amount") if ocr_result else None,
        ocr_status="processed" if ocr_result else "pending",
        created_by=user.id,
    )
    db.add(receipt)
    db.flush()

    if ocr_result:
        for item in ocr_result.get("items") or []:
            db.add(ReceiptItem(
                receipt_id=receipt.id,
                name=item["name"],
                qty=item.get("qty"),
                unit_price=item.get("unit_price"),
                total_price=item["total_price"],
            ))

    audit(db, "receipt", receipt.id, "insert", user.id, {"org_id": receipt_org_id})
    db.commit()
    db.refresh(receipt)

    return RedirectResponse(f"/expenses/{receipt.id}/confirm?org_id={org_id}", status_code=303)


# ── CONFIRM ───────────────────────────────────────────────────────────────────

@router.get("/{receipt_id}/confirm", response_class=HTMLResponse)
def confirm_form(
    receipt_id: int, request: Request,
    org_id: int | None = None, err: str | None = None, db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    receipt = db.query(Receipt).get(receipt_id)
    if not receipt:
        return HTMLResponse("Квитанция не найдена", status_code=404)

    org_map = {o.id: o.name for o in db.query(Organization).all()}
    current_org = resolve_org(org_id, user, db)
    raw_items = db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt_id).all()

    # AI-нормализация: сопоставить OCR-строки с эталонным каталогом
    ocr_payload = [
        {"name": it.name, "qty": it.qty, "unit_price": it.unit_price, "total_price": it.total_price}
        for it in raw_items
    ]
    normalized = normalize_items(db, ocr_payload) if ocr_payload else []

    items = []
    unmatched_count = 0
    for it, norm in zip(raw_items, normalized):
        exact = match_product(db, it.name)
        ai_pid = norm.get("matched_product_id")
        ai_name = norm.get("matched_name")
        match_type = norm.get("match_type", "none")     # ai_standard | ai_provisional | none
        ai_is_standard = norm.get("is_standard", False)
        ai_suggested = False

        if exact:
            # Точный alias → зелёный
            display_name = exact.name
            display_product_id = exact.id
            product_matched = True
            fuzzy_matched = False
            provisional_matched = False
        elif ai_pid and ai_is_standard:
            # AI нашёл эталонный → жёлтый
            display_name = ai_name
            display_product_id = ai_pid
            product_matched = False
            fuzzy_matched = True
            provisional_matched = False
        elif ai_pid and not ai_is_standard:
            # AI нашёл временный → оранжевый
            display_name = ai_name
            display_product_id = ai_pid
            product_matched = False
            fuzzy_matched = False
            provisional_matched = True
        else:
            # Нет в каталоге вообще → если товар читаемый, ИИ предлагает
            # стандартизированное имя (без бренда/сорта/веса) — человек проверяет/правит.
            suggested_name = norm.get("suggested_name")
            ai_suggested = bool(suggested_name)
            if not ai_suggested:
                # ИИ сам не считает эту строку товаром (см. normalize.py) — не
                # показываем как редактируемую позицию. Сумму НЕ переносим никуда:
                # сумма — всегда только кол-во × цена, введённые человеком; для
                # непризнанной строки таких чисел нет, значит и суммы нет. Если
                # это реальная покупка — человек допишет позицию сам, глядя на чек.
                unmatched_count += 1
                continue
            display_name = suggested_name
            display_product_id = None
            product_matched = False
            fuzzy_matched = False
            provisional_matched = False

        display_name = display_name[:1].upper() + display_name[1:] if display_name else display_name
        # unit: берём из продукта если найден, иначе пустая строка (пользователь укажет)
        is_standard_match = product_matched or fuzzy_matched
        matched_product = db.get(Product, display_product_id) if display_product_id else None
        display_unit = matched_product.unit if matched_product else ""
        # Для персонала — только 2 состояния: точный алиас = не трогать,
        # всё остальное (AI-догадка/временное/не найдено) = проверь глазами.
        # На период обучения намеренно строго: даже уверенная AI-догадка требует взгляда.
        needs_check = not product_matched
        # Сумма всегда считается из кол-во × цена — если одного из них нет,
        # позиция не готова, даже если OCR где-то распознал итоговое число.
        # Поле "Сумма" в этом случае остаётся пустым (не показываем сырой OCR-total
        # как будто он посчитан) — у человека есть сам чек на руках, доп. подсказка
        # с числом не нужна.
        missing_breakdown = it.qty is None or it.unit_price is None
        if missing_breakdown:
            check_hint = "впиши количество и цену — без этого позиция не считается заполненной"
            needs_check = True
        elif fuzzy_matched:
            check_hint = f"проверь — похоже на «{display_name}»"
        elif provisional_matched:
            check_hint = f"проверь — уже покупали как «{display_name}»"
        elif ai_suggested:
            check_hint = f"проверь — ИИ предлагает «{display_name}», в каталоге нет (OCR: «{it.name}»)"
        else:
            check_hint = ""
        items.append({
            "id": it.id,
            "raw_name": it.name,
            "display_name": display_name,
            "display_product_id": display_product_id,
            "product_matched": product_matched,
            "fuzzy_matched": fuzzy_matched,
            "provisional_matched": provisional_matched,
            "is_standard_match": is_standard_match,
            "needs_check": needs_check,
            "check_hint": check_hint,
            "unit": display_unit,
            "candidates": [],
            "qty": it.qty,
            "unit_price": it.unit_price,
            # Сумма на экране — ВСЕГДА кол-во × цена, никогда сырой OCR-total.
            # Иначе показанное число может разойтись с тем, что сервер реально
            # сохранит при "Провести" (он всегда пересчитывает сам).
            "total_price": None if missing_breakdown else round(float(it.qty) * float(it.unit_price), 2),
        })

    needs_check_count = sum(1 for it in items if it["needs_check"])

    # Для экрана успеха — подтянуть все транзакции чека (могут быть в нескольких категориях)
    confirmed_tx = None
    if receipt.ocr_status == "confirmed":
        rts = db.query(ReceiptTransaction).filter(ReceiptTransaction.receipt_id == receipt_id).all()
        txs = [db.query(Transaction).get(rt.transaction_id) for rt in rts]
        txs = [t for t in txs if t]
        if txs:
            cat_names = []
            for t in txs:
                cat = db.query(ExpenseCategory).get(t.category_id) if t.category_id else None
                name = cat.name if cat else "без категории"
                if name not in cat_names:
                    cat_names.append(name)
            confirmed_tx = {
                "amount": receipt.amount_confirmed,
                "date": txs[0].date.strftime("%d.%m.%Y") if txs[0].date else "—",
                "category_name": " + ".join(cat_names),
            }

    creator = db.get(User, receipt.created_by) if receipt.created_by else None
    all_suppliers = db.query(Supplier).order_by(Supplier.name).all()

    return templates.TemplateResponse("expenses/confirm.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else org_id,
        "upload_orgs": get_upload_orgs(user, db),
        "receipt": {
            "id": receipt.id,
            "file_path": receipt.file_path,
            "ocr_raw": receipt.ocr_raw,
            "amount_detected": receipt.amount_detected,
            "amount_confirmed": receipt.amount_confirmed,
            "ocr_status": receipt.ocr_status,
            "org_id": receipt.organization_id,
            "org_name": org_map.get(receipt.organization_id, "—"),
            "created_by_name": creator.name if creator else "—",
        },
        "items": items,
        "unmatched_count": unmatched_count,
        "suppliers": all_suppliers,
        "today": date.today().isoformat(),
        "confirmed_tx": confirmed_tx,
        "error": "Выбери поставщика — без него нельзя провести квитанцию" if err == "supplier" else "Укажи количество и цену для всех позиций" if err == "qty" else "Укажи единицу измерения для всех позиций" if err == "unit" else None,
        "success": None,
    })


@router.post("/{receipt_id}/confirm")
def handle_confirm(
    receipt_id: int,
    request: Request,
    org_id: int = Form(None),
    receipt_org_id: int = Form(None),
    action: str = Form(...),
    amount: float = Form(None),
    amount_paid: str = Form(None),
    due_date: str = Form(None),
    description: str = Form(None),
    date_: str = Form(None, alias="date"),
    item_name: List[str] = Form(default=[]),
    item_raw_name: List[str] = Form(default=[]),
    item_product_id: List[str] = Form(default=[]),
    item_unit: List[str] = Form(default=[]),
    item_qty: List[str] = Form(default=[]),
    item_unit_price: List[str] = Form(default=[]),
    add_to_warehouse: str = Form(default=""),
    supplier_id: str = Form(default=""),
    new_supplier_name: str = Form(default=""),
    new_supplier_phone: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    receipt = db.query(Receipt).get(receipt_id)
    if not receipt:
        return HTMLResponse("Квитанция не найдена", status_code=404)

    # Переназначить объект если выбран конкретный садик
    if receipt_org_id and receipt_org_id != receipt.organization_id:
        receipt.organization_id = receipt_org_id

    if action == "reject":
        receipt.ocr_status = "rejected"
        audit(db, "receipt", receipt.id, "update", user.id, {"status": "rejected"})
        db.commit()
        return RedirectResponse(f"/expenses/?org_id={org_id}", status_code=303)

    tx_date = date.fromisoformat(date_) if date_ else date.today()
    org_map = {o.id: o.name for o in db.query(Organization).all()}

    if not amount:
        accessible = get_accessible_orgs(user, db)
        items = db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt_id).all()
        return templates.TemplateResponse("expenses/confirm.html", {
            "request": request, "current_user": user,
            "accessible_orgs": accessible,
            "current_org_id": org_id,
            "receipt": {"id": receipt.id, "file_path": receipt.file_path,
                        "ocr_raw": receipt.ocr_raw, "amount_detected": receipt.amount_detected,
                        "amount_confirmed": receipt.amount_confirmed, "ocr_status": receipt.ocr_status,
                        "org_name": org_map.get(receipt.organization_id, "—")},
            "items": items,
            "today": date.today().isoformat(),
            "error": "Укажи сумму",
            "success": None,
        })

    def _parse_amount(s):
        try:
            return float(str(s).replace(",", ".")) if s else None
        except ValueError:
            return None

    def _safe_num(vals, i):
        try:
            v = vals[i].strip() if i < len(vals) else ""
            return float(v.replace(",", ".")) if v else None
        except (ValueError, AttributeError):
            return None

    amount_paid_val = _parse_amount(amount_paid)
    # Не оплачено полностью (частично или в долг). Если равно сумме или не указано —
    # считаем оплаченным полностью (NULL, историческое поведение).
    if amount_paid_val is not None and amount_paid_val >= amount:
        amount_paid_val = None
    due_date_val = date.fromisoformat(due_date) if due_date else None

    sid = resolve_supplier(db, supplier_id, new_supplier_name, new_supplier_phone)
    if not sid:
        return RedirectResponse(
            f"/expenses/{receipt_id}/confirm?org_id={org_id or ''}&err=supplier",
            status_code=303,
        )

    # Валидация + разрешение товара для каждой позиции — нужно ДО разбивки по категориям,
    # т.к. категория расхода теперь берётся из товара, а не выбирается человеком.
    resolved_items = []  # [{name, raw, product, qty, unit_price, total}, ...]
    for i, name in enumerate(item_name):
        name = name.strip()
        if not name:
            continue
        qty_val = _safe_num(item_qty, i)
        price_val = _safe_num(item_unit_price, i)
        # Сумма никогда не берётся из отправленного поля напрямую (его туда мог
        # вписать только JS-расчёт на глазах у человека) — сервер всегда сам
        # пересчитывает total = кол-во × цена. Нет кол-ва/цены — нет позиции,
        # заполнять руками обязательно, автоподстановки "1 шт." нет.
        if qty_val is None or price_val is None:
            return RedirectResponse(
                f"/expenses/{receipt_id}/confirm?org_id={org_id or ''}&err=qty",
                status_code=303,
            )
        total = round(qty_val * price_val, 2)
        unit_val = item_unit[i].strip() if i < len(item_unit) else ""
        if not unit_val:
            return RedirectResponse(
                f"/expenses/{receipt_id}/confirm?org_id={org_id or ''}&err=unit",
                status_code=303,
            )

        raw = item_raw_name[i].strip() if i < len(item_raw_name) else ""
        pid_str = item_product_id[i].strip() if i < len(item_product_id) else ""
        if pid_str and pid_str.isdigit():
            product = db.get(Product, int(pid_str))
            if not product:
                product = get_or_create_product(db, name)
        else:
            product = get_or_create_product(db, name)
        if raw:
            ensure_alias(db, raw, product.id)

        # Обновить unit для временных продуктов (у эталонов unit уже верный)
        if unit_val and not product.is_standard and product.unit != unit_val:
            product.unit = unit_val

        resolved_items.append({
            "name": name, "raw": raw, "product": product,
            "qty": qty_val, "unit_price": price_val, "total": total,
        })

    tx_by_cat = create_split_transactions(
        db, resolved_items, amount, amount_paid_val, due_date_val,
        receipt.organization_id, sid, description, tx_date, user.id,
        receipt_id=receipt.id,
    )
    main_tx_id = next(iter(tx_by_cat.values()), None)

    total_confirmed = amount
    receipt.ocr_status = "confirmed"
    receipt.amount_confirmed = total_confirmed
    receipt.confirmed_by = user.id
    receipt.confirmed_at = datetime.now()
    audit(db, "receipt", receipt.id, "update", user.id, {"status": "confirmed", "amount": total_confirmed})

    # Сохраняем отредактированные позиции
    want_warehouse = add_to_warehouse == "1"
    if resolved_items:
        db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt_id).delete()

        for it in resolved_items:
            product = it["product"]
            db.add(ReceiptItem(
                receipt_id=receipt_id,
                name=it["raw"] or it["name"],
                product_id=product.id,
                qty=it["qty"],
                unit_price=it["unit_price"],
                total_price=it["total"],
            ))
            db.flush()

            # Автоматически добавить в склад если есть количество и цена
            if want_warehouse and it["qty"] and it["qty"] > 0 and it["unit_price"] and it["unit_price"] > 0:
                db.add(WarehouseReceipt(
                    date=tx_date,
                    product_id=product.id,
                    quantity=it["qty"],
                    price_per_unit=it["unit_price"],
                    total_cost=round(it["qty"] * it["unit_price"], 2),
                    organization_id=receipt.organization_id,
                    transaction_id=tx_by_cat.get(product.expense_category_id, main_tx_id),
                    created_by=user.id,
                ))

    db.commit()

    return RedirectResponse(f"/expenses/{receipt_id}/confirm?org_id={org_id}", status_code=303)


# ── MANUAL ENTRY (без квитанции) ──────────────────────────────────────────────

@router.get("/add", response_class=HTMLResponse)
def add_form(request: Request, org_id: int | None = None, category_id: int | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)
    # «Закуп» (не сервис) — категория больше не выбирается вручную, определяется
    # по товару при сохранении (как в confirm.html после фото чека).
    service_category_id = get_service_category_id(db)
    all_suppliers = db.query(Supplier).order_by(Supplier.name).all()
    is_service_mode = bool(category_id) and category_id == service_category_id
    return templates.TemplateResponse("expenses/add.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "upload_orgs": get_upload_orgs(user, db),
        "suppliers": all_suppliers,
        "today": date.today().isoformat(),
        "preselect_category_id": category_id,
        "is_service_mode": is_service_mode,
        "error": None,
    })


@router.post("/add")
def handle_add(
    request: Request,
    org_id: int = Form(...),
    amount: float = Form(...),
    amount_paid: str = Form(None),
    due_date: str = Form(None),
    category_id: str | None = Form(None),
    description: str = Form(None),
    date_: str = Form(None, alias="date"),
    item_name: List[str] = Form(default=[]),
    item_qty: List[str] = Form(default=[]),
    item_unit_price: List[str] = Form(default=[]),
    item_unit: List[str] = Form(default=[]),
    item_product_id: List[str] = Form(default=[]),
    supplier_id: str = Form(default=""),
    new_supplier_name: str = Form(default=""),
    new_supplier_phone: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)
    category_id = int(category_id) if category_id and str(category_id).isdigit() else None
    service_category_id = get_service_category_id(db)
    is_service_mode = bool(category_id) and category_id == service_category_id

    def _error_response(message: str):
        return templates.TemplateResponse("expenses/add.html", {
            "request": request, "current_user": user,
            "accessible_orgs": accessible,
            "current_org_id": current_org.id if current_org else org_id,
            "upload_orgs": get_upload_orgs(user, db),
            "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
            "preselect_category_id": category_id,
            "is_service_mode": is_service_mode,
            "today": date.today().isoformat(),
            "error": message,
        })

    if not current_org:
        return _error_response("Объект не найден")

    tx_date = date.fromisoformat(date_) if date_ else date.today()
    # «Сервисные расходы» — без привязки к конкретному поставщику: таксисты,
    # курьеры и т.п. меняются от раза к разу, отслеживать их как поставщиков
    # с задолженностью не имеет смысла (решено 12.07)
    sid = None if is_service_mode else resolve_supplier(db, supplier_id, new_supplier_name, new_supplier_phone)

    if not sid and not is_service_mode:
        return _error_response("Выбери поставщика — без него нельзя провести расход")

    if is_service_mode and not (description or "").strip():
        return _error_response("Укажи в комментарии, за что заплатили — без поставщика это единственная зацепка")

    def _parse_amount(s):
        try:
            return float(str(s).replace(",", ".")) if s else None
        except ValueError:
            return None

    amount_paid_val = _parse_amount(amount_paid)
    if amount_paid_val is not None and amount_paid_val >= amount:
        amount_paid_val = None
    due_date_val = date.fromisoformat(due_date) if due_date else None

    if is_service_mode:
        # Категория зафиксирована (Сервисные расходы), позиций нет — не через
        # авто-разбивку по товару, единственная Transaction с ручной категорией.
        tx = Transaction(
            organization_id=org_id, type="expense", amount=amount,
            amount_paid=amount_paid_val, due_date=due_date_val,
            category_id=category_id, supplier_id=None, description=description, date=tx_date,
            created_by=user.id,
        )
        db.add(tx)
        db.flush()
        audit(db, "transaction", tx.id, "insert", user.id, {"org_id": org_id, "amount": amount, "manual": True})
        db.commit()
        return RedirectResponse(f"/expenses/?org_id={org_id}", status_code=303)

    # Закуп — категория больше не выбирается вручную, определяется по товару
    # каждой позиции (та же логика, что в confirm.html после фото чека).
    resolved_items, item_error = resolve_manual_items(
        db, item_name, item_qty, item_unit_price, item_unit, item_product_id,
    )
    if item_error:
        return _error_response(item_error)

    receipt_id = None
    if resolved_items:
        receipt = Receipt(
            organization_id=org_id,
            file_path="manual",
            ocr_status="manual",
            amount_confirmed=amount,
            confirmed_by=user.id,
            confirmed_at=datetime.now(),
            created_by=user.id,
        )
        db.add(receipt)
        db.flush()
        receipt_id = receipt.id

    tx_by_cat = create_split_transactions(
        db, resolved_items, amount, amount_paid_val, due_date_val,
        org_id, sid, description, tx_date, user.id, receipt_id=receipt_id,
    )
    # Пока принудительно включено, чекбокс на форме нельзя снять (решено 15.07) —
    # как только появится реальный выбор, читать add_to_warehouse из Form.
    create_warehouse_receipts(db, resolved_items, tx_by_cat, org_id, tx_date, user.id)

    if resolved_items:
        for it in resolved_items:
            db.add(ReceiptItem(
                receipt_id=receipt_id,
                name=it["name"],
                product_id=it["product"].id,
                qty=it["qty"],
                unit_price=it["unit_price"],
                total_price=it["total"],
            ))

    db.commit()
    return RedirectResponse(f"/expenses/?org_id={org_id}", status_code=303)


# ── RECURRING EXPENSES (справочник ежемесячных расходов: ФОТ/Охрана/Коммуналка) ─

@router.get("/recurring", response_class=HTMLResponse)
def recurring_list(request: Request, org_id: int | None = None, month: str | None = None,
                    db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)
    if not current_org:
        return RedirectResponse("/expenses/", status_code=302)

    # <input type="month"> отдаёт браузером "YYYY-MM" (не полную дату) — нормализуем к 1-му числу
    m_start = date.fromisoformat(f"{month[:7]}-01") if month else recurring_expenses.month_start()
    rows = recurring_expenses.list_for_month(db, current_org.id, user.role, m_start)

    return templates.TemplateResponse("expenses/recurring.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id,
        "rows": rows,
        "month": m_start.isoformat(),
        "month_label": m_start.strftime("%m.%Y"),
        "today": date.today().isoformat(),
    })


@router.post("/recurring/{template_id}/post")
def recurring_post(
    template_id: int,
    request: Request,
    org_id: int = Form(...),
    amount: float = Form(...),
    date_: str = Form(None, alias="date"),
    description: str = Form(None),
    month: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    tmpl = db.get(RecurringExpenseTemplate, template_id)
    if not tmpl or not tmpl.active or tmpl.organization_id != org_id:
        return RedirectResponse(f"/expenses/recurring?org_id={org_id}&month={month}", status_code=303)
    if tmpl.owner_only and user.role not in ("owner", "founder"):
        return RedirectResponse("/expenses/", status_code=302)
    if amount <= 0:
        return RedirectResponse(f"/expenses/recurring?org_id={org_id}&month={month}", status_code=303)
    # Счёт/начисление приходит раз в месяц — повторное проведение заблокировано:
    # сначала удали или исправь то, что уже провели (решено 13.07, распространено
    # на ФОТ 14.07 — это агрегированная сумма по всем сотрудникам, а не выплата
    # одному человеку, деления на аванс/остаток на уровне этой проводки нет)
    already_posted = recurring_expenses.month_postings(db, tmpl, date.fromisoformat(month))
    if already_posted:
        return RedirectResponse(f"/expenses/recurring?org_id={org_id}&month={month}", status_code=303)

    tx_date = date.fromisoformat(date_) if date_ else date.today()
    tx = Transaction(
        organization_id=tmpl.organization_id, type="expense", amount=amount,
        category_id=tmpl.category_id, description=description or tmpl.name,
        date=tx_date, period=date.fromisoformat(month), recurring_template_id=tmpl.id, created_by=user.id,
    )
    db.add(tx)
    db.flush()
    audit(db, "transaction", tx.id, "insert", user.id, {"recurring": tmpl.name, "amount": amount})
    db.commit()
    return RedirectResponse(f"/expenses/recurring?org_id={org_id}&month={month}", status_code=303)


@router.post("/tx/{tx_id}/delete")
def delete_tx(tx_id: int, request: Request, org_id: int = Form(...), month: str | None = Form(None), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tx = db.query(Transaction).get(tx_id)
    if not tx or tx.deleted_at:
        return RedirectResponse(f"/expenses/?org_id={org_id}", status_code=302)
    if tx.recurring_template_id:
        template = db.query(RecurringExpenseTemplate).get(tx.recurring_template_id)
        if template and template.amount_source == "employees_sum":
            # ФОТ — тот же принцип, что и с правкой: не удаляем проводку
            # напрямую, источник истины — оклады сотрудников
            return RedirectResponse(f"/employees/?org_id={org_id}", status_code=302)
    tx.deleted_at = datetime.utcnow()
    audit(db, "transaction", tx.id, "delete", user.id, {"amount": float(tx.amount)})
    db.commit()
    if tx.recurring_template_id:
        m = month or (tx.period.isoformat() if tx.period else recurring_expenses.month_start().isoformat())
        return RedirectResponse(f"/expenses/recurring?org_id={org_id}&month={m}", status_code=303)
    return RedirectResponse(f"/expenses/?org_id={org_id}", status_code=303)


# ── EDIT MANUAL TRANSACTION ───────────────────────────────────────────────────

@router.get("/tx/{tx_id}/edit", response_class=HTMLResponse)
def edit_tx_form(tx_id: int, request: Request, org_id: int | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tx = db.query(Transaction).get(tx_id)
    if not tx or tx.deleted_at:
        return HTMLResponse("Запись не найдена", status_code=404)
    if tx.recurring_template_id:
        template = db.query(RecurringExpenseTemplate).get(tx.recurring_template_id)
        if template and template.amount_source == "employees_sum":
            # ФОТ считается от окладов сотрудников — проводку саму не
            # правим, источник истины для суммы — их зарплаты
            return RedirectResponse(f"/employees/?org_id={org_id or tx.organization_id}", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)

    # Загружаем позиции если есть связанная квитанция
    items = []
    rt = db.query(ReceiptTransaction).filter(ReceiptTransaction.transaction_id == tx_id).first()
    if rt:
        raw_items = db.query(ReceiptItem).filter(ReceiptItem.receipt_id == rt.receipt_id).all()
        for it in raw_items:
            product = db.query(Product).get(it.product_id) if it.product_id else None
            items.append({
                "name": product.name if product else it.name,
                "qty": it.qty,
                "unit_price": it.unit_price,
                "total_price": it.total_price,
            })

    return templates.TemplateResponse("expenses/edit_tx.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else org_id,
        "upload_orgs": get_upload_orgs(user, db),
        "tx": {
            "id": tx.id,
            "amount": tx.amount,
            "amount_paid": tx.amount_paid,
            "due_date": tx.due_date.isoformat() if tx.due_date else "",
            "debt": (tx.amount - tx.amount_paid) if tx.amount_paid is not None else None,
            "date": tx.date.isoformat() if tx.date else date.today().isoformat(),
            "category_id": tx.category_id,
            "category_name": db.query(ExpenseCategory).get(tx.category_id).name if tx.category_id else None,
            "description": tx.description or "",
            "org_id": tx.organization_id,
            "org_name": db.query(Organization).get(tx.organization_id).name,
        },
        "items": items,
        "categories": get_categories(db),
        "error": None,
    })


@router.post("/tx/{tx_id}/edit")
def handle_edit_tx(
    tx_id: int,
    request: Request,
    org_id: int = Form(None),
    amount: float = Form(...),
    amount_paid: str = Form(None),
    due_date: str = Form(None),
    category_id: str | None = Form(None),
    description: str = Form(None),
    date_: str = Form(None, alias="date"),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tx = db.query(Transaction).get(tx_id)
    if not tx or tx.deleted_at:
        return HTMLResponse("Запись не найдена", status_code=404)
    if tx.recurring_template_id:
        template = db.query(RecurringExpenseTemplate).get(tx.recurring_template_id)
        if template and template.amount_source == "employees_sum":
            return RedirectResponse(f"/employees/?org_id={org_id or tx.organization_id}", status_code=303)

    def _parse_amount(s):
        try:
            return float(str(s).replace(",", ".")) if s else None
        except ValueError:
            return None

    amount_paid_val = _parse_amount(amount_paid)
    if amount_paid_val is not None and amount_paid_val >= amount:
        amount_paid_val = None

    tx.amount = amount
    tx.amount_paid = amount_paid_val
    tx.due_date = date.fromisoformat(due_date) if due_date else None
    tx.category_id = int(category_id) if category_id and str(category_id).isdigit() else None
    tx.description = description
    tx.date = date.fromisoformat(date_) if date_ else tx.date
    audit(db, "transaction", tx.id, "update", user.id, {"amount": amount})
    db.commit()
    return RedirectResponse(f"/expenses/?org_id={org_id or tx.organization_id}", status_code=303)


# ── EDIT MANUAL RECEIPT WITH ITEMS (закуп без фото, разбит по категориям) ─────
# Позиции определяют категорию/разбивку — значит редактирование не может просто
# поправить одну Transaction, как для записей без позиций (см. edit_tx.html выше).
# Правка = та же форма add.html, предзаполненная; при сохранении старые Transaction
# этого чека мягко удаляются (deleted_at + audit "delete"), новые создаются заново
# той же функцией, что и при вводе — числа всегда считаются с нуля, не патчатся
# по кускам (решено 15.07, см. обсуждение в сессии).

@router.get("/{receipt_id}/edit-manual", response_class=HTMLResponse)
def edit_manual_form(receipt_id: int, request: Request, org_id: int | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    receipt = db.query(Receipt).get(receipt_id)
    if not receipt or receipt.file_path != "manual":
        return HTMLResponse("Запись не найдена", status_code=404)

    txs = (
        db.query(Transaction)
        .join(ReceiptTransaction, ReceiptTransaction.transaction_id == Transaction.id)
        .filter(ReceiptTransaction.receipt_id == receipt_id, Transaction.deleted_at.is_(None))
        .order_by(Transaction.id)
        .all()
    )
    if not txs:
        return HTMLResponse("Запись не найдена", status_code=404)

    first_tx = txs[0]
    total_amount = float(receipt.amount_confirmed) if receipt.amount_confirmed is not None else sum(float(t.amount) for t in txs)
    # amount_paid=None у отдельной Transaction означает «эта доля оплачена полностью» —
    # чтобы восстановить общую картину, недостающую долю считаем как amount той группы.
    total_paid = sum(float(t.amount_paid) if t.amount_paid is not None else float(t.amount) for t in txs)
    due_date_val = next((t.due_date for t in txs if t.due_date), None)

    accessible = get_accessible_orgs(user, db)

    raw_items = db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt_id).all()
    items = []
    for it in raw_items:
        product = db.get(Product, it.product_id) if it.product_id else None
        items.append({
            "name": product.name if product else it.name,
            "unit": product.unit if product else "",
            "qty": it.qty,
            "unit_price": it.unit_price,
            "product_id": it.product_id,
        })

    return templates.TemplateResponse("expenses/add.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": first_tx.organization_id,
        "upload_orgs": get_upload_orgs(user, db),
        "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
        "today": date.today().isoformat(),
        "preselect_category_id": None,
        "is_service_mode": False,
        "error": None,
        "edit_receipt_id": receipt.id,
        "edit_supplier_id": first_tx.supplier_id,
        "edit_description": first_tx.description or "",
        "edit_amount": total_amount,
        "edit_amount_paid": total_paid if total_paid < total_amount - 0.009 else None,
        "edit_due_date": due_date_val.isoformat() if due_date_val else "",
        "edit_date": first_tx.date.isoformat() if first_tx.date else date.today().isoformat(),
        "edit_items": items,
    })


@router.post("/{receipt_id}/edit-manual")
def handle_edit_manual(
    receipt_id: int,
    request: Request,
    org_id: int = Form(...),
    amount: float = Form(...),
    amount_paid: str = Form(None),
    due_date: str = Form(None),
    description: str = Form(None),
    date_: str = Form(None, alias="date"),
    item_name: List[str] = Form(default=[]),
    item_qty: List[str] = Form(default=[]),
    item_unit_price: List[str] = Form(default=[]),
    item_unit: List[str] = Form(default=[]),
    item_product_id: List[str] = Form(default=[]),
    supplier_id: str = Form(default=""),
    new_supplier_name: str = Form(default=""),
    new_supplier_phone: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    receipt = db.query(Receipt).get(receipt_id)
    if not receipt or receipt.file_path != "manual":
        return HTMLResponse("Запись не найдена", status_code=404)

    def _error_response(message: str):
        raw_items = db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt_id).all()
        items = []
        for it in raw_items:
            product = db.get(Product, it.product_id) if it.product_id else None
            items.append({
                "name": product.name if product else it.name,
                "unit": product.unit if product else "",
                "qty": it.qty, "unit_price": it.unit_price, "product_id": it.product_id,
            })
        return templates.TemplateResponse("expenses/add.html", {
            "request": request, "current_user": user,
            "accessible_orgs": get_accessible_orgs(user, db),
            "current_org_id": org_id,
            "upload_orgs": get_upload_orgs(user, db),
            "suppliers": db.query(Supplier).order_by(Supplier.name).all(),
            "today": date.today().isoformat(),
            "preselect_category_id": None,
            "is_service_mode": False,
            "error": message,
            "edit_receipt_id": receipt.id,
            "edit_supplier_id": int(supplier_id) if supplier_id.isdigit() else None,
            "edit_description": description or "",
            "edit_amount": amount,
            "edit_amount_paid": None,
            "edit_due_date": due_date or "",
            "edit_date": date_ or date.today().isoformat(),
            "edit_items": items,
        })

    tx_date = date.fromisoformat(date_) if date_ else date.today()
    sid = resolve_supplier(db, supplier_id, new_supplier_name, new_supplier_phone)
    if not sid:
        return _error_response("Выбери поставщика — без него нельзя провести расход")

    def _parse_amount(s):
        try:
            return float(str(s).replace(",", ".")) if s else None
        except ValueError:
            return None

    amount_paid_val = _parse_amount(amount_paid)
    if amount_paid_val is not None and amount_paid_val >= amount:
        amount_paid_val = None
    due_date_val = date.fromisoformat(due_date) if due_date else None

    resolved_items, item_error = resolve_manual_items(
        db, item_name, item_qty, item_unit_price, item_unit, item_product_id,
    )
    if item_error:
        return _error_response(item_error)

    # Старые Transaction этого чека — мягко удалить (не потерять историю), затем
    # снять старые связки/позиции и создать всё заново той же логикой, что и ввод.
    old_txs = (
        db.query(Transaction)
        .join(ReceiptTransaction, ReceiptTransaction.transaction_id == Transaction.id)
        .filter(ReceiptTransaction.receipt_id == receipt_id, Transaction.deleted_at.is_(None))
        .all()
    )
    old_tx_ids = [t.id for t in old_txs]
    for old_tx in old_txs:
        old_tx.deleted_at = datetime.utcnow()
        audit(db, "transaction", old_tx.id, "delete", user.id, {"amount": float(old_tx.amount), "reason": "edit-manual"})
    # Старые складские поступления этих проводок — тоже мягко удалить, иначе
    # при правке количества остатки на складе задвоятся (старое + новое).
    if old_tx_ids:
        db.query(WarehouseReceipt).filter(
            WarehouseReceipt.transaction_id.in_(old_tx_ids),
            WarehouseReceipt.deleted_at.is_(None),
        ).update({"deleted_at": datetime.utcnow()}, synchronize_session=False)
    db.query(ReceiptTransaction).filter(ReceiptTransaction.receipt_id == receipt_id).delete()
    db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt_id).delete()

    tx_by_cat = create_split_transactions(
        db, resolved_items, amount, amount_paid_val, due_date_val,
        org_id, sid, description, tx_date, user.id, receipt_id=receipt_id,
    )
    create_warehouse_receipts(db, resolved_items, tx_by_cat, org_id, tx_date, user.id)
    for it in resolved_items:
        db.add(ReceiptItem(
            receipt_id=receipt_id,
            name=it["name"],
            product_id=it["product"].id,
            qty=it["qty"],
            unit_price=it["unit_price"],
            total_price=it["total"],
        ))

    receipt.amount_confirmed = amount
    audit(db, "receipt", receipt.id, "update", user.id, {"amount": amount})
    db.commit()
    return RedirectResponse(f"/expenses/?org_id={org_id}", status_code=303)


# ── DELETE RECEIPT ─────────────────────────────────────────────────────────────

@router.post("/{receipt_id}/delete")
def delete_receipt(
    receipt_id: int,
    request: Request,
    org_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    receipt = db.query(Receipt).get(receipt_id)
    if not receipt:
        return HTMLResponse("Квитанция не найдена", status_code=404)

    # Удалить связанные транзакции и склад — порядок важен: сначала снять
    # ReceiptTransaction (FK на transaction_id), иначе удаление Transaction
    # падает нарушением внешнего ключа (баг был всегда, просто не всплывал —
    # ни разу не удаляли квитанцию с уже проведённой транзакцией)
    rts = db.query(ReceiptTransaction).filter(ReceiptTransaction.receipt_id == receipt_id).all()
    tx_ids = [rt.transaction_id for rt in rts]
    if tx_ids:
        db.query(WarehouseReceipt).filter(WarehouseReceipt.transaction_id.in_(tx_ids)).delete(synchronize_session=False)
    db.query(ReceiptTransaction).filter(ReceiptTransaction.receipt_id == receipt_id).delete()
    if tx_ids:
        db.query(Transaction).filter(Transaction.id.in_(tx_ids)).delete(synchronize_session=False)
    db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt_id).delete()

    # Удалить файл фото
    if receipt.file_path and receipt.file_path != "manual":
        photo = MEDIA_DIR.parent / receipt.file_path
        if photo.exists():
            photo.unlink()

    audit(db, "receipt", receipt.id, "delete", user.id, {})
    db.delete(receipt)
    db.commit()

    return RedirectResponse(f"/expenses/?org_id={org_id or ''}", status_code=303)
