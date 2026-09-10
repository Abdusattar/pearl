"""Пересчёт склада — отдельный роутер (09.09).

Отдельно от warehouse.py по двум причинам: тот файл уже большой, и здесь другой
круг доступа. `staff` сюда пускаем намеренно — пересчёт физически делает
Махабат (role=staff), а старый экран актуализации её разворачивал, то есть
человек, который ведёт склад, к нему допущен не был.
"""
import uuid
from datetime import date as date_type, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_accessible_orgs, get_current_user, resolve_org
from app.models import (
    Organization, Product, StockCount, StockCountLine, StockCountPhoto, User,
)
from app.services import stock_count
from app.services.dedup_guard import acquire_submission_lock

router = APIRouter(prefix="/warehouse/count", tags=["stock_count"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Тот же корень, что у фотографий чеков: media уже отдаётся статикой через
# /media и переживает деплой (проверено на файле чека от 08.09).
PHOTO_DIR = Path(__file__).parent.parent.parent / "media" / "stock_counts"
PHOTO_DIR.mkdir(parents=True, exist_ok=True)
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf"}
PHOTO_MAX_BYTES = 15 * 1024 * 1024


def _descendants(org_id: int, all_orgs: list) -> set:
    ids = {org_id}
    for o in all_orgs:
        if o.parent_id == org_id:
            ids |= _descendants(o.id, all_orgs)
    return ids


def _ctx(request: Request, db: Session, org_id: str | None):
    user = get_current_user(request, db)
    if not user:
        return None, None
    current_org = resolve_org(
        int(org_id) if org_id and str(org_id).isdigit() else None, user, db)
    if not current_org:
        return None, None
    all_orgs = db.query(Organization).all()
    return {
        "request": request,
        "current_user": user,
        "accessible_orgs": get_accessible_orgs(user, db),
        "current_org_id": current_org.id,
        "current_org": current_org,
        "active_page": "warehouse",
    }, _descendants(current_org.id, all_orgs)


def _back(ctx, tab="food", only_left=""):
    url = f"/warehouse/count/?org_id={ctx['current_org_id']}&tab={tab}"
    return url + ("&only_left=1" if only_left else "")


@router.get("/", response_class=HTMLResponse)
def count_page(request: Request, org_id: str | None = None, tab: str = "food",
               only_left: str = "", db: Session = Depends(get_db)):
    ctx, org_ids = _ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)

    count = stock_count.get_active(db, ctx["current_org"].id)
    if count is None:
        last = (
            db.query(StockCount)
            .filter(StockCount.organization_id == ctx["current_org"].id,
                    StockCount.status == "applied")
            .order_by(StockCount.applied_at.desc())
            .first()
        )
        ctx.update({
            "count": None,
            "last": last,
            "today": date_type.today().isoformat(),
            "candidates": len(stock_count.working_set(db, org_ids)),
        })
        return templates.TemplateResponse("warehouse/count_start.html", ctx)

    balances = stock_count._balance_map(db, org_ids)
    rows = (
        db.query(StockCountLine, Product)
        .join(Product, Product.id == StockCountLine.product_id)
        .filter(StockCountLine.count_id == count.id)
        .order_by(Product.name)
        .all()
    )

    tabs = {"food": [], "house": [], "unsorted": []}
    for line, product in rows:
        b = balances.get(product.id, {"balance": Decimal("0"), "has_price": False})
        key = ("food" if product.category in stock_count.FOOD_CATEGORIES
               else ("unsorted" if product.category is None else "house"))
        tabs[key].append({
            "line": line,
            "product": product,
            "category": product.category or "не разобрано",
            "balance": b["balance"],
            "has_price": b["has_price"],
        })

    shown = tabs.get(tab, tabs["food"])
    if only_left:
        shown = [r for r in shown if r["line"].actual_qty is None]

    sections = {}
    for r in shown:
        sections.setdefault(r["category"], []).append(r)

    starter = db.get(User, count.started_by)
    ctx.update({
        "count": count,
        "sections": sorted(sections.items(),
                           key=lambda kv: stock_count.category_sort_key(kv[0])),
        "tab": tab,
        "only_left": only_left,
        "tab_counts": {
            k: {"total": len(v),
                "marked": sum(1 for r in v if r["line"].actual_qty is not None)}
            for k, v in tabs.items()
        },
        "progress": stock_count.progress(db, count.id),
        "issues": stock_count.ISSUES,
        "started_by_name": starter.name if starter else "?",
        "photos": count.photos,
    })
    return templates.TemplateResponse("warehouse/count.html", ctx)


@router.post("/start")
def count_start(request: Request, org_id: str = Form(""), count_date: str = Form(""),
                db: Session = Depends(get_db)):
    ctx, org_ids = _ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    # Одну активную сессию на объект держит ещё и частичный уникальный индекс,
    # но два быстрых клика столкнулись бы на вставке и отдали 500 вместо экрана.
    acquire_submission_lock(db, "stock_count_start", str(ctx["current_org"].id))
    d = date_type.fromisoformat(count_date) if count_date else date_type.today()
    stock_count.start(db, ctx["current_org"].id, org_ids, ctx["current_user"].id, d)
    db.commit()
    return RedirectResponse(_back(ctx), status_code=303)


@router.post("/line/{line_id}")
def count_mark(line_id: int, request: Request, org_id: str = Form(""),
               mode: str = Form(...), value: str = Form(""), tab: str = Form("food"),
               only_left: str = Form(""), db: Session = Depends(get_db)):
    ctx, org_ids = _ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    line = db.get(StockCountLine, line_id)
    if line is None or line.count.status != "active":
        return RedirectResponse(_back(ctx, tab, only_left), status_code=303)

    if mode == "clear":
        stock_count.unmark(db, line)
    else:
        balances = stock_count._balance_map(db, org_ids)
        expected = balances.get(line.product_id, {"balance": Decimal("0")})["balance"]
        parsed = None
        if mode == stock_count.MODE_NUMBER:
            raw = (value or "").strip().replace(",", ".").replace(" ", "")
            try:
                parsed = Decimal(raw)
            except (InvalidOperation, ValueError):
                # Мусор в поле не должен молча превращаться в ноль — тот же
                # урок, что и с рецептами 29.07 («тихий ноль»).
                return RedirectResponse(_back(ctx, tab, only_left), status_code=303)
        stock_count.mark(db, line, mode, parsed, expected, ctx["current_user"].id)
    db.commit()
    return RedirectResponse(_back(ctx, tab, only_left), status_code=303)


@router.post("/line/{line_id}/flag")
def count_flag(line_id: int, request: Request, org_id: str = Form(""),
               issue: str = Form(""), note: str = Form(""), tab: str = Form("food"),
               db: Session = Depends(get_db)):
    ctx, _ = _ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    line = db.get(StockCountLine, line_id)
    if line is not None and line.count.status == "active":
        stock_count.flag(db, line, issue, note, ctx["current_user"].id)
        db.commit()
    return RedirectResponse(_back(ctx, tab), status_code=303)


@router.post("/photo")
async def count_photo_upload(request: Request, org_id: str = Form(""),
                             caption: str = Form(""), tab: str = Form("food"),
                             file: UploadFile = File(...),
                             db: Session = Depends(get_db)):
    """Приложить снимок бумажного листа к активному пересчёту.

    Лист — основание для цифр, поэтому грузится к сессии, а не «куда-нибудь в
    расходы»: иначе через месяц связь акта с тетрадью придётся восстанавливать
    по датам."""
    ctx, _ = _ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    count = stock_count.get_active(db, ctx["current_org"].id)
    if count is None:
        return RedirectResponse(_back(ctx, tab), status_code=303)

    ext = Path(file.filename or "").suffix.lower()
    if ext not in PHOTO_EXTS:
        return RedirectResponse(_back(ctx, tab) + "&photo_err=type", status_code=303)
    data = await file.read()
    if not data:
        return RedirectResponse(_back(ctx, tab) + "&photo_err=empty", status_code=303)
    if len(data) > PHOTO_MAX_BYTES:
        return RedirectResponse(_back(ctx, tab) + "&photo_err=size", status_code=303)

    month_dir = PHOTO_DIR / datetime.now().strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex[:12]}{ext}"
    (month_dir / name).write_bytes(data)

    db.add(StockCountPhoto(
        count_id=count.id,
        file_path=f"stock_counts/{month_dir.name}/{name}",
        caption=(caption or "").strip()[:200] or None,
        uploaded_by=ctx["current_user"].id,
    ))
    db.commit()
    return RedirectResponse(_back(ctx, tab), status_code=303)


