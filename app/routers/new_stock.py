"""Новый вход `/new/stock`: склад, карточка продукта, пересчёт (блок 4, макет 21.09)."""
from __future__ import annotations

import uuid
from datetime import date
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
    ctx.update({"v": svc.counted_view(db, site.id), "saved": saved, "can_write": user.role in WRITE_ROLES})
    return templates.TemplateResponse("new/stock.html", ctx)


def _open_stock_draft(db, site, draft_id, kind):
    from app.services import drafts
    from app.services.purchases import site_orgs
    return drafts.open_draft(db, {o.id for o in site_orgs(db, site.id)} | {site.id}, draft_id, kind)


def _before_count(db, site, draft_id=None):
    """Что внести ДО пересчёта: закупки и передачи из чата — иначе лягут поверх
    посчитанного и остаток задвоится (23.09, точка ноль)."""
    from app.services import drafts, today as td
    return [r for r in td.unchecked_receipts(db, site.id)
            if r.id != draft_id and (r.kind or drafts.RECEIPT) in (drafts.RECEIPT, drafts.TRANSFER)]


@router.get("/stock/count", response_class=HTMLResponse)
def count_page(request: Request, cat: str | None = None, draft: int | None = None, db: Session = Depends(get_db)):
    cat = int(cat) if cat and cat.isdigit() else (svc.KEY if cat == svc.KEY else None)
    user, site = _user_site(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    ctx = _base_ctx(request, user, site, db, "warehouse")
    data, values, rc = svc.count_rows(db, site.id, cat), {}, None
    if draft:
        rc = _open_stock_draft(db, site, draft, "count")
        if rc is None:
            return RedirectResponse("/new/receipts", status_code=302)   # уже внесён или отложен
        from app.services import drafts
        try:
            got = svc.draft_count_rows(db, site.id, drafts.stock_rows(db, rc, site.id))
            db.commit()
        except Exception as e:  # noqa: BLE001 — модель недоступна: пересчёт руками
            got = {"rows": [], "values": {}, "skipped": [], "error": str(e)}
        data = {**data, "current": svc.DRAFT, "rows": got["rows"], "n_draft": len(got["rows"]),
                "skipped": got["skipped"], "draft_id": rc.id,
                "draft_text": (rc.payload or {}).get("text"), "draft_by": rc.created_by,
                "draft_date": rc.created_at.date() if rc.created_at else None}
        values = got["values"]
    ctx.update({"data": data, "error": None, "values": values, "before": _before_count(db, site, rc.id if rc else None),
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
    cat = str(form.get("cat") or "")
    cat = int(cat) if cat.isdigit() else (svc.KEY if cat == svc.KEY else None)
    draft_raw = str(form.get("draft_id") or "")
    rc = _open_stock_draft(db, site, int(draft_raw), "count") if draft_raw.isdigit() else None
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
            res = svc.quick_count(db, user=user, site_id=site.id, items=items, photo_path=photo_path,
                                  d=rc.created_at.date() if rc is not None and rc.created_at else None)
            if rc is not None:
                from app.services import drafts
                drafts.done(db, rc, user=user, result_type="stock_count", result_id=res["count_id"])
                drafts.learn_words(db, rc, {pid for pid, _ in items})
        except ValueError as e:
            error = str(e)
    if error:
        ctx = _base_ctx(request, user, site, db, "warehouse")
        data = svc.count_rows(db, site.id, cat)
        if rc is not None:
            from app.services import drafts
            got = svc.draft_count_rows(db, site.id, drafts.stock_rows(db, rc, site.id))
            data = {**data, "current": svc.DRAFT, "rows": got["rows"], "n_draft": len(got["rows"]),
                    "skipped": got["skipped"], "draft_id": rc.id, "draft_text": (rc.payload or {}).get("text")}
        ctx.update({"data": data, "error": error, "values": values, "before": _before_count(db, site, rc.id if rc else None),
                    "missing": svc.state(db, site.id)["missing"], "can_write": True})
        return templates.TemplateResponse("new/stock_count.html", ctx)
    url = "/new/stock?saved=count"
    once.remember(db, token, user.id, url)
    db.commit()
    return RedirectResponse(url, status_code=303)


def _transfer_ctx(request, user, site, db, **kw) -> dict:
    ctx = _base_ctx(request, user, site, db, "warehouse")
    targets = svc.transfer_targets(db, site.id)
    inc = {int(k) for k in (kw.get("values") or {}) if str(k).isdigit()}
    ctx.update({"rows": svc.transfer_rows(db, site.id, inc), "targets": targets, "can_write": user.role in WRITE_ROLES,
                "draft_id": kw.get("draft_id"), "draft_text": kw.get("draft_text"), "skipped": kw.get("skipped", []),
                "to_org_id": kw.get("to_org_id") or (targets[0].id if len(targets) == 1 else None),
                "values": kw.get("values", {}), "error": kw.get("error"), "today": date.today(), "d": kw.get("d", date.today())})
    return ctx


@router.get("/stock/transfer", response_class=HTMLResponse)
def transfer_page(request: Request, draft: int | None = None, db: Session = Depends(get_db)):
    user, site = _user_site(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    kw = {}
    if draft:
        rc = _open_stock_draft(db, site, draft, "transfer")
        if rc is None:
            return RedirectResponse("/new/receipts", status_code=302)
        from app.services import drafts
        try:
            rows = drafts.stock_rows(db, rc, site.id)
            db.commit()
        except Exception:  # noqa: BLE001
            rows = []
        got = svc.draft_count_rows(db, site.id, rows)
        kw = {"values": got["values"], "skipped": got["skipped"], "draft_id": rc.id,
              "draft_text": (rc.payload or {}).get("text"), "d": rc.created_at.date() if rc.created_at else date.today()}
    return templates.TemplateResponse("new/stock_transfer.html", _transfer_ctx(request, user, site, db, **kw))


@router.post("/stock/transfer", response_class=HTMLResponse)
async def transfer_save(request: Request, db: Session = Depends(get_db)):
    """Передали продукты другому садику (владелец 23.09: Сокулук → Кожомкул)."""
    user, site = _user_site(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES or site is None:
        return HTMLResponse("Передачу записывают сотрудники площадки", status_code=403)
    form = await request.form()
    pids, vals = form.getlist("product_id"), form.getlist("qty")
    values = dict(zip(pids, vals))
    to_raw = str(form.get("to_org_id") or "")
    to_org_id = int(to_raw) if to_raw.isdigit() else None
    try:
        d = date.fromisoformat(str(form.get("date") or "")) if form.get("date") else date.today()
    except ValueError:
        d = date.today()
    items, error = [], None
    for pid, v in zip(pids, vals):
        v = (v or "").strip().replace(" ", "").replace(",", ".")
        if not v:
            continue
        try:
            items.append((int(pid), Decimal(v)))
        except InvalidOperation:
            error = f"Не число: «{v}». Впишите, сколько передали, например 12"
            break
    token = once.clean(str(form.get("form_token") or ""))
    if not error:
        if done := once.done_url(db, token):
            return RedirectResponse(done, status_code=303)
        try:
            outs = svc.transfer_out(db, user=user, site_id=site.id, to_org_id=to_org_id or 0, items=items, d=min(d, date.today()))
            draft_raw = str(form.get("draft_id") or "")
            rc = _open_stock_draft(db, site, int(draft_raw), "transfer") if draft_raw.isdigit() else None
            if rc is not None:
                from app.services import drafts
                drafts.done(db, rc, user=user, result_type="stock_transfer", result_id=outs[0].id)
                drafts.learn_words(db, rc, {pid for pid, _ in items})
        except ValueError as e:
            error = str(e)
    if error:
        return templates.TemplateResponse("new/stock_transfer.html",
                                          _transfer_ctx(request, user, site, db, values=values, error=error,
                                                        to_org_id=to_org_id, d=d))
    url = "/new/stock?saved=transfer"
    once.remember(db, token, user.id, url)
    db.commit()
    return RedirectResponse(url, status_code=303)


def _meals_ctx(request, user, site, db, d, **kw) -> dict:
    from app.services import meals
    ctx = _base_ctx(request, user, site, db, "today")
    row = meals.get(db, site.id, d)
    prev = (db.query(meals.MealCount).filter(meals.MealCount.site_org_id == site.id, meals.MealCount.date < d)
            .order_by(meals.MealCount.date.desc()).first())
    from datetime import timedelta
    ctx.update({"d": d, "row": row, "prev": prev, "roster": meals.roster(db, site.id), "example": meals.EXAMPLE,
                "can_write": user.role in WRITE_ROLES, "today": date.today(), "timedelta": timedelta,
                "values": {}, "menu": None, **kw})
    return ctx


@router.get("/meals", response_class=HTMLResponse)
def meals_page(request: Request, d: str | None = None, saved: int = 0, db: Session = Depends(get_db)):
    """«Сегодня едят» (23.09): та же запись, что приходит из чата, — поправить или внести пропущенный день."""
    user, site = _user_site(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    try:
        day = date.fromisoformat(d) if d else date.today()
    except ValueError:
        day = date.today()
    return templates.TemplateResponse("new/meals.html", _meals_ctx(request, user, site, db, min(day, date.today()), saved=saved))


@router.post("/meals", response_class=HTMLResponse)
async def meals_save(request: Request, db: Session = Depends(get_db)):
    from app.services import meals
    user, site = _user_site(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES or site is None:
        return HTMLResponse("Нет прав", status_code=403)
    form = await request.form()
    try:
        day = min(date.fromisoformat(str(form.get("d") or "")), date.today())
    except ValueError:
        day = date.today()
    values, error = {}, None
    for k in meals.FIELDS:
        v = str(form.get(k) or "").strip().replace(" ", "")
        if not v:
            continue
        if not v.isdigit() or int(v) > 2000:
            error = f"{meals.LABEL[k].capitalize()}: нужно число людей, например 310"
            break
        values[k] = int(v)
    if not error and not values:
        error = "Впишите хотя бы одно число"
    if error:
        return templates.TemplateResponse("new/meals.html", _meals_ctx(request, user, site, db, day, error=error,
                                                                       values=values, menu=form.get("menu")))
    token = once.clean(str(form.get("form_token") or ""))
    if done := once.done_url(db, token):
        return RedirectResponse(done, status_code=303)
    meals.record(db, site_org_id=site.id, d=day, values=values, menu=str(form.get("menu") or "").strip() or None,
                 user=user, source="form")
    doubt = meals.doubts(db, site.id, values, day)
    url = f"/new/meals?d={day.isoformat()}&saved={2 if doubt else 1}"
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
