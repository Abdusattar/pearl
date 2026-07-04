from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import Organization, Student, Transaction
from app.services.billing import get_balances

router = APIRouter(prefix="/income", tags=["income"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def list_income(
    request: Request,
    org_id: str | None = None,
    month: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    org_id = int(org_id) if org_id and org_id.isdigit() else None
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)

    # Все дочерние орги рекурсивно
    all_orgs = db.query(Organization).all()
    def descendants(oid):
        ids = {oid}
        for o in all_orgs:
            if o.parent_id == oid:
                ids |= descendants(o.id)
        return ids
    org_ids = descendants(current_org.id) if current_org else set()

    # Доступные месяцы для фильтра
    months_q = (
        db.query(func.to_char(Transaction.date, 'YYYY-MM').label("m"))
        .filter(Transaction.type == "income", Transaction.deleted_at.is_(None))
        .filter(Transaction.organization_id.in_(org_ids))
        .distinct().order_by(func.to_char(Transaction.date, 'YYYY-MM').desc())
        .all()
    )
    available_months = [r.m for r in months_q]

    # Дефолт — текущий месяц
    if not month:
        month = date.today().strftime("%Y-%m")

    # Основной запрос
    query = (
        db.query(Transaction, Student)
        .outerjoin(Student, Transaction.student_id == Student.id)
        .filter(Transaction.type == "income", Transaction.deleted_at.is_(None))
        .filter(Transaction.organization_id.in_(org_ids))
    )
    if month:
        query = query.filter(func.to_char(Transaction.date, 'YYYY-MM') == month)
    if q:
        query = query.filter(Student.name.ilike(f"%{q}%") | Student.pin.ilike(f"%{q}%"))

    rows = query.order_by(Transaction.date.desc(), Transaction.id.desc()).all()

    student_ids = [r.Student.id for r in rows if r.Student]
    balances = get_balances(db, student_ids)

    total = sum(r.Transaction.amount for r in rows)

    # Месяц для отображения
    month_label = ""
    if month:
        y, m = month.split("-")
        month_names = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                       "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
        month_label = f"{month_names[int(m)]} {y}"

    return templates.TemplateResponse("income/list.html", {
        "request": request,
        "rows": rows,
        "balances": balances,
        "total": total,
        "month": month,
        "month_label": month_label,
        "available_months": available_months,
        "q": q or "",
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "current_user": user,
        "active_page": "income",
    })
