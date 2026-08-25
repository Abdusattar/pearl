from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_accessible_orgs, get_current_user, resolve_org
from app.models import AccountBalanceSnapshot, CashFunding, Organization, User
from app.services import podotchet

router = APIRouter(prefix="/podotchet", tags=["podotchet"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Доступ: владельцы + те, кто реально держит деньги на руках (Мунара сейчас,
# Махабат с сентября) — шире, чем /dashboard (owner/founder/staff), потому что
# Мунара по факту role=manager, не staff (25.08).
ALLOWED_ROLES = ("owner", "founder", "staff", "manager")


def _guard(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=302)
    if user.role not in ALLOWED_ROLES:
        return None, RedirectResponse("/", status_code=302)
    return user, None


def _business_orgs(db: Session) -> list[Organization]:
    """Все реальные бизнесы (Школа/Сокулук/Кожомкул), не только доступные
    текущему пользователю — на пополнении нужно указать чей это подотчёт даже
    если сам пользователь ограничен пилотом одного объекта (25.08)."""
    all_orgs = db.query(Organization).all()
    has_children = {o.parent_id for o in all_orgs if o.parent_id is not None}
    return [o for o in all_orgs if o.id not in has_children]


def _parse_amount(raw: str) -> float:
    return float((raw or "0").replace(" ", "").replace(",", "."))


def _parse_date(raw: str):
    return datetime.strptime(raw, "%Y-%m-%d").date()


@router.get("/", response_class=HTMLResponse)
def podotchet_page(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)
    if not current_org:
        return RedirectResponse("/", status_code=302)

    today = date.today()
    expected = podotchet.get_expected_balance(db, current_org.id, today)
    ledger = podotchet.get_podotchet_ledger(db, current_org.id)
    ledger.sort(key=lambda b: (b["date"], b["id"]), reverse=True)

    users_by_id = {u.id: u.name for u in db.query(User).all()}
    orgs_by_id = {o.id: o.name for o in db.query(Organization).all()}

    balances = podotchet.get_balances_by_person(db, current_org.id)
    person_cards = [
        {"name": users_by_id.get(uid, "?"), "amount": amt}
        for uid, amt in sorted(balances.items(), key=lambda kv: -kv[1])
    ]

    since = expected["since"] or date(2026, 1, 1)
    category_spend = podotchet.get_spend_by_category(db, current_org.id, since, today)
    flows = podotchet.get_cross_org_flows(db, since, today)
    flow_rows = [
        {"from": orgs_by_id.get(f["from_org_id"], "?"), "to": orgs_by_id.get(f["to_org_id"], "?"), "amount": f["amount"]}
        for f in flows
    ]

    snapshots = (
        db.query(AccountBalanceSnapshot)
        .filter(AccountBalanceSnapshot.organization_id == current_org.id)
        .order_by(AccountBalanceSnapshot.date.desc(), AccountBalanceSnapshot.id.desc())
        .all()
    )
    days_since_snapshot = (today - expected["since"]).days if expected["since"] else None

    ledger_rows = []
    for b in ledger:
        status_reported = b["amount"] - b["remaining"]
        ledger_rows.append({
            **b,
            "taken_by_name": users_by_id.get(b["taken_by"], "?"),
            "accountable_name": users_by_id.get(b["accountable_user_id"], "?"),
            "source_org_name": orgs_by_id.get(b["source_organization_id"]) if b["source_organization_id"] else None,
            "reported": status_reported,
            "fully_reported": b["remaining"] <= podotchet.DUST,
        })

    return templates.TemplateResponse("podotchet/index.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id,
        "active_page": "podotchet",
        "business_orgs": _business_orgs(db),
        # founder (Айдай/Талас) — собственники, не участвуют в операционке,
        # деньги на руках не держат — не показываем в "снял/отчитывается" (25.08)
        "users": db.query(User).filter(User.deleted_at.is_(None), User.role != "founder").order_by(User.name).all(),
        "expected": expected,
        "person_cards": person_cards,
        "category_spend": category_spend,
        "flow_rows": flow_rows,
        "snapshots": snapshots,
        "ledger_rows": ledger_rows,
        "today": today.isoformat(),
        "days_since_snapshot": days_since_snapshot,
        "users_lookup": users_by_id,
    })


@router.post("/fund")
def create_funding(
    request: Request,
    source_type: str = Form(...),
    amount: str = Form(...),
    date_str: str = Form(..., alias="date"),
    organization_id: str = Form(...),
    taken_by: str = Form(...),
    accountable_user_id: str = Form(...),
    source_organization_id: str = Form(default=""),
    comment: str = Form(default=""),
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect
    redirect_url = f"/podotchet/?org_id={org_id}" if org_id else "/podotchet/"

    try:
        amount_val = _parse_amount(amount)
        date_val = _parse_date(date_str)
    except ValueError:
        return RedirectResponse(f"{redirect_url}?error=Неверная сумма или дата", status_code=303)
    if amount_val <= 0:
        return RedirectResponse(f"{redirect_url}?error=Сумма должна быть больше нуля", status_code=303)
    if source_type not in ("withdrawal", "direct_cash"):
        return RedirectResponse(redirect_url, status_code=303)

    funding = CashFunding(
        organization_id=int(organization_id),
        source_type=source_type,
        amount=amount_val,
        date=date_val,
        taken_by=int(taken_by),
        accountable_user_id=int(accountable_user_id),
        source_organization_id=int(source_organization_id) if source_organization_id else None,
        comment=comment.strip() or None,
        created_by=user.id,
    )
    db.add(funding)
    db.commit()
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/reconcile")
def add_snapshot(
    request: Request,
    balance: str = Form(...),
    date_str: str = Form(..., alias="date"),
    organization_id: str = Form(...),
    comment: str = Form(default=""),
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect
    redirect_url = f"/podotchet/?org_id={org_id}" if org_id else "/podotchet/"

    try:
        balance_val = _parse_amount(balance)
        date_val = _parse_date(date_str)
    except ValueError:
        return RedirectResponse(f"{redirect_url}?error=Неверная сумма или дата", status_code=303)

    db.add(AccountBalanceSnapshot(
        organization_id=int(organization_id), date=date_val,
        balance=balance_val, comment=comment.strip() or None, created_by=user.id,
    ))
    db.commit()
    return RedirectResponse(redirect_url, status_code=303)
