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
from app.models import Dish, DishIngredient, DishMergeDismissed, ExpenseCategory, MenuEntry, WriteOff
from app.services.dishes import get_or_create_dish, find_duplicate_candidates
from app.services.products import UNITS, CATEGORIES, UNITS_NEED_GRAMS_PER_UNIT

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

    # Мягкое напоминание (30.07, смягчено с жёсткого редиректа в тот же день) —
    # авто-списание считает каждое блюдо по своему dish_id независимо, дубль
    # в каталоге не портит цифры склада, только путает человека в списке и
    # дробит статистику "сколько раз подавали". Значит не блокировать работу
    # с меню, просто напоминать, пока в очереди что-то есть.
    pending_duplicates = len(find_duplicate_candidates(db))

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
        "day_cards": day_cards, "meal_types": MEAL_TYPES,
        "start_date": start_date.isoformat(),
        "prev_start": (start_date - timedelta(days=7)).isoformat(),
        "next_start": (start_date + timedelta(days=7)).isoformat(),
        "week_strip": week_strip,
        "warning_date": (
            f"{WEEKDAY_NAMES[warning_date.weekday()]}, {warning_date.strftime('%d.%m')}"
        ) if warning_date else None,
        "pending_duplicates": pending_duplicates,
    })
    return templates.TemplateResponse("menu/form.html", ctx)


