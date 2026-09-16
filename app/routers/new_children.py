"""Новый вход `/new/children`: список детей, карточка, приём наличных (макет блок 5)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.dependencies import get_accessible_orgs, get_current_user
from app.database import get_db
from app.models import Organization, Student
from app.routers.new_buy import WRITE_ROLES, _base_ctx, _site, templates
from app.services import children as svc
from app.services.billing import generate_monthly_charges
from app.services.purchases import default_pocket, pocket_users, site_orgs

router = APIRouter(prefix="/new", tags=["new"])


def _orgs_for(user, site: Organization, db: Session) -> list[Organization]:
    allowed = {o.id for o in get_accessible_orgs(user, db)}
    return [o for o in site_orgs(db, site.id) if o.id in allowed] or site_orgs(db, site.id)


@router.get("/children", response_class=HTMLResponse)
def children_page(request: Request, org: int | None = None, debt: int = 0, q: str | None = None,
                  db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    generate_monthly_charges(db)
    db.commit()
    orgs = _orgs_for(user, site, db)
    current = next((o for o in orgs if o.id == org), None) or next((o for o in orgs if o.type == "kindergarten"), orgs[0])
    data = svc.children_list(db, current, only_debt=bool(debt), q=q)
    ctx = _base_ctx(request, user, site, db, "children")
    ctx.update({"orgs": orgs, "current": current, "data": data, "debt": bool(debt), "q": q or "",
                "can_write": user.role in WRITE_ROLES, "today": date.today()})
    return templates.TemplateResponse("new/children.html", ctx)


@router.get("/children/{student_id}", response_class=HTMLResponse)
def child_page(student_id: int, request: Request, saved: int = 0, cash: int = 0, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    student = db.get(Student, student_id)
    if site is None or student is None or student.organization_id not in {o.id for o in _orgs_for(user, site, db)}:
        return HTMLResponse("Ребёнок не найден", status_code=404)
    ctx = _base_ctx(request, user, site, db, "children")
    ctx.update({"s": student, "card": svc.child_card(db, student), "saved": bool(saved), "cash_open": bool(cash),
                "org": db.get(Organization, student.organization_id), "pockets": pocket_users(db, site.id),
                "can_write": user.role in WRITE_ROLES, "today": date.today(), "error": None, "month": svc.month_name(date.today()),
                "amount": "", "what": "", "pay_date": date.today(), "pocket_user_id": default_pocket(db, site.id, user)})
    return templates.TemplateResponse("new/child.html", ctx)


@router.post("/children/{student_id}/cash", response_class=HTMLResponse)
def child_cash(student_id: int, request: Request, amount: str = Form(""), what: str = Form(""),
               pay_date: str = Form(""), pocket_user_id: str = Form(""), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Наличные принимают сотрудники площадки", status_code=403)
    site = _site(user, db)
    student = db.get(Student, student_id)
    if site is None or student is None:
        return HTMLResponse("Ребёнок не найден", status_code=404)
    try:
        amt = Decimal(amount.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        amt = None
    try:
        d = date.fromisoformat(pay_date) if pay_date else date.today()
    except ValueError:
        d = date.today()
    pocket = int(pocket_user_id) if pocket_user_id.isdigit() else default_pocket(db, site.id, user)
    error = None
    if amt is None or amt <= 0:
        error = "Укажите сумму"
    elif d > date.today():
        error = "Дата не позже сегодняшней"
    if error:
        ctx = _base_ctx(request, user, site, db, "children")
        ctx.update({"s": student, "card": svc.child_card(db, student), "saved": False, "cash_open": True,
                    "org": db.get(Organization, student.organization_id), "pockets": pocket_users(db, site.id),
                    "can_write": True, "today": date.today(), "error": error, "month": svc.month_name(date.today()),
                    "amount": amount, "what": what, "pay_date": d, "pocket_user_id": pocket})
        return templates.TemplateResponse("new/child.html", ctx)
    svc.accept_cash(db, user=user, site_org_id=site.id, student=student, amount=amt, d=d,
                    what=what.strip() or None, pocket_user_id=pocket)
    db.commit()
    return RedirectResponse(f"/new/children/{student.id}?saved=1", status_code=303)
