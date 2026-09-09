from datetime import date as date_type, timedelta
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
from app.services.products import get_or_create_product, UNITS, CATEGORIES
from app.services.warehouse import get_product_balances as _get_balances
from app.services.writeoff_calc import (
    AUTO_REASON, MENU_WRITEOFF_ENABLED, auto_apply_if_pending, compute_day_draft,
)

router = APIRouter(prefix="/warehouse", tags=["warehouse"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _descendants(org_id: int, all_orgs: list) -> set:
    ids = {org_id}
    for o in all_orgs:
        if o.parent_id == org_id:
            ids |= _descendants(o.id, all_orgs)
    return ids


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

    if ctx["current_org"]:
        yesterday = date_type.today() - timedelta(days=1)
        auto_apply_if_pending(db, ctx["current_org"].id, yesterday, ctx["current_user"].id)

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

    tomorrow = date_type.today() + timedelta(days=1)
    tomorrow_draft = compute_day_draft(db, ctx["current_org"].id, tomorrow) if ctx["current_org"] else None

    ctx.update({
        "balances": balances,
        "total_value": total_value,
        "recent_receipts": recent_receipts,
        "recent_writeoffs": recent_writeoffs,
        "tomorrow_date": tomorrow.isoformat(),
        "tomorrow_missing": tomorrow_draft["unlinked"] if tomorrow_draft else [],
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


@router.post("/products/{product_id}/grams-per-unit")
def set_grams_per_unit(
    product_id: int,
    request: Request,
    grams_per_unit: str = Form(...),
    org_id: str | None = Form(None),
    writeoff_date: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Вписать вес 1 шт. (Хлеб/Яйцо и т.п.) прямо со страницы списания — не
    отдельный раздел склада (туда у staff и доступа нет), а на месте, где
    видно, что вес неизвестен (27.07)."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    try:
        val = float(grams_per_unit.replace(",", "."))
    except ValueError:
        val = None
    if val and val > 0:
        product = db.get(Product, product_id)
        if product:
            product.grams_per_unit = val
            db.commit()
    redirect_url = f"/warehouse/writeoff/auto?org_id={org_id or ''}"
    if writeoff_date:
        redirect_url += f"&writeoff_date={writeoff_date}"
    return RedirectResponse(redirect_url, status_code=303)


@router.get("/writeoff/auto", response_class=HTMLResponse)
def writeoff_auto_form(request: Request, org_id: str | None = None, writeoff_date: str | None = None,
                        db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    if not ctx["current_org"]:
        return RedirectResponse(f"/warehouse/?org_id={org_id or ''}", status_code=302)

    target_date = date_type.fromisoformat(writeoff_date) if writeoff_date else date_type.today()
    draft = compute_day_draft(db, ctx["current_org"].id, target_date)

    ctx.update({
        "writeoff_date": target_date.isoformat(),
        "draft": draft,
        "error": None,
        "menu_writeoff_enabled": MENU_WRITEOFF_ENABLED,
    })
    return templates.TemplateResponse("warehouse/writeoff_auto_form.html", ctx)


@router.post("/writeoff/auto", response_class=HTMLResponse)
def writeoff_auto_save(
    request: Request,
    org_id: str | None = Form(None),
    writeoff_date: str = Form(...),
    item_product_id: List[str] = Form(default=[]),
    item_quantity: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    if not ctx["current_org"]:
        return RedirectResponse(f"/warehouse/?org_id={org_id or ''}", status_code=302)

    # Ручное подтверждение расчёта по меню — тот же путь «меню → склад», что и
    # авто-списание, поэтому выключается тем же флагом (09.09). Экран остаётся
    # доступным как справка «сколько по норме должно уйти», но со склада уже
    # ничего не списывает.
    if not MENU_WRITEOFF_ENABLED:
        return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=302)

    d = date_type.fromisoformat(writeoff_date)
    draft = compute_day_draft(db, ctx["current_org"].id, d)
    total_present = draft["total_present"]

    # Авто-джоб мог уже провести эту дату сам (см. auto_apply_if_pending) — ручное
    # подтверждение здесь считается правкой этих же чисел, а не добавкой поверх них,
    # иначе продукт задвоился бы. Мягко убираем старые авто-строки за эту дату/объект
    # и вставляем то, что реально подтвердили на экране (тот же приём, что и в
    # recurring_expenses — исправил/удалил и провёл заново, не плюсом).
    old_auto = db.query(WriteOff).filter(
        WriteOff.organization_id == ctx["current_org"].id,
        WriteOff.date == d,
        WriteOff.reason == AUTO_REASON,
        WriteOff.deleted_at.is_(None),
    ).all()
    for w in old_auto:
        w.deleted_at = func.now()

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
            organization_id=ctx["current_org"].id, reason=AUTO_REASON,
            children_count=total_present, created_by=ctx["current_user"].id,
        ))
    db.commit()
    return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=302)


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
    item_dish_id: List[str] = Form(default=[]),
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
        dish_str = item_dish_id[i].strip() if i < len(item_dish_id) else ""
        db.add(WriteOff(
            date=d, product_id=int(pid_str), quantity=qty,
            organization_id=ctx["current_org"].id, reason="питание детей",
            meal_type=meal_type, dish_id=int(dish_str) if dish_str else None,
            created_by=ctx["current_user"].id,
        ))
    db.commit()
    return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=302)


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