@router.post("/photo/{photo_id}/delete")
def count_photo_delete(photo_id: int, request: Request, org_id: str = Form(""),
                       tab: str = Form("food"), db: Session = Depends(get_db)):
    """Убрать ошибочно приложенный снимок. Файл с диска не трогаем — снимок
    мог быть приложен и к уже завершённому пересчёту, а удаление файла сделало
    бы дыру в чужой истории."""
    ctx, _ = _ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    photo = db.get(StockCountPhoto, photo_id)
    if photo is not None and photo.count.organization_id == ctx["current_org"].id:
        db.delete(photo)
        db.commit()
    return RedirectResponse(_back(ctx, tab), status_code=303)


@router.get("/finish", response_class=HTMLResponse)
def count_finish(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    ctx, org_ids = _ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    count = stock_count.get_active(db, ctx["current_org"].id)
    if count is None:
        return RedirectResponse(_back(ctx), status_code=303)
    ctx.update({"count": count, "summary": stock_count.summary(db, count, org_ids),
                "photos": count.photos})
    return templates.TemplateResponse("warehouse/count_finish.html", ctx)


@router.post("/apply")
def count_apply(request: Request, org_id: str = Form(""), db: Session = Depends(get_db)):
    ctx, org_ids = _ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    acquire_submission_lock(db, "stock_count_apply", str(ctx["current_org"].id))
    count = stock_count.get_active(db, ctx["current_org"].id)
    if count is None:
        return RedirectResponse(_back(ctx), status_code=303)
    stock_count.apply(db, count, org_ids, ctx["current_user"].id)
    db.commit()
    return RedirectResponse(f"/warehouse/?org_id={ctx['current_org_id']}", status_code=303)


@router.post("/cancel")
def count_cancel(request: Request, org_id: str = Form(""), reason: str = Form(""),
                 db: Session = Depends(get_db)):
    ctx, _ = _ctx(request, db, org_id)
    if ctx is None:
        return RedirectResponse("/login", status_code=302)
    count = stock_count.get_active(db, ctx["current_org"].id)
    if count is not None:
        stock_count.cancel(db, count, ctx["current_user"].id, reason)
        db.commit()
    return RedirectResponse(_back(ctx), status_code=303)
