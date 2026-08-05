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


def _has_any_expense(db: Session, org_id: int, category_ids: list[int]) -> bool:
    if not category_ids:
        return False
    return (
        db.query(Transaction.id)
        .filter(
            Transaction.organization_id == org_id,
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

    utility_ids = unit_economics.category_subtree_ids(db, "Коммунальные расходы")
    utilities = unit_economics.monthly_expense_total(db, oid, utility_ids, m_start)
    utilities_ever = _has_any_expense(db, oid, utility_ids)

    security_ids = unit_economics.category_subtree_ids(db, "Охрана")
    security = unit_economics.monthly_expense_total(db, oid, security_ids, m_start)
    security_ever = _has_any_expense(db, oid, security_ids)

    ads_ids = unit_economics.category_subtree_ids(db, "Реклама")
    ads = unit_economics.monthly_expense_total(db, oid, ads_ids, m_start)
    ads_ever = _has_any_expense(db, oid, ads_ids)

    billing = unit_economics.monthly_billing_summary(db, oid, m_start)

    total_cost = food_cost + payroll + amortization + utilities + security + ads

    food_note = None
    if not writeoffs_ever:
        food_note = "Списаний питания ещё не было — механизм не запускался"
    elif shared_kitchen:
        food_note = "Общая кухня Школы и Садика Сокулук — сумма по обоим объектам вместе, раздельно не считается"

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
            "note": None if utilities_ever else "Категория заведена, расходов ещё не проводили",
        },
        {
            "key": "security", "title": "Охрана", "value": security,
            "ready": security_ever,
            "note": None if security_ever else "Категория заведена, расходов ещё не проводили",
        },
        {
            "key": "ads", "title": "Реклама", "value": ads,
            "ready": ads_ever,
            "note": None if ads_ever else "Категория заведена, расходов ещё не проводили",
        },
    ]

    return templates.TemplateResponse("dashboard/unit_economics.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": oid,
        "current_org": current_org,
        "active_page": "dashboard",
        "month_label": m_start.strftime("%m.%Y"),
        "blocks": blocks,
        "total_cost": total_cost,
        "billing": billing,
    })
