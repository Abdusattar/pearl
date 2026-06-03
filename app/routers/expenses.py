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
    ExpenseCategory, Organization, Receipt, ReceiptItem, ReceiptTransaction,
    Transaction, User, AuditLog,
)
from app.services.ocr import compute_hash, analyze_receipt

router = APIRouter(prefix="/expenses", tags=["expenses"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

MEDIA_DIR = Path(__file__).parent.parent.parent / "media" / "receipts"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# ORG IDs — matches seed data
ORG_KINDERGARTENS = 3  # "Садики" — родительский узел


def get_mock_user(db: Session) -> User:
    """Временно — возвращает первого пользователя. Заменить на auth."""
    return db.query(User).filter(User.deleted_at.is_(None)).first()


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
    """Объекты доступные для загрузки квитанций (листовые + 'Оба садика')."""
    orgs = get_accessible_orgs(user, db)
    # Для manager — Сокулук, Кожомкул, + "Оба садика" (id=3)
    if user.role == "manager":
        return orgs
    # Для director — убрать корневую Жемчужину
    if user.role == "director":
        return [o for o in orgs if o.parent_id is not None]
    return [o for o in orgs if o.parent_id is not None]


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
    org_id: int | None = None,
    category_id: int | None = None,
    month: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
):
    user = get_mock_user(db)
    if not user:
        return HTMLResponse("Нет пользователей в БД. Запустите: python app/seed.py", status_code=503)

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

    q = db.query(Receipt).filter(Receipt.organization_id.in_(visible_org_ids))

    if category_id:
        # Фильтр по категории через транзакции
        tx_receipt_ids = (
            db.query(ReceiptTransaction.receipt_id)
            .join(Transaction, Transaction.id == ReceiptTransaction.transaction_id)
            .filter(Transaction.category_id == category_id)
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
        q = q.filter(Receipt.ocr_status == status)

    receipts_raw = q.order_by(Receipt.created_at.desc()).limit(200).all()
    pending_count = db.query(Receipt).filter(
        Receipt.organization_id.in_(visible_org_ids),
        Receipt.ocr_status.in_(["pending", "processed"]),
    ).count()

    # Enrich receipts with display data
    org_map = {o.id: o.name for o in db.query(Organization).all()}
    cat_map = {}
    receipts = []
    for r in receipts_raw:
        # Get category via first transaction
        rt = db.query(ReceiptTransaction).filter(ReceiptTransaction.receipt_id == r.id).first()
        cat_name = None
        if rt:
            tx = db.query(Transaction).get(rt.transaction_id)
            if tx and tx.category_id:
                if tx.category_id not in cat_map:
                    cat = db.query(ExpenseCategory).get(tx.category_id)
                    cat_map[tx.category_id] = cat.name if cat else None
                cat_name = cat_map[tx.category_id]
        receipts.append({
            "id": r.id,
            "created_at": r.created_at,
            "org_name": org_map.get(r.organization_id, "—"),
            "category_name": cat_name,
            "amount_detected": r.amount_detected,
            "amount_confirmed": r.amount_confirmed,
            "ocr_status": r.ocr_status,
        })

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
    user = get_mock_user(db)
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
    user = get_mock_user(db)
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
    user = get_mock_user(db)
    accessible = get_accessible_orgs(user, db)
    receipt = db.query(Receipt).get(receipt_id)
    if not receipt:
        return HTMLResponse("Квитанция не найдена", status_code=404)

    org_map = {o.id: o.name for o in db.query(Organization).all()}
    current_org = resolve_org(org_id, user, db)
    items = db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt_id).all()

    return templates.TemplateResponse("expenses/confirm.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else org_id,
        "receipt": {
            "id": receipt.id,
            "file_path": receipt.file_path,
            "ocr_raw": receipt.ocr_raw,
            "amount_detected": receipt.amount_detected,
            "amount_confirmed": receipt.amount_confirmed,
            "ocr_status": receipt.ocr_status,
            "org_name": org_map.get(receipt.organization_id, "—"),
        },
        "items": items,
        "is_both_kinder": receipt.organization_id == ORG_KINDERGARTENS,
        "categories": get_categories(db),
        "today": date.today().isoformat(),
        "error": None,
        "success": None,
    })


