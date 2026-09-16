"""Новый вход: лента расходов `/new/expenses` (2а) и оплата поставщику `/new/pay` (2в/2д)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Supplier
from app.routers.new_buy import WRITE_ROLES, _base_ctx, _site, templates
from app.services import ledger as svc
from app.services.purchases import default_pocket, founders, pocket_users, site_orgs
from app.services.supplier_ledger import get_supplier_balance
from app.services.today import supplier_debts

router = APIRouter(prefix="/new", tags=["new"])

MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
              "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


@router.get("/expenses", response_class=HTMLResponse)
def expenses_feed(request: Request, month: str | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    first, last, key = svc.month_bounds(month)
    days, totals = svc.month_rows(db, site.id, first, last)
    prev = (first.replace(day=1) - date.resolution).replace(day=1)
    nxt = (last + date.resolution)
    debts = supplier_debts(db, site.id)
    ctx = _base_ctx(request, user, site, db, "expenses")
    ctx.update({
        "days": days, "totals": totals, "month_key": key, "month_name": MONTHS_NOM[first.month - 1],
        "prev_key": prev.strftime("%Y-%m"), "next_key": nxt.strftime("%Y-%m") if nxt <= date.today() else None,
        "debt_total": sum((d["debt"] for d in debts), Decimal("0")), "debts": debts[:6],
        "can_write": user.role in WRITE_ROLES,
    })
    return templates.TemplateResponse("new/expenses.html", ctx)


def _pay_ctx(request, user, site, db, supplier: Supplier | None, **kw) -> dict:
    ctx = _base_ctx(request, user, site, db, "expenses")
    debts = supplier_debts(db, site.id)
    ctx.update({
        "supplier": supplier, "debts": debts,
        "balance": float(get_supplier_balance(db, supplier.id)) if supplier else 0.0,
        "pockets": pocket_users(db, site.id), "site_orgs": site_orgs(db, site.id),
        "today": date.today(), "can_write": user.role in WRITE_ROLES,
        "amount": kw.get("amount", ""), "source": kw.get("source", "cash"), "payer_id": kw.get("payer_id", default_pocket(db, site.id, user)),
        "account_org_id": kw.get("account_org_id"), "pay_date": kw.get("pay_date", date.today()),
        "comment": kw.get("comment", ""), "error": kw.get("error"), "saved": kw.get("saved"),
    })
    return ctx


@router.get("/pay", response_class=HTMLResponse)
def pay_form(request: Request, supplier: int | None = None, saved: int | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    sup = db.get(Supplier, supplier) if supplier else None
    ctx = _pay_ctx(request, user, site, db, sup, saved=saved)
    if sup:
        ctx["amount"] = (f"{ctx['balance']:.2f}".rstrip("0").rstrip(".").replace(".", ",")) if ctx["balance"] > 0 else ""
    return templates.TemplateResponse("new/pay.html", ctx)


@router.post("/pay", response_class=HTMLResponse)
def pay_submit(request: Request, supplier_id: int = Form(...), amount: str = Form(""), source: str = Form("cash"),
               payer_id: str = Form(""), account_org_id: str = Form(""), pay_date: str = Form(""),
               comment: str = Form(""), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Оплату записывают сотрудники площадки", status_code=403)
    site = _site(user, db)
    sup = db.get(Supplier, supplier_id)
    if site is None or sup is None:
        return HTMLResponse("Поставщик не найден", status_code=404)
    payer = int(payer_id) if payer_id.isdigit() else default_pocket(db, site.id, user)
    acc = int(account_org_id) if account_org_id.isdigit() else None
    try:
        d = date.fromisoformat(pay_date) if pay_date else date.today()
    except ValueError:
        d = date.today()

    def render(error):
        return templates.TemplateResponse("new/pay.html", _pay_ctx(
            request, user, site, db, sup, amount=amount, source=source, payer_id=payer,
            account_org_id=acc, pay_date=d, comment=comment, error=error))

    try:
        amt = Decimal(amount.replace(" ", "").replace(",", "."))
    except (InvalidOperation, AttributeError):
        return render("Укажите сумму")
    if source not in ("cash", "account"):
        return render("Откуда деньги: из кассы или со счёта")
    if source == "account" and acc not in {o.id for o in site_orgs(db, site.id)}:
        return render("Со счёта садика или школы? Выберите")
    if d > date.today():
        return render("Дата не позже сегодняшней")
    try:
        svc.pay_supplier(db, user=user, site_org_id=site.id, supplier_id=sup.id, amount=amt, d=d,
                         source=source, payer_id=payer, account_org_id=acc, comment=comment.strip() or None)
    except ValueError as e:
        return render(str(e))
    db.commit()
    return RedirectResponse(f"/new/pay?supplier={sup.id}&saved=1", status_code=303)
