from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_accessible_orgs, get_current_user, resolve_org
from app.models import Asset, Employee, Transaction, WriteOff
from app.services import unit_economics
from app.services.recurring_expenses import month_start

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _guard(request: Request, db: Session):
    """Доступ owner/founder/staff — тот же круг, что у /employees/ (10.07,
    расширено 27.07 под Махабат): дашборд показывает ФОТ и вообще всю
    финансовую картину, секретность здесь не имеет смысла для тех, кто и так
    вводит эти данные."""
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=302)
    if user.role not in ("owner", "founder", "staff"):
        return None, RedirectResponse("/", status_code=302)
    return user, None


def _campus_note(ever: bool, shared_kitchen: bool, title: str) -> str | None:
    if not ever:
        return "Категория заведена, расходов ещё не проводили"
    if shared_kitchen:
        return f"«{title}» считается на весь кампус (Школа+Садик Сокулук вместе), не по одному объекту"
    return None


def _has_any_expense(db: Session, org_ids: set[int], category_ids: list[int]) -> bool:
    if not category_ids or not org_ids:
        return False
    return (
        db.query(Transaction.id)
        .filter(
            Transaction.organization_id.in_(org_ids),
            Transaction.type == "expense",
            Transaction.category_id.in_(category_ids),
            Transaction.deleted_at.is_(None),
        )
        .first()
        is not None
    )


@router.get("/unit-economics", response_class=HTMLResponse)
def unit_economics_view(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)
    if not current_org:
        return RedirectResponse("/", status_code=302)

    oid = current_org.id
    m_start = month_start()

    kitchen_ids = unit_economics.kitchen_group(oid)
    food_cost = unit_economics.monthly_food_cost(db, oid, m_start)
    writeoffs_ever = (
        db.query(WriteOff.id)
        .filter(WriteOff.organization_id.in_(kitchen_ids), WriteOff.deleted_at.is_(None))
        .first()
        is not None
    )
    shared_kitchen = len(kitchen_ids) > 1

    payroll = unit_economics.monthly_payroll(db, oid)
    employees_count = (
        db.query(Employee.id)
        .filter(Employee.organization_id == oid, Employee.status == "active")
        .count()
    )

    amortization = unit_economics.monthly_amortization(db, oid, m_start)
    assets_count = db.query(Asset.id).filter(Asset.organization_id == oid, Asset.deleted_at.is_(None)).count()

    # Коммуналка/Охрана — тоже на весь кампус (Школа+Садик Сокулук, один
    # двор/здание), считаем через kitchen_ids по той же причине, что и
    # питание: право провести счёт под конкретным id сейчас зависит от роли
    # (Махабат/пилот — Садик Сокулук, Айжан — Школа), реальная проводка
    # может уйти под любой из двух.
    utility_ids = unit_economics.category_subtree_ids(db, "Коммунальные расходы")
    utilities = unit_economics.monthly_expense_total(db, kitchen_ids, utility_ids, m_start)
    utilities_ever = _has_any_expense(db, kitchen_ids, utility_ids)

    security_ids = unit_economics.category_subtree_ids(db, "Охрана")
    security = unit_economics.monthly_expense_total(db, kitchen_ids, security_ids, m_start)
    security_ever = _has_any_expense(db, kitchen_ids, security_ids)

    # Реклама — не привязана к конкретному зданию (SMM/продвижение целиком),
    # но своей орг-принадлежности у неё нет по умолчанию — оставляем per-org,
    # не кампус: единого основания считать её общей нет (в отличие от еды/
    # коммуналки/охраны, которые физически про одно здание).
    ads_ids = unit_economics.category_subtree_ids(db, "Реклама")
    ads = unit_economics.monthly_expense_total(db, {oid}, ads_ids, m_start)
    ads_ever = _has_any_expense(db, {oid}, ads_ids)

    billing = unit_economics.monthly_billing_summary(db, oid, m_start)

    total_cost = food_cost + payroll + amortization + utilities + security + ads

    food_note = None
    if not writeoffs_ever:
        food_note = "Списаний питания ещё не было — механизм не запускался"
    elif shared_kitchen:
        # Не "сумма по обоим объектам" — сейчас в Школе нет своих групп/детей
        # (headcount по классам всегда 0), поэтому вся сумма списаний — это
        # фактическое потребление Садика. Общий на два объекта — только
        # закуп/склад, из которого берётся средняя цена (advisor 05.08).
        food_note = "Пока это списания только Садика Сокулук (в Школе ещё нет своих групп/детей) — цена усреднена по общему складу, закуп идёт под обоими объектами"

    blocks = [
        {
            "key": "food", "title": "Питание / склад", "value": food_cost,
            "ready": writeoffs_ever,
            "note": food_note,
        },
        {
            "key": "payroll", "title": "ФОТ", "value": payroll,
            "ready": employees_count > 0,
            "note": None if employees_count > 0 else "Сотрудники не заведены — /employees/ пуст для этого объекта",
        },
        {
            "key": "amortization", "title": "Амортизация", "value": amortization,
            "ready": assets_count > 0,
            "note": None if assets_count > 0 else "Активы не заведены — /assets/ пуст для этого объекта",
        },
        {
            "key": "utilities", "title": "Коммуналка", "value": utilities,
            "ready": utilities_ever,
            "note": _campus_note(utilities_ever, shared_kitchen, "Коммуналка"),
        },
        {
            "key": "security", "title": "Охрана", "value": security,
            "ready": security_ever,
            "note": _campus_note(security_ever, shared_kitchen, "Охрана"),
        },
        {
            "key": "ads", "title": "Реклама", "value": ads,
            "ready": ads_ever,
            "note": None if ads_ever else "Категория заведена, расходов ещё не проводили",
        },
    ]

    total_note = (
        "Питание/Коммуналка/Охрана — общие на весь кампус (Школа+Садик Сокулук). "
        "Если смотреть Школу и Садик по очереди и сложить два «Итого» — эти суммы посчитаются дважды."
        if shared_kitchen else None
    )

    return templates.TemplateResponse("dashboard/unit_economics.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": oid,
        "current_org": current_org,
        "total_note": total_note,
        "active_page": "dashboard",
        "month_label": m_start.strftime("%m.%Y"),
        "blocks": blocks,
        "total_cost": total_cost,
        "billing": billing,
    })
