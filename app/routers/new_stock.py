"""Новый вход `/new/stock`: склад, карточка продукта, пересчёт (блок 4, макет 21.09)."""
from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Product
from app.routers.new_buy import WRITE_ROLES, _base_ctx, _site, templates
from app.services import once
from app.services import stock as svc

router = APIRouter(prefix="/new", tags=["new"])

MEDIA_DIR = Path(__file__).parent.parent.parent / "media" / "stock_counts"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _user_site(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, None
    return user, _site(user, db)


@router.get("/stock", response_class=HTMLResponse)
def stock_page(request: Request, saved: str | None = None, db: Session = Depends(get_db)):
    user, site = _user_site(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    ctx = _base_ctx(request, user, site, db, "warehouse")
    ctx.update({"st": svc.state(db, site.id), "saved": saved, "can_write": user.role in WRITE_ROLES})
    return templates.TemplateResponse("new/stock.html", ctx)


@router.get("/stock/count", response_class=HTMLResponse)
def count_page(request: Request, cat: int | None = None, db: Session = Depends(get_db)):
    user, site = _user_site(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    ctx = _base_ctx(request, user, site, db, "warehouse")
    ctx.update({"data": svc.count_rows(db, site.id, cat), "error": None, "values": {},
                "missing": svc.state(db, site.id)["missing"], "can_write": user.role in WRITE_ROLES})
    return templates.TemplateResponse("new/stock_count.html", ctx)


@router.post("/stock/count", response_class=HTMLResponse)
async def count_save(request: Request, db: Session = Depends(get_db)):
    user, site = _user_site(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES or site is None:
        return HTMLResponse("Пересчитывают сотрудники площадки", status_code=403)
    form = await request.form()
    cat = form.get("cat")
    cat = int(cat) if cat and str(cat).isdigit() else None
    pids, vals = form.getlist("product_id"), form.getlist("actual")
    values = dict(zip(pids, vals))
    items, error = [], None
    for pid, v in zip(pids, vals):
        v = (v or "").strip().replace(" ", "").replace(",", ".")
        if not v:
            continue
        try:
            n = Decimal(v)
        except InvalidOperation:
            error = f"Не число: «{v}». Впишите, сколько на полке, например 12,5"
            break
        if n < 0:
            error = "На полке не бывает меньше нуля"
            break
        items.append((int(pid), n))
    token = once.clean(str(form.get("form_token") or ""))
    if not error:
        if done := once.done_url(db, token):
            return RedirectResponse(done, status_code=303)
        photo_path = None
        photo = form.get("photo")
        if photo is not None and getattr(photo, "filename", "") and items:
            ext = Path(photo.filename).suffix.lower() or ".jpg"
            name = f"{uuid.uuid4().hex}{ext}"
            (MEDIA_DIR / name).write_bytes(await photo.read())
            photo_path = f"stock_counts/{name}"
        try:
            svc.quick_count(db, user=user, site_id=site.id, items=items, photo_path=photo_path)
        except ValueError as e:
            error = str(e)
    if error:
        ctx = _base_ctx(request, user, site, db, "warehouse")
        ctx.update({"data": svc.count_rows(db, site.id, cat), "error": error, "values": values,
                    "missing": svc.state(db, site.id)["missing"], "can_write": True})
        return templates.TemplateResponse("new/stock_count.html", ctx)
    url = "/new/stock?saved=count"
    once.remember(db, token, user.id, url)
    db.commit()
    return RedirectResponse(url, status_code=303)


@router.get("/stock/{product_id}", response_class=HTMLResponse)
def product_page(product_id: int, request: Request, saved: int = 0, err: str | None = None,
                 db: Session = Depends(get_db)):
    user, site = _user_site(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    p = db.get(Product, product_id)
    if site is None or p is None:
        return HTMLResponse("Продукт не найден", status_code=404)
    while p.merged_into_id is not None:
        p = p.merged_into
    ctx = _base_ctx(request, user, site, db, "warehouse")
    ctx.update({"p": p, "card": svc.product_card(db, site.id, p), "saved": saved, "error": err,
                "can_write": user.role in WRITE_ROLES})
    return templates.TemplateResponse("new/stock_product.html", ctx)


@router.post("/stock/{product_id}/edit")
async def product_edit(product_id: int, request: Request, db: Session = Depends(get_db)):
    user, site = _user_site(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Нет прав", status_code=403)
    p = db.get(Product, product_id)
    if site is None or p is None:
        return HTMLResponse("Продукт не найден", status_code=404)
    form = await request.form()
    cat = str(form.get("category_id") or "")
    try:
        svc.edit_product(db, user=user, p=p, category_id=int(cat) if cat.isdigit() else None,
                         unit=str(form.get("unit") or ""), pack_name=str(form.get("pack_name") or ""),
                         pack_qty=str(form.get("pack_qty") or ""), aliases=str(form.get("aliases") or ""))
    except ValueError as e:
        return RedirectResponse(f"/new/stock/{p.id}?err={e}", status_code=303)
    db.commit()
    return RedirectResponse(f"/new/stock/{p.id}?saved=1", status_code=303)
