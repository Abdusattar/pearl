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
        "figures": _figures_for(user, svc.now_figures(db, site.id, viewer=user)),
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
    from app.services import drafts
    rows = []
    for r in receipts:
        p = r.payload or {}
        if (r.kind or "receipt") == drafts.KITCHEN:
            url = f"/new/kitchen?draft={r.id}"
        elif r.kind == drafts.SERVICE:
            url = f"/new/nocheck?draft={r.id}"
        elif r.kind == drafts.COUNT:
            url = f"/new/stock/count?draft={r.id}"
        elif r.kind == drafts.TRANSFER:
            url = f"/new/stock/transfer?draft={r.id}"
        else:
            url = f"/new/buy?receipt={r.id}" + (f"&supplier={p['supplier_id']}" if p.get("supplier_id") else "")
        rows.append({"r": r, "by": names.get(r.created_by), "date": r.created_at.date() if r.created_at else None,
                     "what": drafts.title(r), "url": url, "kitchen": (r.kind or "receipt") == drafts.KITCHEN,
                     "text": ((r.payload or {}).get("text") or "")[:160] if drafts.is_text(r) else None,
                     "source": r.source})
    ctx = _base_ctx(request, user, site, db, "today")
    ctx.update({"rows": rows, "can_write": user.role in WRITE_ROLES,
                "skipped": request.query_params.get("skipped"), "done": request.query_params.get("done"),
                "done_day": request.query_params.get("day"), "done_id": request.query_params.get("id")})
    return templates.TemplateResponse("new/receipts.html", ctx)


def _figures_for(user, f: dict) -> dict:
    """Сотруднику — без остатков на счетах (23.09): это деньги учредителей."""
    from app.routers.new_cash import sees_accounts
    return f if sees_accounts(user) else {**f, "accounts": []}
