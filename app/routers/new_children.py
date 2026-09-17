"""Новый вход `/new/children`: список детей, карточка, приём наличных (макет блок 5)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.dependencies import get_accessible_orgs, get_current_user
from app.database import get_db
from app.models import Group, Organization, Student
from app.routers.new_buy import WRITE_ROLES, _base_ctx, _site, templates
from app.services import children as svc
from app.services import once, repeats
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


@router.get("/children/add", response_class=HTMLResponse)
def child_add_form(request: Request, org: int | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    orgs = _orgs_for(user, site, db)
    current = next((o for o in orgs if o.id == org), None) or orgs[0]
    ctx = _base_ctx(request, user, site, db, "children")
    ctx.update({"orgs": orgs, "current": current, "groups": _groups(db, current.id), "today": date.today(),
                "form": {}, "similar": [], "error": None, "can_write": user.role in WRITE_ROLES})
    return templates.TemplateResponse("new/child_add.html", ctx)


@router.get("/children/{student_id}", response_class=HTMLResponse)
def child_page(student_id: int, request: Request, saved: int = 0, cash: int = 0, err: str | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    student = db.get(Student, student_id)
    if site is None or student is None or student.organization_id not in {o.id for o in _orgs_for(user, site, db)}:
        return HTMLResponse("Ребёнок не найден", status_code=404)
    ctx = _base_ctx(request, user, site, db, "children")
    ctx.update({"s": student, "card": svc.child_card(db, student), "saved": saved, "cash_open": bool(cash),
                "org": db.get(Organization, student.organization_id), "pockets": pocket_users(db, site.id),
                "groups": _groups(db, student.organization_id),
                "can_write": user.role in WRITE_ROLES, "today": date.today(), "error": err, "month": svc.month_name(date.today()),
                "amount": "", "what": "", "pay_date": date.today(), "pocket_user_id": default_pocket(db, site.id, user)})
    return templates.TemplateResponse("new/child.html", ctx)


@router.post("/children/{student_id}/cash", response_class=HTMLResponse)
def child_cash(student_id: int, request: Request, amount: str = Form(""), what: str = Form(""),
               pay_date: str = Form(""), pocket_user_id: str = Form(""), repeat_ok: str = Form(""),
               form_token: str = Form(""), db: Session = Depends(get_db)):
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
    def render(error, repeat=None):
        ctx = _base_ctx(request, user, site, db, "children")
        ctx.update({"s": student, "card": svc.child_card(db, student), "saved": 0, "cash_open": True,
                    "org": db.get(Organization, student.organization_id), "pockets": pocket_users(db, site.id),
                    "groups": _groups(db, student.organization_id),
                    "can_write": True, "today": date.today(), "error": error, "month": svc.month_name(date.today()),
                    "amount": amount, "what": what, "pay_date": d, "pocket_user_id": pocket,
                    "repeat": repeat, "repeat_back": f"/new/children/{student.id}"})
        return templates.TemplateResponse("new/child.html", ctx)

    if error:
        return render(error)
    token = once.clean(form_token)
    if done := once.done_url(db, token):
        return RedirectResponse(done, status_code=303)
    if repeat_ok != "1" and (rep := repeats.child_cash(db, student.id, amt, d)):
        return render(None, rep)
    svc.accept_cash(db, user=user, site_org_id=site.id, student=student, amount=amt, d=d,
                    what=what.strip() or None, pocket_user_id=pocket)
    url = f"/new/children/{student.id}?saved=1"
    once.remember(db, token, user.id, url)
    db.commit()
    return RedirectResponse(url, status_code=303)


# ── добавление, скидка, статус, группа ───────────────────────────────────

def _groups(db: Session, org_id: int) -> list[Group]:
    return db.query(Group).filter(Group.organization_id == org_id, Group.deleted_at.is_(None)).order_by(Group.name).all()


@router.post("/children/add", response_class=HTMLResponse)
async def child_add(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Детей заводят сотрудники", status_code=403)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    form = await request.form()

    def g(k):
        return (form.get(k) or "").strip()

    orgs = _orgs_for(user, site, db)
    org_id = int(g("org_id")) if g("org_id").isdigit() else orgs[0].id
    current = next((o for o in orgs if o.id == org_id), orgs[0])
    data = {k: g(k) for k in ("last_name", "first_name", "patronymic", "group_id", "parent_name", "parent_contact", "inn", "start")}

    def render(error=None, similar=None):
        ctx = _base_ctx(request, user, site, db, "children")
        ctx.update({"orgs": orgs, "current": current, "groups": _groups(db, current.id), "today": date.today(),
                    "form": data, "similar": similar or [], "error": error, "can_write": True})
        return templates.TemplateResponse("new/child_add.html", ctx)

    if not data["last_name"] or not data["first_name"]:
        return render("Фамилия и имя обязательны")
    try:
        start = date.fromisoformat(data["start"]) if data["start"] else date.today()
    except ValueError:
        return render("Дата зачисления не читается")
    token = once.clean(g("form_token"))
    if done := once.done_url(db, token):
        return RedirectResponse(done, status_code=303)
    if g("dup_ok") != "1":
        similar = svc.similar_children(db, current.id, data["last_name"], data["first_name"], data["inn"] or None)
        if similar:
            return render(None, similar)
    gid = int(data["group_id"]) if data["group_id"].isdigit() else None
    student = svc.add_child(db, user=user, org_id=current.id, last_name=data["last_name"], first_name=data["first_name"],
                            patronymic=data["patronymic"], group_id=gid, parent_name=data["parent_name"],
                            parent_contact=data["parent_contact"], inn=data["inn"] or None, start=start)
    url = f"/new/children/{student.id}?saved=2"
    once.remember(db, token, user.id, url)
    db.commit()
    return RedirectResponse(url, status_code=303)


def _student_for(db: Session, user, site, student_id: int) -> Student | None:
    student = db.get(Student, student_id)
    if student is None or student.organization_id not in {o.id for o in _orgs_for(user, site, db)}:
        return None
    return student


@router.post("/children/{student_id}/discount")
def child_discount(student_id: int, request: Request, amount: str = Form(""), reason: str = Form(""), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Нет прав", status_code=403)
    site = _site(user, db)
    student = _student_for(db, user, site, student_id) if site else None
    if student is None:
        return HTMLResponse("Ребёнок не найден", status_code=404)
    try:
        val = float(amount.replace(" ", "").replace(",", ".")) if amount.strip() else 0.0
        svc.set_discount(db, user=user, student=student, amount=val, reason=reason)
    except ValueError as e:
        return RedirectResponse(f"/new/children/{student.id}?err={e}", status_code=303)
    db.commit()
    return RedirectResponse(f"/new/children/{student.id}?saved=3", status_code=303)


@router.post("/children/{student_id}/status")
def child_status(student_id: int, request: Request, status: str = Form(...), on_date: str = Form(""),
                 reason: str = Form(""), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Нет прав", status_code=403)
    site = _site(user, db)
    student = _student_for(db, user, site, student_id) if site else None
    if student is None:
        return HTMLResponse("Ребёнок не найден", status_code=404)
    try:
        d = date.fromisoformat(on_date) if on_date else date.today()
        svc.set_status(db, user=user, student=student, status=status, d=d, reason=reason.strip() or None)
    except ValueError as e:
        return RedirectResponse(f"/new/children/{student.id}?err={e}", status_code=303)
    db.commit()
    return RedirectResponse(f"/new/children/{student.id}?saved=4", status_code=303)


@router.post("/children/{student_id}/group")
def child_group(student_id: int, request: Request, group_id: str = Form(""), on_date: str = Form(""), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Нет прав", status_code=403)
    site = _site(user, db)
    student = _student_for(db, user, site, student_id) if site else None
    if student is None or not group_id.isdigit():
        return HTMLResponse("Ребёнок или группа не найдены", status_code=404)
    d = date.fromisoformat(on_date) if on_date else date.today()
    svc.move_group(db, user=user, student=student, group_id=int(group_id), d=d)
    db.commit()
    return RedirectResponse(f"/new/children/{student.id}?saved=5", status_code=303)
