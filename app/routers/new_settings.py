"""Новый вход `/new/settings`: правила бизнеса (макет 21.09). Только владелец."""
from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Organization, Service, User
from app.routers.new_buy import _base_ctx, _site, templates
from app.services import rules
from app.services import settings_view as svc
from app.services.purchases import site_orgs

router = APIRouter(prefix="/new", tags=["new"])
OWNER_ROLES = ("owner",)


def _owner(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, None, RedirectResponse("/login", status_code=302)
    if user.role not in OWNER_ROLES:
        return None, None, RedirectResponse("/new/today", status_code=302)
    site = _site(user, db)
    if site is None:
        return None, None, HTMLResponse("Объект не найден", status_code=404)
    return user, site, None


def _org(db: Session, site, org_id) -> Organization | None:
    oid = int(org_id) if str(org_id).isdigit() else None
    return next((o for o in site_orgs(db, site.id) if o.id == oid), None)


def _back(msg: str | None = None, err: str | None = None, anchor: str = "") -> RedirectResponse:
    q = f"?saved={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
    return RedirectResponse(f"/new/settings{q}{'#' + anchor if anchor else ''}", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str | None = None, err: str | None = None, db: Session = Depends(get_db)):
    user, site, stop = _owner(request, db)
    if stop:
        return stop
    ctx = _base_ctx(request, user, site, db, "settings")
    ctx.update({"s": svc.overview(db, site.id), "RULES": rules.RULES, "WEEKDAYS": rules.WEEKDAYS,
                "saved": saved, "error": err, "role_text": svc.ROLE_TEXT, "role_sees": svc.ROLE_SEES,
                "users": db.query(User).filter(User.deleted_at.is_(None), User.role.in_(("director", "manager", "staff", "owner"))).order_by(User.name).all()})
    return templates.TemplateResponse("new/settings.html", ctx)


@router.post("/settings/rule")
async def settings_rule(request: Request, db: Session = Depends(get_db)):
    user, site, stop = _owner(request, db)
    if stop:
        return stop
    form = await request.form()
    key = str(form.get("key") or "")
    try:
        if key == "kitchen_weekdays":
            days = sorted({int(d) for d in form.getlist("day") if str(d).isdigit() and 0 <= int(d) <= 6})
            if not days:
                raise ValueError("Кухня работает хотя бы один день")
            rules.put(db, user=user, key=key, value=days)
        elif key in rules.RULES:
            rules.set_number(db, user=user, key=key, raw=str(form.get("value") or ""))
        else:
            raise ValueError("Нет такого правила")
    except ValueError as e:
        return _back(err=str(e))
    db.commit()
    return _back("Записано. Система считает по новому правилу.")


@router.post("/settings/holder")
async def settings_holder(request: Request, db: Session = Depends(get_db)):
    user, site, stop = _owner(request, db)
    if stop:
        return stop
    form = await request.form()
    org = _org(db, site, form.get("org_id"))
    hid = str(form.get("user_id") or "")
    if org is None:
        return _back(err="Нет такого объекта")
    svc.set_holder(db, user=user, org=org, holder_id=int(hid) if hid.isdigit() else None)
    db.commit()
    return _back("Держатель наличных записан.")


@router.post("/settings/frozen")
async def settings_frozen(request: Request, db: Session = Depends(get_db)):
    user, site, stop = _owner(request, db)
    if stop:
        return stop
    form = await request.form()
    org = _org(db, site, form.get("org_id"))
    if org is None:
        return _back(err="Нет такого объекта")
    try:
        svc.set_frozen(db, user=user, org=org, raw=str(form.get("percent") or ""))
    except ValueError as e:
        return _back(err=str(e))
    db.commit()
    return _back("Заморозка записана: действует со следующего начисления.")


@router.post("/settings/minor")
async def settings_minor(request: Request, db: Session = Depends(get_db)):
    user, site, stop = _owner(request, db)
    if stop:
        return stop
    form = await request.form()
    svc.set_minor(db, user=user, minor_ids={int(x) for x in form.getlist("minor") if str(x).isdigit()})
    db.commit()
    return _back("Записано: склад считает остаток по новому списку.")


@router.post("/settings/service/{service_id}")
async def settings_service(service_id: int, request: Request, db: Session = Depends(get_db)):
    user, site, stop = _owner(request, db)
    if stop:
        return stop
    s = db.get(Service, service_id)
    if s is None or _org(db, site, s.organization_id) is None:
        return _back(err="Нет такой услуги")
    form = await request.form()
    try:
        svc.set_service_price(db, user=user, svc=s, price_raw=str(form.get("price") or ""))
    except ValueError as e:
        return _back(err=str(e))
    db.commit()
    return _back(f"Цена «{s.name}» записана.")


@router.get("/settings/tariff/{org_id}", response_class=HTMLResponse)
def tariff_page(org_id: int, request: Request, err: str | None = None, db: Session = Depends(get_db)):
    user, site, stop = _owner(request, db)
    if stop:
        return stop
    org = _org(db, site, org_id)
    if org is None:
        return HTMLResponse("Объект не найден", status_code=404)
    t = svc.tuition(db, org.id)
    pending = next((p for p in rules.pending_tariffs(db) if p["org_id"] == org.id), None)
    ctx = _base_ctx(request, user, site, db, "settings")
    ctx.update({"org": org, "svc": t, "history": svc.history(db, t), "months": svc.tariff_months(db, org.id),
                "month_label": svc.month_label, "charged": svc.charged_this_month(db, org.id), "error": err,
                "pending": pending, "pending_from": svc.month_label(date.fromisoformat(pending["from"])) if pending else None})
    return templates.TemplateResponse("new/settings_tariff.html", ctx)


@router.post("/settings/tariff/{org_id}")
async def tariff_save(org_id: int, request: Request, db: Session = Depends(get_db)):
    user, site, stop = _owner(request, db)
    if stop:
        return stop
    org = _org(db, site, org_id)
    if org is None:
        return HTMLResponse("Объект не найден", status_code=404)
    form = await request.form()
    if form.get("cancel_pending"):
        svc.cancel_pending(db, user=user, org_id=org.id)
        db.commit()
        return _back("Отложенный тариф отменён.")
    try:
        msg = svc.set_tariff(db, user=user, org=org, price_raw=str(form.get("price") or ""),
                             month_raw=str(form.get("month") or ""))
    except ValueError as e:
        return RedirectResponse(f"/new/settings/tariff/{org.id}?err={quote(str(e))}", status_code=303)
    db.commit()
    return _back(msg)
