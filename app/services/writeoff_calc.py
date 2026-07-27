from datetime import date as date_type

from sqlalchemy.orm import Session

from app.models import Attendance, DishIngredient, Enrollment, Group, MenuEntry, Product, Student

# unit товара -> как перевести граммы рецепта в это количество склада.
# кг/л: граммы/1000 (для л — допущение плотность ≈1 г/мл, справедливо для молока/масла).
# шт: нужен Product.grams_per_unit (вес 1 шт.) — для Хлеба/Яйца/Чая он пока не известен
# без реального взвешивания, поэтому явно НЕ гадаем, а просим ввести вручную (27.07).
GRAMS_DIVISOR = {"кг": 1000, "л": 1000, "г": 1}


def compute_day_draft(db: Session, org_id: int, target_date: date_type) -> dict:
    """Расчёт-черновик списания на дату: меню × норма из dish_ingredients ×
    фактическая явка (садик/школа). Ничего не пишет в БД — только считает."""
    groups = (
        db.query(Group)
        .filter(Group.organization_id == org_id, Group.deleted_at.is_(None))
        .all()
    )
    absent_ids = {
        a.student_id for a in db.query(Attendance).filter(
            Attendance.organization_id == org_id,
            Attendance.date == target_date,
            Attendance.present.is_(False),
        )
    }
    attendance_marked = db.query(Attendance.id).filter(
        Attendance.organization_id == org_id, Attendance.date == target_date,
    ).first() is not None

    headcount = {"kindergarten_group": 0, "class": 0}
    for g in groups:
        student_ids = [
            sid for (sid,) in db.query(Student.id)
            .join(Enrollment, Enrollment.student_id == Student.id)
            .filter(
                Enrollment.group_id == g.id,
                Enrollment.end_date.is_(None),
                Enrollment.start_date <= target_date,
                Student.status == "active",
                Student.deleted_at.is_(None),
            )
        ]
        present = sum(1 for sid in student_ids if sid not in absent_ids)
        headcount[g.type] = headcount.get(g.type, 0) + present

    entries = (
        db.query(MenuEntry)
        .filter(MenuEntry.organization_id == org_id, MenuEntry.date == target_date)
        .all()
    )
    dish_ids = [e.dish_id for e in entries]

    ing_rows = (
        db.query(DishIngredient).filter(DishIngredient.dish_id.in_(dish_ids)).all()
        if dish_ids else []
    )

    # агрегируем граммы по товару (None = сырьё ещё не привязано к Product)
    agg: dict = {}
    for ing in ing_rows:
        total_g = (
            float(ing.qty_sadik_g or 0) * headcount.get("kindergarten_group", 0)
            + float(ing.qty_shkola_g or 0) * headcount.get("class", 0)
        )
        if total_g <= 0:
            continue
        bucket = agg.setdefault(ing.product_id, {"raw_names": set(), "total_g": 0.0})
        bucket["raw_names"].add(ing.raw_name)
        bucket["total_g"] += total_g

    items = []
    unlinked = []
    for pid, bucket in agg.items():
        if pid is None:
            unlinked.append({
                "names": sorted(bucket["raw_names"]),
                "total_g": round(bucket["total_g"], 1),
            })
            continue
        product = db.get(Product, pid)
        unit = (product.unit or "кг").strip()
        total_g = bucket["total_g"]
        quantity = None
        needs_manual = False
        if unit in GRAMS_DIVISOR:
            quantity = round(total_g / GRAMS_DIVISOR[unit], 3)
        elif unit == "шт" and product.grams_per_unit:
            quantity = round(total_g / float(product.grams_per_unit), 2)
        else:
            needs_manual = True
        items.append({
            "product_id": pid, "name": product.name, "unit": unit,
            "quantity": quantity, "needs_manual": needs_manual,
            "total_g": round(total_g, 1),
        })
    items.sort(key=lambda x: x["name"])
    unlinked.sort(key=lambda x: -x["total_g"])

    return {
        "headcount": headcount,
        "total_present": headcount.get("kindergarten_group", 0) + headcount.get("class", 0),
        "attendance_marked": attendance_marked,
        "dish_names": [e.dish.name for e in entries],
        "rows": items,
        "unlinked": unlinked,
    }
