from datetime import date, datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs
from app.models import AuditLog, Supplier, SupplierPayment
from app.services import supplier_ledger

router = APIRouter(prefix="/suppliers", tags=["suppliers"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def audit(db: Session, entity_id: int, action: str, user_id: int, new_data: dict | None = None):
    db.add(AuditLog(
        entity_type="supplier_payment", entity_id=entity_id,
        action=action, user_id=user_id, new_data=new_data,
    ))


@router.get("/", response_class=HTMLResponse)
def supplier_list(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org_id = request.query_params.get("org_id")

    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    balances = supplier_ledger.get_all_supplier_balances(db)

    supplier_rows = []
    for s in suppliers:
        debt = balances.get(s.id) or 0
        supplier_rows.append({
            "id": s.id, "name": s.name, "phone": s.phone, "inn": s.inn,
            "debt": debt if debt > 0 else None,
        })

    return templates.TemplateResponse("suppliers/list.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": int(current_org_id) if current_org_id else (accessible[0].id if accessible else None),
        "suppliers": supplier_rows,
    })


@router.get("/{supplier_id}", response_class=HTMLResponse)
def supplier_detail(supplier_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org_id = request.query_params.get("org_id")
    supplier = db.query(Supplier).get(supplier_id)
    if not supplier:
        return RedirectResponse("/suppliers/", status_code=302)

    balance = supplier_ledger.get_supplier_balance(db, supplier_id)
    ledger = supplier_ledger.get_ledger_rows(db, supplier_id)

    return templates.TemplateResponse("suppliers/detail.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": int(current_org_id) if current_org_id else (accessible[0].id if accessible else None),
        "supplier": supplier,
        "balance": balance,
        "ledger": ledger,
        "today": date.today().isoformat(),
    })


@router.post("/{supplier_id}/edit")
def edit_supplier(
    supplier_id: int,
    request: Request,
    name: str = Form(...),
    phone: str = Form(...),
    inn: str = Form(default=""),
    opening_balance: str = Form(default=""),
    opening_balance_date: str = Form(default=""),
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    supplier = db.query(Supplier).get(supplier_id)
    if supplier:
        supplier.name = name.strip()
        supplier.phone = phone.strip()
        supplier.inn = inn.strip() or None
        try:
            supplier.opening_balance = float((opening_balance or "0").replace(",", ".")) or 0
        except ValueError:
            pass
        supplier.opening_balance_date = (
            datetime.strptime(opening_balance_date, "%Y-%m-%d").date() if opening_balance_date else None
        )
        db.commit()
    redirect_url = f"/suppliers/{supplier_id}?org_id={org_id}" if org_id else f"/suppliers/{supplier_id}"
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/{supplier_id}/payments")
def add_supplier_payment(
    supplier_id: int,
    request: Request,
    amount: str = Form(...),
    payment_date: str = Form(...),
    comment: str = Form(default=""),
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    redirect_url = f"/suppliers/{supplier_id}?org_id={org_id}" if org_id else f"/suppliers/{supplier_id}"
    try:
        amount_val = float((amount or "0").replace(",", "."))
        date_val = datetime.strptime(payment_date, "%Y-%m-%d").date()
        payment = supplier_ledger.add_payment(db, supplier_id, amount_val, date_val, comment.strip() or None, user.id)
        db.flush()
        audit(db, payment.id, "insert", user.id, {"supplier_id": supplier_id, "amount": amount_val, "date": payment_date})
        db.commit()
    except ValueError as e:
        db.rollback()
        return RedirectResponse(f"{redirect_url}&error={e}" if "?" in redirect_url else f"{redirect_url}?error={e}", status_code=303)
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/{supplier_id}/payments/{payment_id}/delete")
def delete_supplier_payment(
    supplier_id: int,
    payment_id: int,
    request: Request,
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    payment = db.query(SupplierPayment).get(payment_id)
    if payment and payment.supplier_id == supplier_id:
        payment.deleted_at = datetime.utcnow()
        audit(db, payment.id, "delete", user.id, {"supplier_id": supplier_id, "amount": float(payment.amount)})
        db.commit()
    redirect_url = f"/suppliers/{supplier_id}?org_id={org_id}" if org_id else f"/suppliers/{supplier_id}"
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/", response_class=HTMLResponse)
def create_supplier(
    request: Request,
    name: str = Form(...),
    phone: str = Form(...),
    inn: str = Form(default=""),
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    name = name.strip()
    phone_val = phone.strip()
    inn_val = inn.strip() or None

    if name and phone_val:
        existing = db.query(Supplier).filter(Supplier.name == name).first()
        if not existing:
            s = Supplier(name=name, phone=phone_val, inn=inn_val)
            db.add(s)
            db.commit()

    redirect_url = f"/suppliers/?org_id={org_id}" if org_id else "/suppliers/"
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/{supplier_id}/delete")
def delete_supplier(
    supplier_id: int,
    request: Request,
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    s = db.query(Supplier).get(supplier_id)
    if s:
        db.delete(s)
        db.commit()
    redirect_url = f"/suppliers/?org_id={org_id}" if org_id else "/suppliers/"
    return RedirectResponse(redirect_url, status_code=303)
