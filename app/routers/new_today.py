"""Новый вход `/new/today`: экран «Сегодня» (макет блок 1)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.routers.new_buy import WRITE_ROLES, _base_ctx, _site, templates
from app.models import User
from app.services import today as svc
from app.services.purchases import site_orgs

router = APIRouter(prefix="/new", tags=["new"])


@router.get("/today", response_class=HTMLResponse)
def today_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    ctx = _base_ctx(request, user, site, db, "today")
    orgs = site_orgs(db, site.id)
    ctx.update({
        "today": date.today(),
        "site_line": site.name if len(orgs) < 2 else "Сокулук, школа и садик",
        "todo": svc.todo(db, site.id),
        "figures": svc.now_figures(db, site.id),
        "buy_sub": svc.buy_subtitle(db, site.id),
        "kitchen": svc.kitchen_action(db, site.id),
        "can_write": user.role in WRITE_ROLES,
    })
    return templates.TemplateResponse("new/today.html", ctx)


@router.get("/receipts", response_class=HTMLResponse)
def receipts_page(request: Request, db: Session = Depends(get_db)):
    """Чеки с фото, которые ещё не внесены (макет «Сегодня» 21.09). Сюда же
    придут записи из чата: бот разберёт, Махабат подтвердит."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    receipts = svc.unchecked_receipts(db, site.id)
    names = {u.id: u.name for u in db.query(User).filter(User.id.in_({r.created_by for r in receipts if r.created_by})).all()}         if receipts else {}
    rows = [{"r": r, "by": names.get(r.created_by), "date": r.created_at.date() if r.created_at else None,
             "amount": r.amount_detected} for r in receipts]
    ctx = _base_ctx(request, user, site, db, "today")
    ctx.update({"rows": rows, "can_write": user.role in WRITE_ROLES,
                "skipped": request.query_params.get("skipped")})
    return templates.TemplateResponse("new/receipts.html", ctx)
