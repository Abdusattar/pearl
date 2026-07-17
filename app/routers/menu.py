from datetime import date as date_type, timedelta
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import Dish, MenuEntry
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
