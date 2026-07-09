from datetime import date as date_type
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import ExpenseCategory, Organization, Product, WarehouseReceipt, WriteOff
from app.services.products import get_or_create_product

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


def _get_balance_map(db: Session, org_ids: set) -> dict:
    """Текущий остаток и средняя цена по ВСЕМ продуктам (не только тем, что были в приходе) —
    нужно для актуализации, где корректировать можно и то, чего ещё не было."""
    recv = (
        db.query(
            WarehouseReceipt.product_id.label("pid"),
            func.sum(WarehouseReceipt.quantity).label("qty"),
            func.sum(WarehouseReceipt.total_cost).label("cost"),
        )
        .filter(WarehouseReceipt.organization_id.in_(org_ids), WarehouseReceipt.deleted_at.is_(None))
        .group_by(WarehouseReceipt.product_id)
        .subquery()
    )
    woff = (
        db.query(WriteOff.product_id.label("pid"), func.sum(WriteOff.quantity).label("qty"))
        .filter(WriteOff.organization_id.in_(org_ids), WriteOff.deleted_at.is_(None))
        .group_by(WriteOff.product_id)
        .subquery()
    )
    rows = (
        db.query(
            Product.id,
            func.coalesce(recv.c.qty, 0),
            func.coalesce(recv.c.cost, 0),
            func.coalesce(woff.c.qty, 0),
        )
        .outerjoin(recv, Product.id == recv.c.pid)
        .outerjoin(woff, Product.id == woff.c.pid)
        .all()
    )
    result = {}
    for pid, received, total_cost, written in rows:
        received, total_cost, written = float(received), float(total_cost), float(written)
        balance = received - written
        result[pid] = {
            "balance": balance,
            "avg_price": (total_cost / received) if received > 0 else 0,
        }
    return result


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
        reason=reason or "питание детей",
        created_by=ctx["current_user"].id,
    )
    db.add(writeoff)
    db.commit()
    return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=302)


MEAL_TYPES = ["Завтрак", "Обед", "Полдник", "Ужин"]


