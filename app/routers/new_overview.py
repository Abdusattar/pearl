"""Новый вход `/new/overview`: Обзор для собственников (макет блок 6)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.routers.new_buy import _base_ctx, _site, templates
from app.services import overview as svc
from app.services.children import MONTHS_NOM

router = APIRouter(prefix="/new", tags=["new"])


@router.get("/overview", response_class=HTMLResponse)
def overview_page(request: Request, month: str | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    orgs = svc.visible_orgs(db, site.id, user)
    sig = svc.signals(db, site.id, orgs)
    table = svc.month_table(db, site.id, orgs, month)
    ctx = _base_ctx(request, user, site, db, "overview")
    ctx.update({"orgs": orgs, "signals": sig, "status": svc.status_line(sig), "figures": svc.figures(db, site.id, orgs),
                "table": table, "month_name": MONTHS_NOM[table["first"].month - 1], "today": date.today()})
    return templates.TemplateResponse("new/overview.html", ctx)
