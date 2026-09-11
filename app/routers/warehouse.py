from datetime import date as date_type, datetime, timedelta
from pathlib import Path
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import (
    AuditLog, ExpenseCategory, Organization, Product, WarehouseReceipt, WriteOff,
)
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


def _writeoff_days(db: Session, org_ids: set, limit_days: int = 14) -> list:
    """Расход на кухню по дням — то, что человек проверяет вечером: занесли или нет.

    Дни с пропусками видно только в сплошном списке, поэтому группируем по дате
    и отдаём как есть, без склейки. Пересчёт склада сюда не попадает: у него
    свой reason и своя страница, мешать их — потерять оба."""
    rows = (
        db.query(WriteOff)
        .filter(WriteOff.organization_id.in_(org_ids), WriteOff.deleted_at.is_(None))
        .order_by(WriteOff.date.desc(), WriteOff.id.asc())
        .limit(400).all()
    )
    days, order = {}, []
    for w in rows:
        if w.date not in days:
            if len(order) >= limit_days:
                continue
            days[w.date] = []
            order.append(w.date)
        days[w.date].append(w)
    return [{"date": d, "lines": days[d]} for d in order]


@router.get("/", response_class=HTMLResponse)
def index(request: Request, org_id: str | None = None, err: str | None = None,
          msg: str | None = None, db: Session = Depends(get_db)):
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

    # Нулевые позиции с экрана убраны (11.09): 113 строк с остатком и так
    # длиннее телефона, а «ноль» не отвечает ни на один вопрос — товар либо
    # закончился, либо его никогда не покупали.
    in_stock = [b for b in balances if abs(b["balance"]) > 0.0001]
    by_cat = {}
    for b in in_stock:
        by_cat.setdefault(b["product"].category or "прочее", []).append(b)
    cat_order = ["овощи", "крупы", "мясо", "молочные", "масла", "зелень", "специи",
                 "бакалея", "фрукты", "напитки", "бытовая химия", "инвентарь",
                 "стройматериалы", "прочее"]
    groups = [{"name": c, "items": by_cat[c]} for c in cat_order if c in by_cat]
    groups += [{"name": c, "items": v} for c, v in by_cat.items() if c not in cat_order]

    today = date_type.today()
    days = _writeoff_days(db, org_ids)
    today_lines = next((d["lines"] for d in days if d["date"] == today), [])

    # Остаток по каждому товару — нужен на правке строки: показать, сколько
    # останется, надо до сохранения, а не ловить нехватку после.
    balance_by_pid = {b["product"].id: b["balance"] for b in balances}

    ctx.update({
        "balances": balances,
        "balance_by_pid": balance_by_pid,
        "groups": groups,
        "in_stock_count": len(in_stock),
        "minus_items": [b for b in in_stock if b["balance"] < 0],
        "total_value": total_value,
        "recent_receipts": recent_receipts,
        "recent_writeoffs": recent_writeoffs,
        "days": days,
        "today": today,
        "today_lines": today_lines,
        "tomorrow_date": tomorrow.isoformat(),
        "tomorrow_missing": tomorrow_draft["unlinked"] if tomorrow_draft else [],
        "err": err,
        "msg": msg,
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

    all_orgs = db.query(Organization).all()
    org_ids = _descendants(ctx["current_org"].id, all_orgs) if ctx["current_org"] else set()
    balances = _get_balances(db, org_ids)
    in_stock = [b for b in balances if b["balance"] > 0]

    # Тот же запрет, что и на списании по приёмам пищи: в минус склад не уходит.
    available = _get_balance_map(db, org_ids).get(product_id, {}).get("balance", 0.0)
    if quantity > available + 0.001:
        product = db.get(Product, product_id)
        unit = (product.unit if product else "") or "ед"
        ctx.update({
            "in_stock": in_stock, "today": writeoff_date,
            "error": (f"Не списано: {product.name if product else product_id} — "
                      f"списываете {quantity:g} {unit}, на складе {available:g} {unit}"),
        })
        return templates.TemplateResponse("warehouse/writeoff_form.html", ctx)

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

    # Сначала разбираем всю форму, потом проверяем, и только потом пишем.
    # Иначе первые строки уже легли бы в базу, а на четвёртой всплыла ошибка —
    # человек не поймёт, что списалось, а что нет.
    parsed = []
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
        parsed.append({"product_id": int(pid_str), "qty": qty,
                       "dish_id": int(dish_str) if dish_str else None})

    # Больше, чем лежит на складе, списать нельзя (10.09). Морковь ушла в минус
    # на 3 988 кг, потому что 4 кг ввели как 4000 — граммы в поле килограммов.
    # Считаем сразу по всем строкам формы: один товар может встретиться дважды,
    # и порознь каждая строка проходит, а вместе они уводят остаток в минус.
    all_orgs = db.query(Organization).all()
    org_ids = _descendants(ctx["current_org"].id, all_orgs) if ctx["current_org"] else set()
    balance_map = _get_balance_map(db, org_ids)
    wanted = {}
    for item in parsed:
        wanted[item["product_id"]] = wanted.get(item["product_id"], 0.0) + item["qty"]

    problems = []
    for pid, qty in wanted.items():
        available = balance_map.get(pid, {}).get("balance", 0.0)
        if qty > available + 0.001:
            product = db.get(Product, pid)
            unit = (product.unit if product else "") or "ед"
            problems.append(
                f"{product.name if product else pid}: списываете {qty:g} {unit}, "
                f"на складе {available:g} {unit}"
            )

    if problems:
        balances = _get_balances(db, org_ids)
        in_stock = [b for b in balances if b["balance"] > 0]
        ctx.update({
            "in_stock": in_stock,
            "in_stock_json": [
                {"id": b["product"].id, "name": b["product"].name,
                 "unit": b["product"].unit or "кг", "balance": b["balance"]}
                for b in in_stock
            ],
            "meal_types": MEAL_TYPES,
            "today": writeoff_date,
            "meal_type": meal_type,
            # Введённое возвращается на форму — переписывать десяток строк
            # заново из-за одной опечатки человек не станет, он просто
            # исправит цифру «на глаз» и потеряет остальное.
            "submitted": parsed,
            "error": "Не списано: больше, чем есть на складе. " + "; ".join(problems),
        })
        return templates.TemplateResponse("warehouse/writeoff_meal_form.html", ctx)

    for item in parsed:
        db.add(WriteOff(
            date=d, product_id=item["product_id"], quantity=item["qty"],
            organization_id=ctx["current_org"].id, reason="питание детей",
            meal_type=meal_type, dish_id=item["dish_id"],
            created_by=ctx["current_user"].id,
        ))
    db.commit()
    return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=302)


def _writeoff_for_edit(db: Session, ctx: dict, wid: int):
    """Строка списания, которую этому пользователю можно трогать.

    Возвращает (строка, ошибка). Чужой объект и уже удалённая строка —
    не «нет прав», а «нечего править»: подробности о существовании чужих
    записей наружу не выдаём."""
    all_orgs = db.query(Organization).all()
    org_ids = _descendants(ctx["current_org"].id, all_orgs) if ctx["current_org"] else set()
    w = db.get(WriteOff, wid)
    if not w or w.deleted_at is not None or w.organization_id not in org_ids:
        return None, "Строка не найдена"
    return w, None


@router.post("/writeoff/{wid}/edit")
def writeoff_edit(
    wid: int,
    request: Request,
    org_id: str | None = Form(None),
    quantity: str = Form(...),
    db: Session = Depends(get_db),
):
    """Правка количества в уже проведённом списании.

    Расход кухни заводится каждый день по листу поваров — описка в цифре тут
    рядовое событие, а не корректировка счёта. До 11.09 пути исправить её не
    было вообще: 200 граммов масла, занесённые как 200 кг, уводили склад в
    минус на 199,6 кг, и разобрать это мог только разработчик напрямую в базе."""
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    back = f"/warehouse/?org_id={ctx['current_org_id']}"
    w, err = _writeoff_for_edit(db, ctx, wid)
    if err:
        return RedirectResponse(f"{back}&err={quote(err)}", status_code=302)

    try:
        qty = float(quantity.strip().replace(",", "."))
    except ValueError:
        return RedirectResponse(f"{back}&err={quote('Количество не похоже на число')}", status_code=302)
    if qty <= 0:
        return RedirectResponse(f"{back}&err={quote('Количество должно быть больше нуля')}", status_code=302)

    # Та же проверка, что на списании, но с поправкой: эта строка уже сидит
    # в расходе, поэтому её собственное количество возвращается в остаток.
    all_orgs = db.query(Organization).all()
    org_ids = _descendants(ctx["current_org"].id, all_orgs)
    available = _get_balance_map(db, org_ids).get(w.product_id, {}).get("balance", 0.0) + float(w.quantity)
    product = db.get(Product, w.product_id)
    unit = (product.unit if product else "") or "ед"
    if qty > available + 0.001:
        msg = (f"{product.name if product else w.product_id}: на складе {available:g} {unit}, "
               f"списать {qty:g} нельзя")
        return RedirectResponse(f"{back}&err={quote(msg)}", status_code=302)

    old = float(w.quantity)
    w.quantity = qty
    db.add(AuditLog(
        entity_type="write_off", entity_id=w.id, action="update",
        user_id=ctx["current_user"].id,
        old_data={"quantity": f"{old:g}"},
        new_data={"quantity": f"{qty:g}", "unit": unit},
    ))
    db.commit()
    ok = f"{product.name if product else ''}: {old:g} → {qty:g} {unit}"
    return RedirectResponse(f"{back}&msg={quote(ok)}", status_code=302)


@router.post("/writeoff/{wid}/delete")
def writeoff_delete(
    wid: int,
    request: Request,
    org_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Мягкое удаление строки списания — лишняя, задвоенная, ошибочная.

    Мягкое, а не физическое: тот же приём, что у расходов и сверок — запись
    остаётся в базе, из остатка выпадает (везде фильтр deleted_at IS NULL)."""
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    back = f"/warehouse/?org_id={ctx['current_org_id']}"
    w, err = _writeoff_for_edit(db, ctx, wid)
    if err:
        return RedirectResponse(f"{back}&err={quote(err)}", status_code=302)

    product = db.get(Product, w.product_id)
    unit = (product.unit if product else "") or "ед"
    w.deleted_at = datetime.utcnow()
    db.add(AuditLog(
        entity_type="write_off", entity_id=w.id, action="delete",
        user_id=ctx["current_user"].id,
        old_data={"quantity": f"{float(w.quantity):g}", "unit": unit,
                  "date": w.date.isoformat()},
        new_data={"deleted": True},
    ))
    db.commit()
    ok = f"Убрано: {product.name if product else ''} {float(w.quantity):g} {unit}"
    return RedirectResponse(f"{back}&msg={quote(ok)}", status_code=302)


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