@router.get("/writeoff/meal", response_class=HTMLResponse)
def writeoff_meal_form(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    all_orgs = db.query(Organization).all()
    org_ids = _descendants(ctx["current_org"].id, all_orgs) if ctx["current_org"] else set()
    balances = _get_balances(db, org_ids)
    in_stock = [b for b in balances if b["balance"] > 0]
    in_stock_json = [
        {"id": b["product"].id, "name": b["product"].name,
         "unit": b["product"].unit or "кг", "balance": b["balance"]}
        for b in in_stock
    ]

    ctx.update({
        "in_stock": in_stock, "in_stock_json": in_stock_json, "meal_types": MEAL_TYPES,
        "today": date_type.today().isoformat(), "error": None,
    })
    return templates.TemplateResponse("warehouse/writeoff_meal_form.html", ctx)


@router.post("/writeoff/meal", response_class=HTMLResponse)
def writeoff_meal_save(
    request: Request,
    org_id: str | None = Form(None),
    writeoff_date: str = Form(...),
    meal_type: str = Form(...),
    item_product_id: List[str] = Form(default=[]),
    item_quantity: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    d = date_type.fromisoformat(writeoff_date)
    for i, pid_str in enumerate(item_product_id):
        pid_str = pid_str.strip()
        qty_str = item_quantity[i].strip() if i < len(item_quantity) else ""
        if not pid_str or not qty_str:
            continue
        try:
            qty = float(qty_str.replace(",", "."))
        except ValueError:
            continue
        if qty <= 0:
            continue
        db.add(WriteOff(
            date=d, product_id=int(pid_str), quantity=qty,
            organization_id=ctx["current_org"].id, reason="питание детей",
            meal_type=meal_type, created_by=ctx["current_user"].id,
        ))
    db.commit()
    return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=302)


@router.get("/actualize", response_class=HTMLResponse)
def actualize_form(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    if ctx["current_user"].role == "staff":
        return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=302)

    all_orgs = db.query(Organization).all()
    org_ids = _descendants(ctx["current_org"].id, all_orgs) if ctx["current_org"] else set()
    balance_map = _get_balance_map(db, org_ids)
    products = db.query(Product).order_by(Product.name).all()
    products_json = [
        {"id": p.id, "name": p.name, "unit": p.unit or "кг",
         "balance": balance_map.get(p.id, {"balance": 0})["balance"]}
        for p in products
    ]

    ctx.update({
        "products_json": products_json,
        "today": date_type.today().isoformat(), "error": None, "results": None,
    })
    return templates.TemplateResponse("warehouse/actualize_form.html", ctx)


@router.post("/actualize", response_class=HTMLResponse)
def actualize_save(
    request: Request,
    org_id: str | None = Form(None),
    actualize_date: str = Form(...),
    item_name: List[str] = Form(default=[]),
    item_product_id: List[str] = Form(default=[]),
    item_qty: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    if ctx["current_user"].role == "staff":
        return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=302)

    all_orgs = db.query(Organization).all()
    org_ids = _descendants(ctx["current_org"].id, all_orgs) if ctx["current_org"] else set()
    balance_map = _get_balance_map(db, org_ids)
    d = date_type.fromisoformat(actualize_date)

    results = []
    for i, name in enumerate(item_name):
        name = name.strip()
        qty_str = item_qty[i].strip() if i < len(item_qty) else ""
        if not name or not qty_str:
            continue
        try:
            actual_qty = float(qty_str.replace(",", "."))
        except ValueError:
            continue

        pid_str = item_product_id[i].strip() if i < len(item_product_id) else ""
        if pid_str and pid_str.isdigit():
            product = db.get(Product, int(pid_str))
            if not product:
                product = get_or_create_product(db, name)
        else:
            product = get_or_create_product(db, name)
        db.flush()

        current = balance_map.get(product.id, {"balance": 0, "avg_price": 0})
        delta = round(actual_qty - current["balance"], 3)

        if delta > 0.001:
            price = current["avg_price"]
            db.add(WarehouseReceipt(
                date=d, product_id=product.id, quantity=delta, price_per_unit=price,
                total_cost=round(delta * price, 2), organization_id=ctx["current_org"].id,
                supplier_name="Инвентаризация", created_by=ctx["current_user"].id,
            ))
            results.append(f"{product.name}: +{delta:.3f} {product.unit or 'кг'}")
        elif delta < -0.001:
            db.add(WriteOff(
                date=d, product_id=product.id, quantity=abs(delta), organization_id=ctx["current_org"].id,
                reason="инвентаризация", created_by=ctx["current_user"].id,
            ))
            results.append(f"{product.name}: {delta:.3f} {product.unit or 'кг'}")
        else:
            results.append(f"{product.name}: совпадает")

    db.commit()

    products = db.query(Product).order_by(Product.name).all()
    balance_map = _get_balance_map(db, org_ids)
    products_json = [
        {"id": p.id, "name": p.name, "unit": p.unit or "кг",
         "balance": balance_map.get(p.id, {"balance": 0})["balance"]}
        for p in products
    ]
    ctx.update({
        "products_json": products_json, "today": date_type.today().isoformat(),
        "error": None if results else "Ничего не сохранено — заполни хотя бы одну строку",
        "results": results,
    })
    return templates.TemplateResponse("warehouse/actualize_form.html", ctx)


@router.get("/products/", response_class=HTMLResponse)
def products_list(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    if ctx["current_user"].role == "staff":
        return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=302)
    products = db.query(Product).order_by(Product.category.nullslast(), Product.name).all()
    expense_categories = db.query(ExpenseCategory).order_by(
        ExpenseCategory.parent_id.nullsfirst(), ExpenseCategory.name
    ).all()
    ctx.update({
        "products": products, "units": UNITS, "categories": CATEGORIES,
        "expense_categories": expense_categories,
    })
    return templates.TemplateResponse("warehouse/products.html", ctx)
