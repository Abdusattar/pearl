"""Новый вход `/new/cash`: касса с карманами (макет блок 4: 4а экран, 4б снятие
и пересчёт, 4г учредители). Зарплата (4в) и история — следующим шагом."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import User
from app.routers.new_buy import WRITE_ROLES, _base_ctx, _site, templates
from app.services import cash as svc
from app.services.purchases import OPERATIONAL_ROLES, default_pocket, site_orgs

router = APIRouter(prefix="/new", tags=["new"])

FORMS = {
    "withdraw": "Снятие со счёта",
    "transfer": "Передать деньги",
    "recount": "Пересчёт кармана",
    "founder": "Учредители",
}
CASH_ROLES = WRITE_ROLES + ("founder",)   # учредители видят кассу, взносы и изъятия пишут сотрудники


def _amount(s: str) -> Decimal | None:
    try:
        v = Decimal((s or "").replace(" ", "").replace(",", "."))
        return v if v > 0 else None
    except InvalidOperation:
        return None


def _date(s: str) -> date | None:
    try:
        d = date.fromisoformat(s) if s else date.today()
    except ValueError:
        return None
    return d if d <= date.today() else None


@router.get("/cash", response_class=HTMLResponse)
def cash_page(request: Request, saved: str | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    ctx = _base_ctx(request, user, site, db, "cash")
    ctx.update({"pockets": svc.pockets(db, site.id), "accounts": svc.accounts(db, site.id),
                "recent": svc.recent(db, site.id), "saved": saved, "forms": FORMS,
                "can_write": user.role in WRITE_ROLES})
    return templates.TemplateResponse("new/cash.html", ctx)


def _form_ctx(request, user, site, db, kind: str, **kw) -> dict:
    ctx = _base_ctx(request, user, site, db, "cash")
    people = svc.pocket_people(db, site.id)
    if user.id not in {p.id for p in people} and user.role in OPERATIONAL_ROLES:
        people.append(user)
    me = default_pocket(db, site.id, user)
    my = svc.pocket_balance(db, site.id, me)
    ctx.update({
        "kind": kind, "title": FORMS[kind], "people": people, "site_orgs": site_orgs(db, site.id),
        "founders": svc.founder_list(db), "today": date.today(), "my_balance": my,
        "pocket_balances": {p.id: svc.pocket_balance(db, site.id, p.id) for p in people},
        "amount": kw.get("amount", ""), "d": kw.get("d", date.today()), "comment": kw.get("comment", ""),
        "account_org_id": kw.get("account_org_id"), "from_user_id": kw.get("from_user_id", me),
        "to_user_id": kw.get("to_user_id"), "pocket_user_id": kw.get("pocket_user_id", me),
        "founder_id": kw.get("founder_id"), "direction": kw.get("direction", "fund"),
        "reason": kw.get("reason", ""), "error": kw.get("error"), "can_write": user.role in WRITE_ROLES,
    })
    return ctx


@router.get("/cash/{kind}", response_class=HTMLResponse)
def cash_form(kind: str, request: Request, db: Session = Depends(get_db)):
    if kind not in FORMS:
        return HTMLResponse("Не найдено", status_code=404)
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    return templates.TemplateResponse("new/cash_form.html", _form_ctx(request, user, site, db, kind))


@router.post("/cash/{kind}", response_class=HTMLResponse)
async def cash_submit(kind: str, request: Request, db: Session = Depends(get_db)):
    if kind not in FORMS:
        return HTMLResponse("Не найдено", status_code=404)
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Записывают сотрудники площадки", status_code=403)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    form = await request.form()
    g = lambda k: (form.get(k) or "").strip()
    amount = _amount(g("amount"))
    d = _date(g("date"))
    ints = {k: int(g(k)) if g(k).isdigit() else None for k in
            ("account_org_id", "from_user_id", "to_user_id", "pocket_user_id", "founder_id")}
    kw = dict(amount=g("amount"), d=d or date.today(), comment=g("comment"), reason=g("reason"),
              direction=g("direction") or "fund", **ints)

    def render(error):
        return templates.TemplateResponse("new/cash_form.html", _form_ctx(request, user, site, db, kind, error=error, **kw))

    if d is None:
        return render("Дата не позже сегодняшней")
    if kind != "recount" and amount is None:
        return render("Укажите сумму")
    orgs = {o.id for o in site_orgs(db, site.id)}
    people = {p.id for p in svc.pocket_people(db, site.id)} | ({user.id} if user.role in OPERATIONAL_ROLES else set())
    try:
        if kind == "withdraw":
            if ints["account_org_id"] not in orgs:
                return render("Со счёта садика или школы? Выберите")
            svc.withdraw(db, user=user, site_org_id=site.id, account_org_id=ints["account_org_id"], amount=amount, d=d,
                         comment=g("comment") or None, pocket_user_id=ints["pocket_user_id"] or default_pocket(db, site.id, user))
            msg = "withdraw"
        elif kind == "transfer":
            if ints["from_user_id"] not in people or ints["to_user_id"] not in people:
                return render("Выберите, кто кому передал")
            svc.transfer(db, user=user, site_org_id=site.id, from_user_id=ints["from_user_id"],
                         to_user_id=ints["to_user_id"], amount=amount, d=d, comment=g("comment") or None)
            msg = "transfer"
        elif kind == "recount":
            actual = _amount(g("amount")) if g("amount") not in ("", "0") else Decimal("0")
            if actual is None:
                return render("Сколько насчитали?")
            if ints["pocket_user_id"] not in people:
                return render("Чей карман?")
            svc.recount(db, user=user, site_org_id=site.id, pocket_user_id=ints["pocket_user_id"], actual=actual, d=d,
                        reason=g("reason"))
            msg = "recount"
        else:
            if not ints["founder_id"]:
                return render("Кто из учредителей?")
            if ints["pocket_user_id"] not in people:
                return render("В чей карман / из чьего кармана?")
            if kw["direction"] == "withdraw":
                svc.founder_withdraw(db, user=user, site_org_id=site.id, founder_id=ints["founder_id"],
                                     pocket_user_id=ints["pocket_user_id"], amount=amount, d=d, comment=g("comment") or None)
            else:
                svc.founder_fund(db, user=user, site_org_id=site.id, founder_id=ints["founder_id"],
                                 pocket_user_id=ints["pocket_user_id"], amount=amount, d=d, comment=g("comment") or None)
            msg = "founder"
    except ValueError as e:
        return render(str(e))
    db.commit()
    return RedirectResponse(f"/new/cash?saved={msg}", status_code=303)
