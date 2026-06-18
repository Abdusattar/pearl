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
    Transaction, User, AuditLog, WarehouseReceipt,
)
from app.services.ocr import compute_hash, analyze_receipt
from app.services.products import match_product, get_or_create_product, ensure_alias
from app.dependencies import get_current_user

router = APIRouter(prefix="/expenses", tags=["expenses"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

MEDIA_DIR = Path(__file__).parent.parent.parent / "media" / "receipts"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

ORG_KINDERGARTENS = 3


def get_accessible_orgs(user: User, db: Session) -> list[Organization]:
    """Орги доступные пользователю по роли."""
    all_orgs = db.query(Organization).all()
    if user.role == "owner":
        return all_orgs
    if user.role == "director":
        return all_orgs  # видит всё, но фильтр применяется при запросах
    if user.role == "manager":
        # Мунара видит только садики и их детей
        kinder_ids = {ORG_KINDERGARTENS}
        kinder_ids |= {o.id for o in all_orgs if o.parent_id == ORG_KINDERGARTENS}
        return [o for o in all_orgs if o.id in kinder_ids]
    return [o for o in all_orgs if o.id == user.organization_id]


def get_upload_orgs(user: User, db: Session) -> list[Organization]:
    """Только листовые орги (без родительских узлов) — куда можно загружать расходы."""
    all_orgs = db.query(Organization).all()
    has_children = {o.parent_id for o in all_orgs if o.parent_id is not None}
    orgs = get_accessible_orgs(user, db)
    return [o for o in orgs if o.id not in has_children]


def resolve_org(org_id: int | None, user: User, db: Session) -> Organization:
    accessible = get_accessible_orgs(user, db)
    if org_id:
        org = next((o for o in accessible if o.id == org_id), None)
        if org:
            return org
    return accessible[0] if accessible else None


def get_categories(db: Session) -> list[ExpenseCategory]:
    return db.query(ExpenseCategory).order_by(
        ExpenseCategory.parent_id.nullsfirst(), ExpenseCategory.name
    ).all()


def audit(db: Session, entity_type: str, entity_id: int, action: str,
          user_id: int, new_data: dict = None):
    db.add(AuditLog(
        entity_type=entity_type, entity_id=entity_id,
        action=action, user_id=user_id, new_data=new_data,
    ))


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

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)
    current_org_id = current_org.id if current_org else None

    # Доступные org_id для фильтрации
    if user.role == "manager":
        visible_org_ids = [o.id for o in accessible]
    elif current_org_id:
        # Показываем текущую орг и её детей
        all_orgs = db.query(Organization).all()
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

    org_map = {o.id: o.name for o in db.query(Organization).all()}
    cat_map = {}

    def _cat_name(category_id_val):
        if not category_id_val:
            return None
        if category_id_val not in cat_map:
            c = db.query(ExpenseCategory).get(category_id_val)
            cat_map[category_id_val] = c.name if c else None
        return cat_map[category_id_val]

    receipts = []
    for r in receipts_raw:
        rt = db.query(ReceiptTransaction).filter(ReceiptTransaction.receipt_id == r.id).first()
        tx_cat = None
        tx_id = None
        if rt:
            tx = db.query(Transaction).get(rt.transaction_id)
            if tx:
                tx_cat = _cat_name(tx.category_id)
                tx_id = tx.id
        if r.ocr_status == "manual":
            href = f"/expenses/tx/{tx_id}/edit?org_id={current_org_id}" if tx_id else None
        else:
            href = f"/expenses/{r.id}/confirm?org_id={current_org_id}"
        receipts.append({
            "row_type": "receipt",
            "id": r.id,
            "tx_id": tx_id,
            "href": href,
            "sort_date": r.created_at.date() if hasattr(r.created_at, 'date') else r.created_at,
            "date_display": r.created_at.strftime('%d.%m.%Y'),
            "org_name": org_map.get(r.organization_id, "—"),
            "category_name": tx_cat,
            "amount_detected": r.amount_detected,
            "amount_confirmed": r.amount_confirmed,
            "ocr_status": r.ocr_status,
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
            receipts.append({
                "row_type": "manual",
                "id": None,
                "tx_id": tx.id,
                "href": f"/expenses/tx/{tx.id}/edit?org_id={current_org_id}",
                "sort_date": tx.date,
                "date_display": tx.date.strftime('%d.%m.%Y') if tx.date else "—",
                "org_name": org_map.get(tx.organization_id, "—"),
                "category_name": _cat_name(tx.category_id),
                "amount_detected": None,
                "amount_confirmed": tx.amount,
                "ocr_status": "manual",
            })
        receipts.sort(key=lambda r: r["sort_date"] or date.min, reverse=True)

    # Totals by org
    totals = []
    for org_id_t in visible_org_ids:
        total = db.query(func.sum(Transaction.amount)).filter(
            Transaction.organization_id == org_id_t,
            Transaction.type == "expense",
            Transaction.deleted_at.is_(None),
        ).scalar()
        if total:
            totals.append({"org_name": org_map.get(org_id_t, "?"), "total": total})

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
        "totals": totals,
        "pending_count": pending_count,
    })


# ── UPLOAD FORM ───────────────────────────────────────────────────────────────

@router.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request, org_id: int | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
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
    org_id: int | None = None, db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    accessible = get_accessible_orgs(user, db)
    receipt = db.query(Receipt).get(receipt_id)
    if not receipt:
        return HTMLResponse("Квитанция не найдена", status_code=404)

    org_map = {o.id: o.name for o in db.query(Organization).all()}
    current_org = resolve_org(org_id, user, db)
    raw_items = db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt_id).all()

    items = []
    for it in raw_items:
        matched = match_product(db, it.name)
        items.append({
            "id": it.id,
            "raw_name": it.name,
            "display_name": matched.name if matched else it.name,
            "product_matched": matched is not None,
            "qty": it.qty,
            "unit_price": it.unit_price,
            "total_price": it.total_price,
        })

    # Предзаполнить категорию из уже существующей транзакции (если есть)
    pre_category_id = None
    existing_rt = db.query(ReceiptTransaction).filter(ReceiptTransaction.receipt_id == receipt_id).first()
    if existing_rt and receipt.ocr_status not in ("confirmed", "rejected"):
        existing_tx = db.query(Transaction).get(existing_rt.transaction_id)
        if existing_tx:
            pre_category_id = existing_tx.category_id

    # Для экрана успеха — подтянуть транзакцию
    confirmed_tx = None
    if receipt.ocr_status == "confirmed":
        rt = db.query(ReceiptTransaction).filter(ReceiptTransaction.receipt_id == receipt_id).first()
        if rt:
            tx = db.query(Transaction).get(rt.transaction_id)
            if tx:
                cat = db.query(ExpenseCategory).get(tx.category_id) if tx.category_id else None
                confirmed_tx = {
                    "amount": tx.amount,
                    "date": tx.date.strftime("%d.%m.%Y") if tx.date else "—",
                    "category_name": cat.name if cat else "—",
                }

    all_cats = get_categories(db)
    food_parent_ids = {c.id for c in all_cats if 'питан' in c.name.lower()}
    food_cat_ids = list(food_parent_ids | {c.id for c in all_cats if c.parent_id in food_parent_ids})
    warehouse_cat_ids = [c.id for c in all_cats if c.warehouse_eligible]

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
        },
        "items": items,
        "categories": all_cats,
        "food_cat_ids": food_cat_ids,
        "warehouse_cat_ids": warehouse_cat_ids,
        "today": date.today().isoformat(),
        "confirmed_tx": confirmed_tx,
        "pre_category_id": pre_category_id,
        "error": None,
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
    category_id: str | None = Form(None),
    description: str = Form(None),
    date_: str = Form(None, alias="date"),
    item_name: List[str] = Form(default=[]),
    item_raw_name: List[str] = Form(default=[]),
    item_qty: List[str] = Form(default=[]),
    item_unit_price: List[str] = Form(default=[]),
    item_total_price: List[str] = Form(default=[]),
    split_category_id: List[str] = Form(default=[]),
    split_amount: List[str] = Form(default=[]),
    add_to_warehouse: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    receipt = db.query(Receipt).get(receipt_id)
    if not receipt:
        return HTMLResponse("Квитанция не найдена", status_code=404)

    category_id = int(category_id) if category_id and str(category_id).isdigit() else None
    split_category_id = [int(x) for x in split_category_id if x and str(x).isdigit()]

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
            "categories": get_categories(db),
            "today": date.today().isoformat(),
            "error": "Укажи сумму",
            "success": None,
        })

    def _parse_amount(s):
        try:
            return float(str(s).replace(",", ".")) if s else None
        except ValueError:
            return None

    splits = [
        (cat_id, _parse_amount(amt))
        for cat_id, amt in zip(split_category_id, split_amount)
        if cat_id and _parse_amount(amt)
    ]

    main_tx_id = None
    if splits:
        total_confirmed = sum(amt for _, amt in splits)
        for cat_id, amt in splits:
            tx = Transaction(
                organization_id=receipt.organization_id, type="expense", amount=amt,
                category_id=cat_id, description=description, date=tx_date,
                created_by=user.id,
            )
            db.add(tx)
            db.flush()
            if main_tx_id is None:
                main_tx_id = tx.id
            db.add(ReceiptTransaction(receipt_id=receipt.id, transaction_id=tx.id, amount=amt))
            audit(db, "transaction", tx.id, "insert", user.id, {"org_id": receipt.organization_id, "amount": amt})
    else:
        tx = Transaction(
            organization_id=receipt.organization_id, type="expense", amount=amount,
            category_id=category_id, description=description, date=tx_date,
            created_by=user.id,
        )
        db.add(tx)
        db.flush()
        main_tx_id = tx.id
        db.add(ReceiptTransaction(receipt_id=receipt.id, transaction_id=tx.id, amount=amount))
        audit(db, "transaction", tx.id, "insert", user.id, {"org_id": receipt.organization_id, "amount": amount})
        total_confirmed = amount

    receipt.ocr_status = "confirmed"
    receipt.amount_confirmed = total_confirmed
    receipt.confirmed_by = user.id
    receipt.confirmed_at = datetime.now()
    audit(db, "receipt", receipt.id, "update", user.id, {"status": "confirmed", "amount": total_confirmed})

    # Сохраняем отредактированные позиции (если были переданы)
    want_warehouse = add_to_warehouse == "1"
    if item_name:
        db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt_id).delete()
        def _safe_num(vals, i):
            try:
                v = vals[i].strip() if i < len(vals) else ""
                return float(v.replace(",", ".")) if v else None
            except (ValueError, AttributeError):
                return None

        for i, name in enumerate(item_name):
            name = name.strip()
            if not name:
                continue
            total = _safe_num(item_total_price, i)
            if total is None:
                continue

            raw = item_raw_name[i].strip() if i < len(item_raw_name) else ""
            product = get_or_create_product(db, name)
            if raw:
                ensure_alias(db, raw, product.id)

            qty_val = _safe_num(item_qty, i)
            price_val = _safe_num(item_unit_price, i)

            db.add(ReceiptItem(
                receipt_id=receipt_id,
                name=raw or name,
                product_id=product.id,
                qty=qty_val,
                unit_price=price_val,
                total_price=total,
            ))

            # Автоматически добавить в склад если есть количество и цена
            if want_warehouse and qty_val and qty_val > 0 and price_val and price_val > 0:
                db.add(WarehouseReceipt(
                    date=tx_date,
                    product_id=product.id,
                    quantity=qty_val,
                    price_per_unit=price_val,
                    total_cost=round(qty_val * price_val, 2),
                    organization_id=receipt.organization_id,
                    transaction_id=main_tx_id,
                    created_by=user.id,
                ))

    db.commit()

    return RedirectResponse(f"/expenses/{receipt_id}/confirm?org_id={org_id}", status_code=303)


