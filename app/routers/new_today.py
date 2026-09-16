"""Новый вход `/new/today`: экран «Сегодня» (макет блок 1)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.routers.new_buy import WRITE_ROLES, _base_ctx, _site, templates
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
        "can_write": user.role in WRITE_ROLES,
    })
    return templates.TemplateResponse("new/today.html", ctx)
