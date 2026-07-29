from datetime import date as date_type, timedelta
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import Dish, DishIngredient, MenuEntry
from app.services.dishes import get_or_create_dish, frequent_dishes

router = APIRouter(prefix="/menu", tags=["menu"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

MEAL_TYPES = ["Завтрак", "Обед", "Полдник", "Ужин"]
WEEKDAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница"]


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
        "active_page": "menu",
    }


def _week_monday(d: date_type) -> date_type:
    return d - timedelta(days=d.weekday())


WEEK_STRIP_BACK = 2   # недель назад от текущей
WEEK_STRIP_FWD = 4    # недель вперёд


def _build_day_card(d: date_type, entries_by_day: dict) -> dict:
    return {
        "date": d,
        "label": f"{WEEKDAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}",
        "meals": {mt: entries_by_day.get(d, {}).get(mt, []) for mt in MEAL_TYPES},
        "has_entries": d in entries_by_day,
    }


@router.get("/", response_class=HTMLResponse)
def menu_form(request: Request, org_id: str | None = None, start: str | None = None,
              db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    today = date_type.today()
    this_monday = _week_monday(today)
    start_date = _week_monday(date_type.fromisoformat(start)) if start else this_monday
    days = [start_date + timedelta(days=i) for i in range(5)]

    org_id_val = ctx["current_org"].id if ctx["current_org"] else None

    existing = (
        db.query(MenuEntry)
        .filter(MenuEntry.organization_id == org_id_val, MenuEntry.date.in_(days))
        .all()
    ) if org_id_val else []

    entries_by_day = {}
    for e in existing:
        entries_by_day.setdefault(e.date, {}).setdefault(e.meal_type, []).append(
            {"id": e.dish_id, "name": e.dish.name}
        )

    chips_by_meal = {mt: frequent_dishes(db, mt) for mt in MEAL_TYPES}

    # предупреждение (не блок) — ближайший будний день без единой записи меню
    warning_date = None
    for d in days:
        if d >= today and d not in entries_by_day:
            warning_date = d
            break

    day_cards = [_build_day_card(d, entries_by_day) for d in days]

    # лента недель: -2..+4 от текущей календарной недели, с индикатором заполненности
    strip_mondays = [
        this_monday + timedelta(weeks=w)
        for w in range(-WEEK_STRIP_BACK, WEEK_STRIP_FWD + 1)
    ]
    strip_range_start = strip_mondays[0]
    strip_range_end = strip_mondays[-1] + timedelta(days=4)
    filled_dates = set()
    if org_id_val:
        rows = (
            db.query(MenuEntry.date)
            .filter(
                MenuEntry.organization_id == org_id_val,
                MenuEntry.date >= strip_range_start,
                MenuEntry.date <= strip_range_end,
            )
            .distinct()
            .all()
        )
        filled_dates = {r[0] for r in rows}

    week_strip = [
        {
            "start": mon.isoformat(),
            "label": mon.strftime("%d.%m"),
            "is_current": mon == start_date,
            "has_entries": any((mon + timedelta(days=i)) in filled_dates for i in range(5)),
        }
        for mon in strip_mondays
    ]

    ctx.update({
        "day_cards": day_cards, "meal_types": MEAL_TYPES, "chips_by_meal": chips_by_meal,
        "start_date": start_date.isoformat(),
        "prev_start": (start_date - timedelta(days=7)).isoformat(),
        "next_start": (start_date + timedelta(days=7)).isoformat(),
        "week_strip": week_strip,
        "warning_date": (
            f"{WEEKDAY_NAMES[warning_date.weekday()]}, {warning_date.strftime('%d.%m')}"
        ) if warning_date else None,
    })
    return templates.TemplateResponse("menu/form.html", ctx)


@router.post("/day", response_class=HTMLResponse)
def menu_day_save(
    request: Request,
    org_id: str | None = Form(None),
    date: str = Form(...),
    meal: List[str] = Form(default=[]),
    dish: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Автосохранение одного дня — вызывается фронтом сразу при добавлении/
    удалении блюда, без общей кнопки «Сохранить» (решено 17.07, см.
    wiki/blueprints/menu_module.md). Затрагивает только эту дату — соседние
    дни в диапазоне не трогаем. Возвращает канонический список блюд по дню —
    get_or_create_dish может смэтчить опечатку на уже существующее блюдо, и
    фронту нужно перерисовать чипы под настоящим названием, а не тем, что
    ввёл пользователь."""
    ctx = _base_ctx(request, db, org_id)
    if ctx is None or ctx["current_org"] is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    d = date_type.fromisoformat(date)

    # advisory lock на (org_id, дата) — сериализует параллельные сохранения одного дня:
    # без него быстрые правки разных приёмов пищи подряд шлют несколько overlapping
    # POST, каждый видит "нечего удалять" и вставляет свою копию — реальные дубли
    # блюд на проде (20/23/27.07). Держится до конца транзакции, снимается сам.
    date_key = int(d.strftime("%Y%m%d"))
    db.execute(
        text("SELECT pg_advisory_xact_lock(:org_id, :date_key)"),
        {"org_id": ctx["current_org"].id, "date_key": date_key},
    )

    db.query(MenuEntry).filter(
        MenuEntry.organization_id == ctx["current_org"].id,
        MenuEntry.date == d,
    ).delete(synchronize_session=False)

    for i, meal_type in enumerate(meal):
        dish_name = dish[i].strip() if i < len(dish) else ""
        if not dish_name:
            continue
        dish_obj = get_or_create_dish(db, dish_name)
        db.add(MenuEntry(
            organization_id=ctx["current_org"].id,
            date=d,
            meal_type=meal_type,
            dish_id=dish_obj.id,
            created_by=ctx["current_user"].id,
        ))
    db.commit()

    entries = (
        db.query(MenuEntry)
        .filter(MenuEntry.organization_id == ctx["current_org"].id, MenuEntry.date == d)
        .all()
    )
    meals: dict[str, list[dict]] = {mt: [] for mt in MEAL_TYPES}
    for e in entries:
        meals.setdefault(e.meal_type, []).append({"id": e.dish_id, "name": e.dish.name})

    return JSONResponse({"meals": meals})


@router.get("/dishes/search")
def dishes_search(q: str = "", db: Session = Depends(get_db)):
    from app.services.dishes import search_dishes
    return search_dishes(db, q)


@router.get("/dishes/for-meal")
def dishes_for_meal(org_id: int, date: str, meal_type: str, db: Session = Depends(get_db)):
    """Блюда, заведённые в меню на конкретную дату/приём пищи — для выпадающего
    списка на списании (/warehouse/writeoff/meal). Пустой список = меню не
    заполнено, форма списания это не блокирует (см. wiki/blueprints/menu_module.md)."""
    d = date_type.fromisoformat(date)
    rows = (
        db.query(MenuEntry)
        .filter(MenuEntry.organization_id == org_id, MenuEntry.date == d, MenuEntry.meal_type == meal_type)
        .all()
    )
    return [{"id": r.dish_id, "name": r.dish.name} for r in rows]


@router.get("/dishes/", response_class=HTMLResponse)
def dishes_list(request: Request, org_id: str | None = None, q: str | None = None,
                 db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    dishes = db.query(Dish).order_by(Dish.name).all()
    counts = dict(
        db.query(DishIngredient.dish_id, func.count(DishIngredient.id))
        .group_by(DishIngredient.dish_id)
        .all()
    )
    rows = [{"id": d.id, "name": d.name, "ingredient_count": counts.get(d.id, 0)} for d in dishes]
    if q:
        q_low = q.strip().lower()
        rows = [r for r in rows if q_low in r["name"].lower()]

    ctx.update({"rows": rows, "q": q or ""})
    return templates.TemplateResponse("menu/dishes_list.html", ctx)


@router.post("/dishes/new")
def dishes_new(request: Request, org_id: str | None = Form(None), name: str = Form(...),
                db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    dish = get_or_create_dish(db, name)
    db.commit()
    return RedirectResponse(f"/menu/dishes/{dish.id}?org_id={ctx['current_org_id'] or ''}", status_code=302)


@router.get("/dishes/{dish_id}", response_class=HTMLResponse)
def dish_detail(request: Request, dish_id: int, org_id: str | None = None,
                 return_date: str | None = None, db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    dish = db.get(Dish, dish_id)
    if not dish:
        return RedirectResponse(f"/menu/dishes/?org_id={ctx['current_org_id'] or ''}", status_code=302)

    ingredients = (
        db.query(DishIngredient)
        .filter(DishIngredient.dish_id == dish_id)
        .order_by(DishIngredient.id)
        .all()
    )
    ctx.update({"dish": dish, "ingredients": ingredients, "return_date": return_date, "error": None})
    return templates.TemplateResponse("menu/dish_detail.html", ctx)


def _to_float(s: str) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


@router.post("/dishes/{dish_id}", response_class=HTMLResponse)
def dish_save(
    request: Request,
    dish_id: int,
    org_id: str | None = Form(None),
    return_date: str | None = Form(None),
    item_product_id: List[str] = Form(default=[]),
    item_name: List[str] = Form(default=[]),
    item_qty_sadik: List[str] = Form(default=[]),
    item_qty_shkola: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Полная замена рецепта блюда — проще и надёжнее построчного diff (тот же
    приём, что и в menu_day_save: удалить всё за этот dish_id, вставить заново
    то, что реально отправила форма)."""
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    dish = db.get(Dish, dish_id)
    if not dish:
        return RedirectResponse(f"/menu/dishes/?org_id={ctx['current_org_id'] or ''}", status_code=302)

    parsed_rows = []
    bad_rows = []
    for i, raw_name in enumerate(item_name):
        raw_name = raw_name.strip()
        if not raw_name:
            continue
        pid_str = item_product_id[i].strip() if i < len(item_product_id) else ""
        sadik_str = item_qty_sadik[i].strip() if i < len(item_qty_sadik) else ""
        shkola_str = item_qty_shkola[i].strip() if i < len(item_qty_shkola) else ""
        sadik_val = _to_float(sadik_str)
        shkola_val = _to_float(shkola_str)

        row = {
            "product_id": int(pid_str) if pid_str.isdigit() else None,
            "name": raw_name, "sadik": sadik_val, "shkola": shkola_val,
        }
        parsed_rows.append(row)

        # Строка, которая при сохранении не даст НИ ОДНОГО грамма списания —
        # либо обе колонки пустые, либо текст не распознан как число (опечатка
        # вроде "40г") — раньше сохранялась молча и просто не участвовала в
        # авто-списании, без единого сигнала об этом (найдено advisor'ом 29.07).
        sadik_bad = bool(sadik_str) and sadik_val is None
        shkola_bad = bool(shkola_str) and shkola_val is None
        if sadik_bad or shkola_bad:
            bad_rows.append(f'«{raw_name}» — не распознано число ({sadik_str or shkola_str})')
        elif sadik_val is None and shkola_val is None:
            bad_rows.append(f'«{raw_name}» — не указан вес ни для садика, ни для школы')

    if bad_rows:
        ingredients = (
            db.query(DishIngredient).filter(DishIngredient.dish_id == dish_id).order_by(DishIngredient.id).all()
        )
        ctx.update({
            "dish": dish, "ingredients": ingredients, "return_date": return_date,
            "error": "Не сохранено — проверь строки: " + "; ".join(bad_rows),
            "form_rows": parsed_rows,
        })
        return templates.TemplateResponse("menu/dish_detail.html", ctx)

    db.query(DishIngredient).filter(DishIngredient.dish_id == dish_id).delete(synchronize_session=False)
    for row in parsed_rows:
        db.add(DishIngredient(
            dish_id=dish_id, product_id=row["product_id"], raw_name=row["name"],
            qty_sadik_g=row["sadik"], qty_shkola_g=row["shkola"],
        ))
    db.commit()

    if return_date:
        return RedirectResponse(
            f"/warehouse/writeoff/auto?org_id={ctx['current_org_id'] or ''}&writeoff_date={return_date}",
            status_code=302,
        )
    return RedirectResponse(f"/menu/dishes/{dish_id}?org_id={ctx['current_org_id'] or ''}", status_code=302)
