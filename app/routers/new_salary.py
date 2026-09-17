"""Новый вход `/new/salary`: ведомость и выдача зарплаты (макет 4в, 17.09)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Employee, Transaction
from app.routers.new_buy import WRITE_ROLES, _base_ctx, _site, templates
from app.services import once, repeats
from app.services import salary as svc
from app.services.purchases import default_pocket, pocket_users, site_orgs

router = APIRouter(prefix="/new", tags=["new"])

SALARY_ROLES = WRITE_ROLES + ("founder",)   # учредители смотрят, выдают сотрудники


def _period(s: str | None) -> date:
    try:
        return date.fromisoformat(f"{s}-01") if s else svc.prev_month()
    except ValueError:
        return svc.prev_month()


def _ctx(request, user, site, db, period: date, **kw) -> dict:
    ctx = _base_ctx(request, user, site, db, "cash")
    orgs = svc.payroll_orgs(db, user, site.id)
    ctx.update({
        "period": period, "months": svc.month_choices(), "sheet": svc.sheet(db, orgs, period),
        "today": date.today(), "can_write": user.role in WRITE_ROLES,
        "pockets": pocket_users(db, site.id), "org_names": {o.id: o.name for o in site_orgs(db, site.id)},
        "open_id": kw.get("open_id"), "amount": kw.get("amount"), "d": kw.get("d", date.today()),
        "method": kw.get("method", "hand"), "pocket_user_id": kw.get("pocket_user_id") or default_pocket(db, site.id, user),
        "error": kw.get("error"), "repeat": kw.get("repeat"), "saved": kw.get("saved"),
        "repeat_back": f"/new/salary?month={period:%Y-%m}",
    })
    return ctx


@router.get("/salary", response_class=HTMLResponse)
def salary_page(request: Request, month: str | None = None, open: int | None = None, saved: str | None = None,
                db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in SALARY_ROLES:
        return RedirectResponse("/new/today", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    return templates.TemplateResponse("new/salary.html", _ctx(request, user, site, db, _period(month),
                                                               open_id=open, saved=saved))


def _employee_for(db: Session, user, site, employee_id: int) -> Employee | None:
    e = db.get(Employee, employee_id)
    if e is None or e.organization_id not in {o.id for o in svc.payroll_orgs(db, user, site.id)}:
        return None
    return e


@router.post("/salary/pay", response_class=HTMLResponse)
async def salary_pay(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Зарплату выдают сотрудники площадки", status_code=403)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    form = await request.form()
    g = lambda k: (form.get(k) or "").strip()
    period = _period(g("month"))
    emp = _employee_for(db, user, site, int(g("employee_id"))) if g("employee_id").isdigit() else None
    if emp is None:
        return HTMLResponse("Сотрудник не найден", status_code=404)
    raw_d, method = g("date"), g("method") or "hand"
    pocket_raw = int(g("pocket_user_id")) if g("pocket_user_id").isdigit() else None
    try:
        d = date.fromisoformat(raw_d) if raw_d else date.today()
    except ValueError:
        d = None

    def render(error=None, repeat=None):
        return templates.TemplateResponse("new/salary.html", _ctx(
            request, user, site, db, period, open_id=emp.id, amount=g("amount"), d=d or date.today(),
            method=method, pocket_user_id=pocket_raw, error=error, repeat=repeat))

    try:
        amount = Decimal(g("amount").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return render("Укажите сумму")
    if amount <= 0:
        return render("Укажите сумму")
    if d is None or d > date.today():
        return render("Дата не позже сегодняшней")
    # На руки — из кармана; на карту — переводом со счёта объекта, где человек
    # работает: садик со счёта садика, школа со счёта школы (владелец 17.09).
    pocket = account = None
    if method == "hand" and pocket_raw in {u.id for u in pocket_users(db, site.id)} | {user.id}:
        pocket = pocket_raw
    elif method in ("card", "socfond") and emp.organization_id in {o.id for o in site_orgs(db, site.id)}:
        account = emp.organization_id
    else:
        return render("Как выдали: на руки (из чьего кармана), на карту или это соцфонд?")

    token = once.clean(g("form_token"))
    if done := once.done_url(db, token):
        return RedirectResponse(done, status_code=303)
    if g("repeat_ok") != "1" and (rep := repeats.salary(db, emp.id, period, amount, d)):   # и соцфонд тоже
        return render(None, rep)
    svc.pay(db, user=user, site_org_id=site.id, employee=emp, amount=amount, period=period, d=d,
            pocket_user_id=pocket, account_org_id=account, socfond=method == "socfond")
    url = f"/new/salary?month={period:%Y-%m}&saved=pay"
    once.remember(db, token, user.id, url)
    db.commit()
    return RedirectResponse(url, status_code=303)


@router.post("/salary/{tx_id}/remove")
def salary_remove(tx_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Нет прав", status_code=403)
    site = _site(user, db)
    tx = db.get(Transaction, tx_id)
    emp = _employee_for(db, user, site, tx.employee_id) if site and tx and tx.employee_id else None
    if emp is None:
        return HTMLResponse("Выдача не найдена", status_code=404)
    period = tx.period or svc.prev_month()
    if tx.deleted_at is None:
        svc.remove(db, user=user, tx=tx)
        db.commit()
    return RedirectResponse(f"/new/salary?month={period:%Y-%m}&open={emp.id}&saved=remove", status_code=303)
