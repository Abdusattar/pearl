from datetime import date as date_type, timedelta
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
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


def _next_monday(today: date_type) -> date_type:
    days_ahead = (7 - today.weekday()) % 7
    days_ahead = days_ahead or 7
    return today + timedelta(days=days_ahead)


def _week_days(start: date_type, weeks: int) -> list[date_type]:
    days = []
    for w in range(weeks):
        for i in range(5):  # Пн–Пт, сад без выходных не работает
            days.append(start + timedelta(days=w * 7 + i))
    return days


@router.get("/", response_class=HTMLResponse)
def menu_form(request: Request, org_id: str | None = None, start: str | None = None,
              weeks: int = 1, db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    today = date_type.today()
    start_date = date_type.fromisoformat(start) if start else _next_monday(today)
    weeks = max(1, min(weeks, 4))
    days = _week_days(start_date, weeks)

    existing = (
        db.query(MenuEntry)
        .filter(MenuEntry.organization_id == ctx["current_org"].id,
                MenuEntry.date.in_(days))
        .all()
    ) if ctx["current_org"] else []

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

    day_cards = [
        {
            "date": d,
            "label": f"{WEEKDAY_NAMES[d.weekday()]}, {d.strftime('%d.%m')}",
            "meals": {mt: entries_by_day.get(d, {}).get(mt, []) for mt in MEAL_TYPES},
            "has_entries": d in entries_by_day,
        }
        for d in days
    ]

    ctx.update({
        "day_cards": day_cards, "meal_types": MEAL_TYPES, "chips_by_meal": chips_by_meal,
        "start_date": start_date.isoformat(), "weeks": weeks,
        "next_start": (start_date + timedelta(days=weeks * 7)).isoformat(),
        "warning_date": (
            f"{WEEKDAY_NAMES[warning_date.weekday()]}, {warning_date.strftime('%d.%m')}"
        ) if warning_date else None,
    })
    return templates.TemplateResponse("menu/form.html", ctx)


@router.post("/", response_class=HTMLResponse)
def menu_save(
    request: Request,
    org_id: str | None = Form(None),
    start: str = Form(...),
    weeks: int = Form(1),
    entry_date: List[str] = Form(default=[]),
    entry_meal: List[str] = Form(default=[]),
    entry_dish: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    start_date = date_type.fromisoformat(start)
    days = _week_days(start_date, max(1, min(weeks, 4)))

    # пересохраняем целиком диапазон недель — проще и предсказуемее частичного upsert
    db.query(MenuEntry).filter(
        MenuEntry.organization_id == ctx["current_org"].id,
        MenuEntry.date.in_(days),
    ).delete(synchronize_session=False)

    for i, d_str in enumerate(entry_date):
        dish_name = entry_dish[i].strip() if i < len(entry_dish) else ""
        if not dish_name:
            continue
        dish = get_or_create_dish(db, dish_name)
        db.add(MenuEntry(
            organization_id=ctx["current_org"].id,
            date=date_type.fromisoformat(d_str),
            meal_type=entry_meal[i],
            dish_id=dish.id,
            created_by=ctx["current_user"].id,
        ))
    db.commit()
    return RedirectResponse(
        f"/menu/?org_id={ctx['current_org_id']}&start={start}&weeks={weeks}", status_code=302
    )


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