# ── MANUAL ENTRY (без квитанции) ──────────────────────────────────────────────

@router.get("/add", response_class=HTMLResponse)
def add_form(request: Request, org_id: int | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)
    from app.models import Product
    products = db.query(Product).order_by(Product.name).all()
    all_cats = get_categories(db)
    food_parent_ids = {c.id for c in all_cats if 'питан' in c.name.lower()}
    food_cat_ids = list(food_parent_ids | {c.id for c in all_cats if c.parent_id in food_parent_ids})
    return templates.TemplateResponse("expenses/add.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "upload_orgs": get_upload_orgs(user, db),
        "categories": all_cats,
        "food_cat_ids": food_cat_ids,
        "products": products,
        "today": date.today().isoformat(),
        "error": None,
    })


@router.post("/add")
def handle_add(
    request: Request,
    org_id: int = Form(...),
    amount: float = Form(...),
    category_id: str | None = Form(None),
    description: str = Form(None),
    date_: str = Form(None, alias="date"),
    item_name: List[str] = Form(default=[]),
    item_qty: List[str] = Form(default=[]),
    item_unit_price: List[str] = Form(default=[]),
    item_total_price: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)
    category_id = int(category_id) if category_id and str(category_id).isdigit() else None

    if not current_org:
        return templates.TemplateResponse("expenses/add.html", {
            "request": request, "current_user": user,
            "accessible_orgs": accessible,
            "current_org_id": org_id,
            "upload_orgs": get_upload_orgs(user, db),
            "categories": get_categories(db),
            "today": date.today().isoformat(),
            "error": "Объект не найден",
        })

    tx_date = date.fromisoformat(date_) if date_ else date.today()
    tx = Transaction(
        organization_id=org_id, type="expense", amount=amount,
        category_id=category_id, description=description, date=tx_date,
        created_by=user.id,
    )
    db.add(tx)
    db.flush()
    audit(db, "transaction", tx.id, "insert", user.id, {"org_id": org_id, "amount": amount, "manual": True})

    # Сохраняем позиции если были введены (для справочника продуктов)
    def _safe(vals, i):
        try:
            v = vals[i].strip() if i < len(vals) else ""
            return float(v.replace(",", ".")) if v else None
        except (ValueError, AttributeError):
            return None

    valid_items = [
        (item_name[i].strip(), _safe(item_qty, i), _safe(item_unit_price, i), _safe(item_total_price, i))
        for i in range(len(item_name))
        if item_name[i].strip() and _safe(item_total_price, i)
    ]

    if valid_items:
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
        db.add(ReceiptTransaction(receipt_id=receipt.id, transaction_id=tx.id, amount=amount))
        for name, qty, unit_price, total in valid_items:
            product = get_or_create_product(db, name)
            db.add(ReceiptItem(
                receipt_id=receipt.id,
                name=name,
                product_id=product.id,
                qty=qty,
                unit_price=unit_price,
                total_price=total,
            ))

    db.commit()
    return RedirectResponse(f"/expenses/?org_id={org_id}", status_code=303)


# ── EDIT MANUAL TRANSACTION ───────────────────────────────────────────────────

@router.get("/tx/{tx_id}/edit", response_class=HTMLResponse)
def edit_tx_form(tx_id: int, request: Request, org_id: int | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    tx = db.query(Transaction).get(tx_id)
    if not tx or tx.deleted_at:
        return HTMLResponse("Запись не найдена", status_code=404)
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
            "date": tx.date.isoformat() if tx.date else date.today().isoformat(),
            "category_id": tx.category_id,
            "description": tx.description or "",
            "org_id": tx.organization_id,
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
    category_id: str | None = Form(None),
    description: str = Form(None),
    date_: str = Form(None, alias="date"),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    tx = db.query(Transaction).get(tx_id)
    if not tx or tx.deleted_at:
        return HTMLResponse("Запись не найдена", status_code=404)

    tx.amount = amount
    tx.category_id = int(category_id) if category_id and str(category_id).isdigit() else None
    tx.description = description
    tx.date = date.fromisoformat(date_) if date_ else tx.date
    audit(db, "transaction", tx.id, "update", user.id, {"amount": amount})
    db.commit()
    return RedirectResponse(f"/expenses/?org_id={org_id or tx.organization_id}", status_code=303)