@router.post("/{receipt_id}/confirm")
def handle_confirm(
    receipt_id: int,
    request: Request,
    org_id: int = Form(None),
    action: str = Form(...),
    amount: float = Form(None),
    amount_sokuluk: float = Form(None),
    amount_kojomkul: float = Form(None),
    split_mode: str = Form(None),
    category_id: int = Form(None),
    description: str = Form(None),
    date_: str = Form(None, alias="date"),
    item_name: List[str] = Form(default=[]),
    item_qty: List[str] = Form(default=[]),
    item_unit_price: List[str] = Form(default=[]),
    item_total_price: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = get_mock_user(db)
    receipt = db.query(Receipt).get(receipt_id)
    if not receipt:
        return HTMLResponse("Квитанция не найдена", status_code=404)

    if action == "reject":
        receipt.ocr_status = "rejected"
        audit(db, "receipt", receipt.id, "update", user.id, {"status": "rejected"})
        db.commit()
        return RedirectResponse(f"/expenses/?org_id={org_id}", status_code=303)

    # Validate
    tx_date = date.fromisoformat(date_) if date_ else date.today()
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)
    org_map = {o.id: o.name for o in db.query(Organization).all()}

    if split_mode == "both":
        # Два садика
        if not amount_sokuluk or not amount_kojomkul:
            return templates.TemplateResponse("expenses/confirm.html", {
                "request": request, "current_user": user,
                "accessible_orgs": accessible,
                "current_org_id": org_id,
                "receipt": {"id": receipt.id, "file_path": receipt.file_path,
                            "ocr_raw": receipt.ocr_raw, "amount_detected": receipt.amount_detected,
                            "amount_confirmed": receipt.amount_confirmed, "ocr_status": receipt.ocr_status,
                            "org_name": org_map.get(receipt.organization_id, "—")},
                "is_both_kinder": True,
                "categories": get_categories(db),
                "today": date.today().isoformat(),
                "error": "Введи суммы для обоих садиков",
                "success": None,
            })

        sokuluk_id = db.query(Organization).filter(Organization.parent_id == ORG_KINDERGARTENS).first()
        kojomkul_id = db.query(Organization).filter(Organization.parent_id == ORG_KINDERGARTENS).offset(1).first()

        for kinder, amt in [(sokuluk_id, amount_sokuluk), (kojomkul_id, amount_kojomkul)]:
            if not kinder:
                continue
            tx = Transaction(
                organization_id=kinder.id, type="expense", amount=amt,
                category_id=category_id, description=description, date=tx_date,
                created_by=user.id,
            )
            db.add(tx)
            db.flush()
            db.add(ReceiptTransaction(receipt_id=receipt.id, transaction_id=tx.id, amount=amt))
            audit(db, "transaction", tx.id, "insert", user.id, {"org_id": kinder.id, "amount": amt})

        total_confirmed = amount_sokuluk + amount_kojomkul
    else:
        if not amount:
            return RedirectResponse(f"/expenses/{receipt_id}/confirm?org_id={org_id}&error=1", status_code=303)
        tx = Transaction(
            organization_id=receipt.organization_id, type="expense", amount=amount,
            category_id=category_id, description=description, date=tx_date,
            created_by=user.id,
        )
        db.add(tx)
        db.flush()
        db.add(ReceiptTransaction(receipt_id=receipt.id, transaction_id=tx.id, amount=amount))
        audit(db, "transaction", tx.id, "insert", user.id, {"org_id": receipt.organization_id, "amount": amount})
        total_confirmed = amount

    receipt.ocr_status = "confirmed"
    receipt.amount_confirmed = total_confirmed
    receipt.confirmed_by = user.id
    receipt.confirmed_at = datetime.now()
    audit(db, "receipt", receipt.id, "update", user.id, {"status": "confirmed", "amount": total_confirmed})

    # Сохраняем отредактированные позиции (если были переданы)
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
            db.add(ReceiptItem(
                receipt_id=receipt_id,
                name=name,
                qty=_safe_num(item_qty, i),
                unit_price=_safe_num(item_unit_price, i),
                total_price=total,
            ))

    db.commit()

    return RedirectResponse(f"/expenses/?org_id={org_id}", status_code=303)