@router.post("/day", response_class=HTMLResponse)
def menu_day_save(
    request: Request,
    org_id: str | None = Form(None),
    date: str = Form(...),
    meal: List[str] = Form(default=[]),
    dish_id: List[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Автосохранение одного дня — вызывается фронтом сразу при добавлении/
    удалении блюда, без общей кнопки «Сохранить» (решено 17.07, см.
    wiki/blueprints/menu_module.md). Затрагивает только эту дату — соседние
    дни в диапазоне не трогаем.

    С 29.07 в меню можно добавить только блюдо, у которого уже есть рецепт
    (dish_ingredients) — свободный ввод текста и создание блюда "на лету"
    отсюда убраны (см. get_or_create_dish, теперь только для /dishes/new).
    Причина: без рецепта авто-списание по блюду молча давало 0 — источник
    большей части задвоений/мусора в каталоге, который потом разгребали
    вручную. Фронт уже не даёт добавить нереципированное блюдо, но сервер
    перепроверяет сам — id пришёл из формы, доверять клиенту нельзя."""
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

    # Блюда, которые на эту дату уже были в меню ДО этого сохранения (могли
    # попасть туда ещё до 29.07, когда рецепт не требовался). Каждое
    # автосохранение дня пересылает ВСЕ текущие чипы, включая эти старые —
    # без этой брони новая проверка рецепта молча стирала бы уже стоящие в
    # меню блюда при любой правке дня (найдено advisor'ом 29.07 и
    # воспроизведено: добавление одного блюда с рецептом гасило все старые
    # без рецепта в том же дне). Новые добавления без рецепта по-прежнему
    # блокируются — бронь только для того, что реально уже стояло.
    already_had = {
        row[0] for row in db.query(MenuEntry.dish_id).filter(
            MenuEntry.organization_id == ctx["current_org"].id,
            MenuEntry.date == d,
        )
    }

    db.query(MenuEntry).filter(
        MenuEntry.organization_id == ctx["current_org"].id,
        MenuEntry.date == d,
    ).delete(synchronize_session=False)

    for i, meal_type in enumerate(meal):
        did_str = dish_id[i].strip() if i < len(dish_id) else ""
        if not did_str.isdigit():
            continue
        did = int(did_str)
        has_recipe = db.query(DishIngredient.id).filter(DishIngredient.dish_id == did).first()
        if not has_recipe and did not in already_had:
            continue
        db.add(MenuEntry(
            organization_id=ctx["current_org"].id,
            date=d,
            meal_type=meal_type,
            dish_id=did,
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
def dishes_search(q: str = "", only_with_recipe: bool = False, db: Session = Depends(get_db)):
    from app.services.dishes import search_dishes
    results = search_dishes(db, q)
    if only_with_recipe and results:
        reciped_ids = {
            row[0] for row in db.query(DishIngredient.dish_id)
            .filter(DishIngredient.dish_id.in_([r["id"] for r in results]))
            .distinct()
        }
        results = [r for r in results if r["id"] in reciped_ids]
    return results


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
    rows = [
        {"id": d.id, "name": d.name, "ingredient_count": counts.get(d.id, 0)}
        for d in dishes
    ]
    if q:
        q_low = q.strip().lower()
        rows = [r for r in rows if q_low in r["name"].lower()]

    duplicate_count = len(find_duplicate_candidates(db, limit=1000))
    ctx.update({"rows": rows, "q": q or "", "duplicate_count": duplicate_count})
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


@router.get("/dishes/duplicates", response_class=HTMLResponse)
def dish_duplicates(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    ctx.update({"candidates": find_duplicate_candidates(db)})
    return templates.TemplateResponse("menu/dishes_duplicates.html", ctx)


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
    ctx.update({
        "dish": dish, "ingredients": ingredients, "return_date": return_date, "error": None,
        "units": UNITS, "categories": CATEGORIES, "expense_categories": _leaf_expense_categories(db),
        "units_need_grams": sorted(UNITS_NEED_GRAMS_PER_UNIT),
    })
    return templates.TemplateResponse("menu/dish_detail.html", ctx)


def _to_float(s: str) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return None


def _leaf_expense_categories(db: Session) -> list:
    """Только листья дерева статей расходов — родительский узел («Товары» и
    т.п.) не годится как статья конкретного товара, только группировка."""
    parent_ids = {
        row[0] for row in db.query(ExpenseCategory.parent_id).filter(ExpenseCategory.parent_id.isnot(None)).distinct()
    }
    q = db.query(ExpenseCategory)
    if parent_ids:
        q = q.filter(~ExpenseCategory.id.in_(parent_ids))
    return q.order_by(ExpenseCategory.name).all()


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
        elif row["product_id"] is None:
            # Строка без связи со складом раньше сохранялась молча и просто
            # выпадала из авто-списания — тот же класс «тихого нуля», что и
            # для нераспознанных чисел (найдено 30.07). Выбор товара из
            # подсказки строго обязателен, свободный текст не сохраняется.
            bad_rows.append(f'«{raw_name}» — товар не выбран из подсказки (не привязан к складу)')

    if bad_rows:
        ingredients = (
            db.query(DishIngredient).filter(DishIngredient.dish_id == dish_id).order_by(DishIngredient.id).all()
        )
        ctx.update({
            "dish": dish, "ingredients": ingredients, "return_date": return_date,
            "error": "Не сохранено — проверь строки: " + "; ".join(bad_rows),
            "form_rows": parsed_rows,
            "units": UNITS, "categories": CATEGORIES, "expense_categories": _leaf_expense_categories(db),
        "units_need_grams": sorted(UNITS_NEED_GRAMS_PER_UNIT),
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


@router.post("/dishes/{dish_id}/rename", response_class=HTMLResponse)
def dish_rename(request: Request, dish_id: int, org_id: str | None = Form(None),
                 name: str = Form(...), db: Session = Depends(get_db)):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    dish = db.get(Dish, dish_id)
    new_name = name.strip()
    if dish and new_name:
        clash = (
            db.query(Dish)
            .filter(func.lower(Dish.name) == new_name.lower(), Dish.id != dish_id)
            .first()
        )
        if clash:
            ingredients = (
                db.query(DishIngredient).filter(DishIngredient.dish_id == dish_id).order_by(DishIngredient.id).all()
            )
            ctx.update({
                "dish": dish, "ingredients": ingredients, "return_date": None, "error": None,
                "rename_error": f"Уже есть блюдо «{clash.name}» — переименовать в такое же нельзя. "
                                 f"Если это одно и то же блюдо, слей их на экране «Похожие блюда».",
                "rename_value": new_name,
            })
            return templates.TemplateResponse("menu/dish_detail.html", ctx)
        dish.name = new_name
        db.commit()
    return RedirectResponse(f"/menu/dishes/{dish_id}?org_id={ctx['current_org_id'] or ''}", status_code=302)


@router.post("/dishes/{dish_id}/delete")
def dish_delete(request: Request, dish_id: int, org_id: str | None = Form(None),
                 db: Session = Depends(get_db)):
    """Удаляет блюдо безусловно, независимо от того, использовалось ли оно
    (решено 29.07 — одна простая кнопка, без деления "безопасно/полностью",
    чтобы можно было чистить каталог без раздумий). Каскад:
    - MenuEntry этого блюда удаляются — те дни/приёмы пищи в Меню станут
      пустыми, их нужно будет заново заполнить (осознанно, не пытаемся
      угадать замену)
    - WriteOff НЕ удаляются — это факт "что списали со склада", он не
      перестаёт быть правдой. dish_id в них становится NULL (колонка для
      этого и nullable) — списание остаётся, просто без привязки к блюду
    Для похожих блюд, которые правда одно и то же — слияние на
    /dishes/duplicates сохраняет историю лучше, чем удаление."""
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    dish = db.get(Dish, dish_id)
    if dish:
        db.query(MenuEntry).filter(MenuEntry.dish_id == dish_id).delete(synchronize_session=False)
        db.query(WriteOff).filter(WriteOff.dish_id == dish_id).update({"dish_id": None})
        db.query(DishIngredient).filter(DishIngredient.dish_id == dish_id).delete(synchronize_session=False)
        db.query(DishMergeDismissed).filter(
            (DishMergeDismissed.dish_id_a == dish_id) | (DishMergeDismissed.dish_id_b == dish_id)
        ).delete(synchronize_session=False)
        db.delete(dish)
        db.commit()
    return RedirectResponse(f"/menu/dishes/?org_id={ctx['current_org_id'] or ''}", status_code=302)


@router.post("/dishes/duplicates/merge")
def dish_duplicates_merge(
    request: Request,
    org_id: str | None = Form(None),
    keep_id: int = Form(...),
    drop_id: int = Form(...),
    db: Session = Depends(get_db),
):
    """Слияние: keep_id остаётся, drop_id исчезает. Переносим ВСЁ, что на
    dishes.id ссылается FK (проверено запросом к information_schema 29.07,
    после того как advisor поймал, что первая версия трогала только
    MenuEntry/DishIngredient и падала на WriteOff и на собственной новой
    dish_merge_dismissed):
    - MenuEntry: репойнтим, но сперва дедуп — если на тот же (org, дата,
      приём пищи) уже есть запись с keep_id, drop-строка просто лишняя и
      удаляется, а не дублирует чип в Меню.
    - WriteOff: репойнтим без разбора — это исторический факт "что
      готовили", а не карточка блюда, обеим сторонам всё равно один и тот
      же keep_id корректен.
    - DishIngredient: если у keep_id рецепта нет, а у drop_id есть —
      переносим (не теряем единственный существующий рецепт), иначе рецепт
      drop_id удаляется вместе с ним — Махабат уже выбрала правильную
      карточку.
    - DishMergeDismissed: строки, где встречается drop_id, удаляются —
      иначе они держат мёртвый FK и сами становятся следующей причиной 500."""
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    keep = db.get(Dish, keep_id)
    drop = db.get(Dish, drop_id)
    if keep and drop and keep.id != drop.id:
        keep_slots = {
            (row.organization_id, row.date, row.meal_type)
            for row in db.query(MenuEntry).filter(MenuEntry.dish_id == keep_id)
        }
        drop_entries = db.query(MenuEntry).filter(MenuEntry.dish_id == drop_id).all()
        for entry in drop_entries:
            slot = (entry.organization_id, entry.date, entry.meal_type)
            if slot in keep_slots:
                db.delete(entry)  # keep_id уже стоит в этот день/приём пищи — не дублируем
            else:
                entry.dish_id = keep_id
                keep_slots.add(slot)

        db.query(WriteOff).filter(WriteOff.dish_id == drop_id).update({"dish_id": keep_id})

        keep_has_recipe = db.query(DishIngredient.id).filter(DishIngredient.dish_id == keep_id).first()
        if keep_has_recipe:
            db.query(DishIngredient).filter(DishIngredient.dish_id == drop_id).delete(synchronize_session=False)
        else:
            db.query(DishIngredient).filter(DishIngredient.dish_id == drop_id).update({"dish_id": keep_id})

        db.query(DishMergeDismissed).filter(
            (DishMergeDismissed.dish_id_a == drop_id) | (DishMergeDismissed.dish_id_b == drop_id)
        ).delete(synchronize_session=False)

        db.delete(drop)
        db.commit()
    return RedirectResponse(f"/menu/dishes/duplicates?org_id={ctx['current_org_id'] or ''}", status_code=302)


@router.post("/dishes/duplicates/dismiss")
def dish_duplicates_dismiss(
    request: Request,
    org_id: str | None = Form(None),
    dish_a_id: int = Form(...),
    dish_b_id: int = Form(...),
    db: Session = Depends(get_db),
):
    ctx = _base_ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    lo, hi = sorted((dish_a_id, dish_b_id))
    exists = (
        db.query(DishMergeDismissed.id)
        .filter(DishMergeDismissed.dish_id_a == lo, DishMergeDismissed.dish_id_b == hi)
        .first()
    )
    if not exists:
        db.add(DishMergeDismissed(dish_id_a=lo, dish_id_b=hi))
        db.commit()
    return RedirectResponse(f"/menu/dishes/duplicates?org_id={ctx['current_org_id'] or ''}", status_code=302)
