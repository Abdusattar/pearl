"""Новый вход: лента расходов `/new/expenses` (2а) и оплата поставщику `/new/pay` (2в/2д)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Purchase, Supplier, User
from app.routers.new_buy import WRITE_ROLES, _base_ctx, _site, templates
from app.services import ledger as svc
from app.services import no_receipt, once, repeats
from app.services.purchases import default_pocket, founders, pocket_users, remove_purchase, site_orgs, keep_entry_time
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
               comment: str = Form(""), repeat_ok: str = Form(""), form_token: str = Form(""),
               db: Session = Depends(get_db)):
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

    def render(error, repeat=None):
        ctx = _pay_ctx(request, user, site, db, sup, amount=amount, source=source, payer_id=payer,
                       account_org_id=acc, pay_date=d, comment=comment, error=error)
        ctx.update({"repeat": repeat, "repeat_back": f"/new/pay?supplier={sup.id}"})
        return templates.TemplateResponse("new/pay.html", ctx)

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
    token = once.clean(form_token)
    if done := once.done_url(db, token):
        return RedirectResponse(done, status_code=303)
    if repeat_ok != "1" and (rep := repeats.supplier_payment(db, sup.id, amt, d)):
        return render(None, rep)
    try:
        svc.pay_supplier(db, user=user, site_org_id=site.id, supplier_id=sup.id, amount=amt, d=d,
                         source=source, payer_id=payer, account_org_id=acc, comment=comment.strip() or None)
    except ValueError as e:
        return render(str(e))
    url = f"/new/pay?supplier={sup.id}&saved=1"
    once.remember(db, token, user.id, url)
    db.commit()
    return RedirectResponse(url, status_code=303)


# ── расход без чека (2г) ─────────────────────────────────────────────────

def _nocheck_ctx(request, user, site, db, **kw) -> dict:
    ctx = _base_ctx(request, user, site, db, "expenses")
    ctx.update({
        "kinds": no_receipt.KINDS, "recent": no_receipt.recent_suppliers(db, site.id),
        "all_suppliers": db.query(Supplier).order_by(Supplier.name).all(),
        "pockets": pocket_users(db, site.id), "founders": founders(db), "today": date.today(),
        "can_write": user.role in WRITE_ROLES,
        "amount": kw.get("amount", ""), "kind": kw.get("kind", ""), "what": kw.get("what", ""),
        "supplier_name": kw.get("supplier_name", ""), "for_org": kw.get("for_org", "shared"),
        "payment": kw.get("payment", "cash"), "payer_id": kw.get("payer_id", default_pocket(db, site.id, user)),
        "account_org_id": kw.get("account_org_id"), "founder_id": kw.get("founder_id"),
        "d": kw.get("d", date.today()), "error": kw.get("error"), "repeat": kw.get("repeat"),
        "repeat_back": "/new/expenses", "replaces": kw.get("replaces"), "legacy": kw.get("legacy"),
        "draft": kw.get("draft"),
    })
    return ctx


def _nocheck_draft(db: Session, r) -> dict:
    from app.models import User
    who = db.get(User, r.created_by) if r.created_by else None
    return {"id": r.id, "path": r.file_path, "by": who.name if who else None, "at": r.created_at, "source": r.source}


def _legacy_key(v: str | None) -> tuple[str, int] | None:
    v = (v or "").strip()
    if len(v) > 2 and v[0] in "rt" and v[1] == ":" and v[2:].isdigit():
        return v[0], int(v[2:])
    return None


def _legacy_kind(db: Session, tx) -> str:
    from app.models import ExpenseCategory
    cat = db.get(ExpenseCategory, tx.category_id) if tx.category_id else None
    return next((k for k, (_, name) in no_receipt.KINDS.items() if cat and name == cat.name), "other")


def _live_nocheck(db: Session, site, purchase_id: int | None) -> Purchase | None:
    p = db.get(Purchase, purchase_id) if purchase_id else None
    if p is None or p.site_org_id != site.id or p.deleted_at is not None or p.receipt_id is not None:
        return None
    return p


@router.get("/nocheck", response_class=HTMLResponse)
def nocheck_form(request: Request, edit: int | None = None, legacy: str | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    if (lk := _legacy_key(legacy)) is not None:
        from app.services import legacy as lg
        rows = lg.txs(db, site.id, *lk)
        if not rows:
            return HTMLResponse("Запись не найдена или уже поправлена", status_code=404)
        c = lg.card(db, lk[0], lk[1], rows)
        kw = dict(amount=f"{float(c['total']):g}".replace(".", ","), kind=_legacy_kind(db, rows[0]), what=c["note"] or "",
                  supplier_name=c["supplier"].name if c["supplier"] else "", for_org="shared",
                  payment=c["payment"] if c["payment"] in no_receipt.PAYMENTS else "cash",
                  payer_id=c["payer"].id if c["payer"] else default_pocket(db, site.id, user),
                  account_org_id=c["account_org_id"], d=c["date"],
                  legacy={"key": f"{lk[0]}:{lk[1]}", "title": c["supplier"].name if c["supplier"] else (c["note"] or "расход"),
                          "date": c["date"], "total": c["total"]})
        return templates.TemplateResponse("new/nocheck.html", _nocheck_ctx(request, user, site, db, **kw))
    draft_id = request.query_params.get("draft")
    if draft_id and draft_id.isdigit():
        from app.services import drafts
        r = drafts.open_draft(db, {o.id for o in site_orgs(db, site.id)} | {site.id}, int(draft_id), drafts.SERVICE)
        if r is None:
            return RedirectResponse("/new/receipts", status_code=302)   # уже внесён или отложен
        p = r.payload or {}
        d = None
        if isinstance(p.get("date"), str) and len(p["date"]) == 10:
            try:
                d = date.fromisoformat(p["date"])
            except ValueError:
                d = None
        amount = p.get("amount")
        # Из чьих наличных — того, кто прислал (такси Мунары 24.09 ушло из кармана Махабат)
        author = db.get(User, r.created_by) if r.created_by else None
        payer_id = None
        if author is not None and author.id != user.id and author.id in {u.id for u in pocket_users(db, site.id)}:
            payer_id = author.id
        kw = dict(amount=(f"{float(amount):g}".replace(".", ",") if amount else ""),
                  supplier_name=p.get("supplier_name") or p.get("supplier") or "", d=d or date.today(),
                  draft=_nocheck_draft(db, r), what=p.get("what") or "",
                  **({"payer_id": payer_id} if payer_id else {}))
        return templates.TemplateResponse("new/nocheck.html", _nocheck_ctx(request, user, site, db, **kw))
    if edit is None:
        return templates.TemplateResponse("new/nocheck.html", _nocheck_ctx(request, user, site, db))
    old = _live_nocheck(db, site, edit)
    if old is None:
        return HTMLResponse("Расход не найден или уже поправлен", status_code=404)
    kw = dict(amount=f"{float(old.total):g}".replace(".", ","), kind=no_receipt.kind_of(db, old), what=old.note or "",
              supplier_name=old.supplier.name, for_org=str(old.for_org_id) if old.for_org_id else "shared",
              payment=old.payment, payer_id=old.paid_from_user_id or default_pocket(db, site.id, user),
              account_org_id=old.account_org_id, founder_id=old.founder_id, d=old.date, replaces=old)
    return templates.TemplateResponse("new/nocheck.html", _nocheck_ctx(request, user, site, db, **kw))


@router.post("/nocheck", response_class=HTMLResponse)
async def nocheck_submit(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Расходы записывают сотрудники площадки", status_code=403)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    form = await request.form()
    g = lambda k: (form.get(k) or "").strip()
    num = lambda k: int(g(k)) if g(k).isdigit() else None
    try:
        d = date.fromisoformat(g("date")) if g("date") else date.today()
    except ValueError:
        d = None
    kw = dict(amount=g("amount"), kind=g("kind"), what=g("what"), supplier_name=g("supplier_name"),
              for_org=g("for_org") or "shared", payment=g("payment") or "cash",
              payer_id=num("payer_id") or default_pocket(db, site.id, user), account_org_id=num("account_org_id"),
              founder_id=num("founder_id"), d=d or date.today(), replaces=_live_nocheck(db, site, num("replaces_id")))
    from app.services import drafts
    draft = (drafts.open_draft(db, {o.id for o in site_orgs(db, site.id)} | {site.id}, num("draft_id"), drafts.SERVICE)
             if num("draft_id") else None)
    if draft is not None:
        kw["draft"] = _nocheck_draft(db, draft)
    if g("replaces_id") and kw["replaces"] is None:
        return HTMLResponse("Этот расход уже поправили или убрали — откройте его заново", status_code=409)
    lk = _legacy_key(g("replaces_legacy"))
    legacy_rows = []
    if lk is not None:
        from app.services import legacy as lg
        legacy_rows = lg.txs(db, site.id, *lk)
        if not legacy_rows:
            return HTMLResponse("Эту запись уже поправили или убрали — откройте её заново", status_code=409)
        kw["legacy"] = {"key": f"{lk[0]}:{lk[1]}", "title": g("supplier_name"), "date": legacy_rows[0].date,
                        "total": sum(t.amount for t in legacy_rows)}

    def render(error=None, repeat=None):
        return templates.TemplateResponse("new/nocheck.html", _nocheck_ctx(request, user, site, db, error=error,
                                                                           repeat=repeat, **kw))

    try:
        amount = Decimal(g("amount").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return render("Укажите сумму")
    if amount <= 0:
        return render("Укажите сумму")
    if kw["kind"] not in no_receipt.KINDS:
        return render("Что это: свет, доставка, ремонт…? Выберите")
    if not kw["supplier_name"]:
        return render("Кому заплатили? Например, Северэлектро или Такси")
    orgs = {o.id for o in site_orgs(db, site.id)}
    for_org_id = int(kw["for_org"]) if kw["for_org"].isdigit() else None
    if for_org_id is not None and for_org_id not in orgs:
        return render("Для кого: общее, школа или садик?")
    if for_org_id is None and kw["kind"] in no_receipt.FOR_ORG_REQUIRED:
        return render("Ремонт для кого: школа или садик? Стройка идёт на объект, не в общее")
    if kw["payment"] not in no_receipt.PAYMENTS:
        return render("Откуда деньги?")
    if kw["payment"] == "account" and kw["account_org_id"] not in orgs:
        return render("Со счёта садика или школы? Выберите")
    if kw["payment"] == "founder" and not kw["founder_id"]:
        return render("Кто из учредителей заплатил?")
    if d is None or d > date.today():
        return render("Дата не позже сегодняшней")

    token = once.clean(g("form_token"))
    if done := once.done_url(db, token):
        return RedirectResponse(done, status_code=303)
    repeat_ok = g("repeat_ok") == "1"
    if not repeat_ok and kw["replaces"] is None and not legacy_rows and (rep := no_receipt.find_repeat(db, site.id, kw["kind"], amount, d)):
        return render(None, rep)
    old_tx_ids = [t.id for t in kw["replaces"].transactions] if kw["replaces"] is not None else []
    if kw["replaces"] is not None:
        remove_purchase(db, kw["replaces"], user)
    if legacy_rows:
        old_tx_ids = [t.id for t in legacy_rows]
        lg.remove(db, legacy_rows, user, "поправлено в новом входе")
    supplier = db.query(Supplier).filter(Supplier.name == kw["supplier_name"]).first()
    if supplier is None:
        supplier = Supplier(name=kw["supplier_name"], phone="0000")
        db.add(supplier)
        db.flush()
    purchase = no_receipt.record(
        db, user=user, site_org_id=site.id, supplier=supplier, kind=kw["kind"], amount=amount, what=kw["what"] or None,
        payment=kw["payment"], payer_id=kw["payer_id"], account_org_id=kw["account_org_id"],
        founder_id=kw["founder_id"], for_org_id=for_org_id, d=d, repeat_confirmed=repeat_ok)
    if kw["replaces"] is not None:
        purchase.replaces_id = kw["replaces"].id
    if old_tx_ids:
        keep_entry_time(db, purchase, old_tx_ids)
    url = f"/new/buy/{purchase.id}?saved=1"
    if draft is not None:
        drafts.done(db, draft, user=user, result_type="purchase", result_id=purchase.id)
        from app.services.bot import owner_copy
        owner_copy(db, f"{user.name}: услуга из чата внесена — {supplier.name}, {amount:,.0f}.".replace(",", " "))
        url = f"/new/receipts?done=service&id={purchase.id}"
    once.remember(db, token, user.id, url)
    db.commit()
    return RedirectResponse(url, status_code=303)


# ── поставщики (блок «Расходы», 21.09) ───────────────────────────────────

def _suppliers_page(request, user, site, db, supplier_id: int | None, error: str | None = None, saved: bool = False):
    from app.services import suppliers_view as sv
    rows = sv.listing(db)
    sup = db.get(Supplier, supplier_id) if supplier_id else (rows[0]["s"] if rows else None)
    ctx = _base_ctx(request, user, site, db, "expenses")
    ctx.update({"rows": rows, "c": sv.card(db, sup) if sup else None, "can_write": user.role in WRITE_ROLES,
                "error": error, "saved": saved})
    return templates.TemplateResponse("new/suppliers.html", ctx)


@router.get("/suppliers", response_class=HTMLResponse)
def suppliers_list(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    return _suppliers_page(request, user, site, db, None)


@router.get("/suppliers/{supplier_id}", response_class=HTMLResponse)
def supplier_card(supplier_id: int, request: Request, saved: int = 0, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None or db.get(Supplier, supplier_id) is None:
        return HTMLResponse("Поставщик не найден", status_code=404)
    return _suppliers_page(request, user, site, db, supplier_id, saved=bool(saved))


@router.post("/suppliers/{supplier_id}/debt", response_class=HTMLResponse)
async def supplier_set_debt(supplier_id: int, request: Request, db: Session = Depends(get_db)):
    from app.services import suppliers_view as sv
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Нет прав", status_code=403)
    site = _site(user, db)
    sup = db.get(Supplier, supplier_id)
    if site is None or sup is None:
        return HTMLResponse("Поставщик не найден", status_code=404)
    form = await request.form()
    raw = (form.get("amount") or "").replace(" ", "").replace(",", ".")
    try:
        actual = Decimal(raw)
    except InvalidOperation:
        return _suppliers_page(request, user, site, db, supplier_id, error="Сколько должны на самом деле? Укажите сумму")
    if actual < 0:
        return _suppliers_page(request, user, site, db, supplier_id, error="Долг не может быть меньше нуля")
    try:
        sv.set_debt(db, user=user, site_org_id=site.id, supplier=sup, actual=actual, reason=form.get("reason") or "")
    except ValueError as e:
        return _suppliers_page(request, user, site, db, supplier_id, error=str(e))
    db.commit()
    return RedirectResponse(f"/new/suppliers/{supplier_id}?saved=1", status_code=303)
