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
from app.services import once, repeats
from app.services.purchases import OPERATIONAL_ROLES, default_pocket, site_orgs

router = APIRouter(prefix="/new", tags=["new"])

FORMS = {
    "withdraw": "Снятие со счёта",
    "transfer": "Передать деньги",
    "recount": "Пересчёт кармана",
    "founder": "Учредители",
    "bank": "Остаток в банке",
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
    only_checks = request.query_params.get("f") == "checks"
    items = svc.history(db, site.id, only_checks=only_checks)
    for it in items:
        it["can_remove"] = user.role in WRITE_ROLES and svc.can_remove(user, it)
    ctx.update({"st": svc.state(db, site.id), "items": items, "only_checks": only_checks,
                "saved": saved, "error": request.query_params.get("error"), "forms": FORMS,
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
        # Все счета площадки, не только «живые»: первый остаток по счёту школы (23.09)
        # и есть то, что делает его живым — из state() он бы не попал в форму
        "bank_orgs": [(a["org"], a["expected"]) for a in svc.accounts(db, site.id)] if kind == "bank" else [],
    })
    return ctx


@router.post("/cash/remove", response_class=HTMLResponse)
async def cash_remove(request: Request, db: Session = Depends(get_db)):
    """Убрать ошибочную запись с причиной. Свои снятия и передачи — сам, остальное
    — владелец (21.09). Строка остаётся в истории зачёркнутой."""
    from urllib.parse import quote
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Записывают сотрудники площадки", status_code=403)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    form = await request.form()
    item_id = form.get("id") or ""
    back = "/new/cash" + ("?f=checks" if form.get("f") == "checks" else "")
    sep = "&" if "?" in back else "?"
    if not item_id.isdigit():
        return RedirectResponse(back, status_code=303)
    try:
        svc.remove(db, user=user, site_org_id=site.id, kind=form.get("kind") or "", item_id=int(item_id),
                   reason=form.get("reason") or "")
    except ValueError as e:
        return RedirectResponse(f"{back}{sep}error={quote(str(e))}", status_code=303)
    db.commit()
    return RedirectResponse(f"{back}{sep}saved=removed", status_code=303)


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
    q = request.query_params
    pre = {k: int(q[p]) for k, p in (("pocket_user_id", "pocket"), ("to_user_id", "to"), ("account_org_id", "org"))
           if (q.get(p) or "").isdigit()}
    return templates.TemplateResponse("new/cash_form.html", _form_ctx(request, user, site, db, kind, **pre))


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

    repeat_ok = g("repeat_ok") == "1"

    def render(error, repeat=None):
        ctx = _form_ctx(request, user, site, db, kind, error=error, **kw)
        ctx.update({"repeat": repeat, "repeat_back": "/new/cash"})
        return templates.TemplateResponse("new/cash_form.html", ctx)

    if d is None:
        return render("Дата не позже сегодняшней")
    if kind not in ("recount", "bank") and amount is None:
        return render("Укажите сумму")
    orgs = {o.id for o in site_orgs(db, site.id)}
    people = {p.id for p in svc.pocket_people(db, site.id)} | ({user.id} if user.role in OPERATIONAL_ROLES else set())
    token = once.clean(g("form_token"))
    if done := once.done_url(db, token):
        return RedirectResponse(done, status_code=303)
    try:
        if kind == "withdraw":
            if ints["account_org_id"] not in orgs:
                return render("Со счёта садика или школы? Выберите")
            if not repeat_ok and (rep := repeats.withdrawal(db, site.id, amount, d)):
                return render(None, rep)
            svc.withdraw(db, user=user, site_org_id=site.id, account_org_id=ints["account_org_id"], amount=amount, d=d,
                         comment=g("comment") or None, pocket_user_id=ints["pocket_user_id"] or default_pocket(db, site.id, user))
            msg = "withdraw"
        elif kind == "transfer":
            if ints["from_user_id"] not in people or ints["to_user_id"] not in people:
                return render("Выберите, кто кому передал")
            if not repeat_ok and (rep := repeats.transfer(db, site.id, ints["from_user_id"], ints["to_user_id"], amount, d)):
                return render(None, rep)
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
        elif kind == "bank":
            actual = _amount(g("amount")) if g("amount") not in ("", "0") else Decimal("0")
            if actual is None:
                return render("Сколько в банке сейчас?")
            if ints["account_org_id"] not in orgs:
                return render("Какой счёт? Выберите")
            svc.bank_balance(db, user=user, org_id=ints["account_org_id"], actual=actual, d=d, reason=g("reason"))
            msg = "bank"
        else:
            if not ints["founder_id"]:
                return render("Кто из учредителей?")
            if ints["pocket_user_id"] not in people:
                return render("В чей карман / из чьего кармана?")
            find = repeats.founder_withdraw if kw["direction"] == "withdraw" else repeats.founder_fund
            if not repeat_ok and (rep := find(db, site.id, ints["founder_id"], amount, d)):
                return render(None, rep)
            if kw["direction"] == "withdraw":
                svc.founder_withdraw(db, user=user, site_org_id=site.id, founder_id=ints["founder_id"],
                                     pocket_user_id=ints["pocket_user_id"], amount=amount, d=d, comment=g("comment") or None)
            else:
                svc.founder_fund(db, user=user, site_org_id=site.id, founder_id=ints["founder_id"],
                                 pocket_user_id=ints["pocket_user_id"], amount=amount, d=d, comment=g("comment") or None)
            msg = "founder"
    except ValueError as e:
        return render(str(e))
    url = f"/new/cash?saved={msg}"
    once.remember(db, token, user.id, url)
    db.commit()
    return RedirectResponse(url, status_code=303)

