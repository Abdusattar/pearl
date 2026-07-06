from datetime import date as date_type
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import ExpenseCategory, Organization, Product, WarehouseReceipt, WriteOff

router = APIRouter(prefix="/warehouse", tags=["warehouse"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

UNITS = ["кг", "л", "шт", "г", "уп", "пач"]
CATEGORIES = ["мясо", "птица", "рыба", "молочные", "крупы", "овощи", "фрукты", "масло/жиры", "хлеб", "прочее"]


def _descendants(org_id: int, all_orgs: list) -> set:
    ids = {org_id}
    for o in all_orgs:
        if o.parent_id == org_id:
            ids |= _descendants(o.id, all_orgs)
    return ids


def _get_balances(db: Session, org_ids: set) -> list[dict]:
    recv = (
        db.query(
            WarehouseReceipt.product_id.label("pid"),
            func.sum(WarehouseReceipt.quantity).label("qty"),
            func.sum(WarehouseReceipt.total_cost).label("cost"),
        )
        .filter(
            WarehouseReceipt.organization_id.in_(org_ids),
            WarehouseReceipt.deleted_at.is_(None),
        )
        .group_by(WarehouseReceipt.product_id)
        .subquery()
    )

    woff = (
        db.query(
            WriteOff.product_id.label("pid"),
            func.sum(WriteOff.quantity).label("qty"),
        )
        .filter(
            WriteOff.organization_id.in_(org_ids),
            WriteOff.deleted_at.is_(None),
        )
        .group_by(WriteOff.product_id)
        .subquery()
    )

    rows = (
        db.query(
            Product,
            func.coalesce(recv.c.qty, 0).label("received"),
            func.coalesce(recv.c.cost, 0).label("total_cost"),
            func.coalesce(woff.c.qty, 0).label("written"),
        )
        .outerjoin(recv, Product.id == recv.c.pid)
        .outerjoin(woff, Product.id == woff.c.pid)
        .filter(func.coalesce(recv.c.qty, 0) > 0)
        .order_by(Product.category.nullslast(), Product.name)
        .all()
    )

    result = []
    for product, received, total_cost, written in rows:
        received = float(received)
        written = float(written)
        total_cost = float(total_cost)
        balance = received - written
        avg_price = total_cost / received if received > 0 else 0
        result.append({
            "product": product,
            "received": received,
            "written": written,
            "balance": balance,
            "avg_price": avg_price,
            "balance_value": balance * avg_price,
        })
    return result


def _base_ctx(request: Request, db: Session, org_id_str: str | None) -> dict:
    user = get_current_user(request, db)
    if not user:
        return None
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(int(org_id_str) if org_id_str and org_id_str.isdigit() else None, user, db)
    return {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "current_org": current_org,
        "active_page": "warehouse",
    }


@router.get("/", response_class=HTMLResponse)
def index(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    all_orgs = db.query(Organization).all()
    org_ids = _descendants(ctx["current_org"].id, all_orgs) if ctx["current_org"] else set()

    balances = _get_balances(db, org_ids)
    total_value = sum(b["balance_value"] for b in balances)

    # Last 10 movements
    recent_receipts = (
        db.query(WarehouseReceipt)
        .filter(WarehouseReceipt.organization_id.in_(org_ids), WarehouseReceipt.deleted_at.is_(None))
        .order_by(WarehouseReceipt.date.desc(), WarehouseReceipt.id.desc())
        .limit(5).all()
    )
    recent_writeoffs = (
        db.query(WriteOff)
        .filter(WriteOff.organization_id.in_(org_ids), WriteOff.deleted_at.is_(None))
        .order_by(WriteOff.date.desc(), WriteOff.id.desc())
        .limit(5).all()
    )

    ctx.update({
        "balances": balances,
        "total_value": total_value,
        "recent_receipts": recent_receipts,
        "recent_writeoffs": recent_writeoffs,
    })
    return templates.TemplateResponse("warehouse/index.html", ctx)


@router.get("/receipt/add", response_class=HTMLResponse)
def receipt_add_form(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    products = db.query(Product).order_by(Product.name).all()
    ctx.update({"products": products, "units": UNITS, "categories": CATEGORIES, "today": date_type.today().isoformat(), "error": None})
    return templates.TemplateResponse("warehouse/receipt_form.html", ctx)


@router.post("/receipt/add", response_class=HTMLResponse)
def receipt_add_save(
    request: Request,
    org_id: str | None = Form(None),
    product_id: str = Form(...),
    new_product_name: str = Form(""),
    new_product_unit: str = Form("кг"),
    new_product_category: str = Form("прочее"),
    quantity: float = Form(...),
    price_per_unit: float = Form(...),
    receipt_date: str = Form(...),
    supplier_name: str = Form(""),
    db: Session = Depends(get_db),
):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    # Resolve product
    if product_id == "new":
        name = new_product_name.strip()
        if not name:
            products = db.query(Product).order_by(Product.name).all()
            ctx.update({"products": products, "units": UNITS, "categories": CATEGORIES,
                        "today": receipt_date, "error": "Введите название нового продукта"})
            return templates.TemplateResponse("warehouse/receipt_form.html", ctx)
        existing = db.query(Product).filter(func.lower(Product.name) == name.lower()).first()
        if existing:
            product = existing
        else:
            product = Product(name=name, unit=new_product_unit, category=new_product_category)
            db.add(product)
            db.flush()
    else:
        product = db.query(Product).filter(Product.id == int(product_id)).first()
        if not product:
            return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=302)

    total = round(quantity * price_per_unit, 2)
    receipt = WarehouseReceipt(
        date=date_type.fromisoformat(receipt_date),
        product_id=product.id,
        quantity=quantity,
        price_per_unit=price_per_unit,
        total_cost=total,
        organization_id=ctx["current_org"].id,
        supplier_name=supplier_name.strip() or None,
        created_by=ctx["current_user"].id,
    )
    db.add(receipt)
    db.commit()
    return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=302)


@router.get("/writeoff/add", response_class=HTMLResponse)
def writeoff_add_form(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    all_orgs = db.query(Organization).all()
    org_ids = _descendants(ctx["current_org"].id, all_orgs) if ctx["current_org"] else set()
    balances = _get_balances(db, org_ids)
    in_stock = [b for b in balances if b["balance"] > 0]

    ctx.update({"in_stock": in_stock, "today": date_type.today().isoformat(), "error": None})
    return templates.TemplateResponse("warehouse/writeoff_form.html", ctx)


@router.post("/writeoff/add", response_class=HTMLResponse)
def writeoff_add_save(
    request: Request,
    org_id: str | None = Form(None),
    product_id: int = Form(...),
    quantity: float = Form(...),
    writeoff_date: str = Form(...),
    children_count: int = Form(None),
    reason: str = Form("питание детей"),
    db: Session = Depends(get_db),
):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    writeoff = WriteOff(
        date=date_type.fromisoformat(writeoff_date),
        product_id=product_id,
        quantity=quantity,
        organization_id=ctx["current_org"].id,
        children_count=children_count if children_count else None,
        reason=reason or "питание детей",
        created_by=ctx["current_user"].id,
    )
    db.add(writeoff)
    db.commit()
    return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=302)


@router.get("/products/", response_class=HTMLResponse)
def products_list(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    products = db.query(Product).order_by(Product.category.nullslast(), Product.name).all()
    expense_categories = db.query(ExpenseCategory).order_by(
        ExpenseCategory.parent_id.nullsfirst(), ExpenseCategory.name
    ).all()
    ctx.update({
        "products": products, "units": UNITS, "categories": CATEGORIES,
        "expense_categories": expense_categories,
    })
    return templates.TemplateResponse("warehouse/products.html", ctx)


@router.post("/products/add")
def products_add(
    request: Request,
    org_id: str | None = Form(None),
    name: str = Form(...),
    unit: str = Form("кг"),
    category: str = Form("прочее"),
    expense_category_id: str = Form(None),
    db: Session = Depends(get_db),
):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    existing = db.query(Product).filter(func.lower(Product.name) == name.strip().lower()).first()
    if not existing:
        exp_cat_id = int(expense_category_id) if expense_category_id and expense_category_id.isdigit() else None
        db.add(Product(name=name.strip(), unit=unit, category=category, expense_category_id=exp_cat_id))
        db.commit()
    return RedirectResponse(f"/warehouse/products/?org_id={ctx['current_org_id']}", status_code=302)
