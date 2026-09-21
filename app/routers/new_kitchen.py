"""Новый вход `/new/kitchen`: лист кухни за день (макет 3б, план 08)."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user
from app.models import Organization, Product, User
from app.routers.new_buy import WRITE_ROLES, _base_ctx, _site, templates
from app.services import kitchen as svc
from app.services import drafts, once
from app.services.purchases import site_orgs
from app.services import recognize as rz
from app.services.ocr import compute_hash
from app.services.products import rank_candidates

router = APIRouter(prefix="/new", tags=["new"])

MEDIA_DIR = Path(__file__).parent.parent.parent / "media" / "kitchen"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _parse_date(s: str | None) -> date:
    s = (s or "").strip()
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _ctx(request: Request, user, site: Organization, db: Session, *, d: date, rows: list[dict],
         sheet, children: str, like_date: date | None, saved: date | None, next_day: date | None,
         error: str | None, photo_path: str | None) -> dict:
    ctx = _base_ctx(request, user, site, db, "warehouse")
    missing = svc.missing_days(db, site.id)
    if not rows or rows[-1]["name"]:
        rows = rows + [{"product_id": None, "name": "", "unit": "", "sub_unit": None, "sub_factor": None,
                        "chosen_unit": "", "qty": "", "minor": False, "balance": None, "balance_text": "", "error": None}]
    ctx.update({
        "d": d, "today": date.today(), "rows": rows, "sheet": sheet, "children": children,
        "like_date": like_date, "saved": saved, "next_day": next_day, "error": error,
        "missing": [x for x in missing if x != d][:6], "missing_total": len([x for x in missing if x != d]),
        "photo_path": photo_path, "can_write": user.role in WRITE_ROLES,
        "shortfalls": (sheet.shortfalls if sheet else None) if saved else None,
    })
    return ctx


def _draft_info(db: Session, r) -> dict:
    """Полоса «Из чата: прислала Мунара 21.09 в 15:38» и день с листа, если прочитан."""
    who = db.get(User, r.created_by) if r.created_by else None
    p = r.payload or {}
    raw = p.get("date")
    d = _parse_date(raw) if isinstance(raw, str) and len(raw) == 10 else None
    return {"id": r.id, "path": r.file_path, "by": who.name if who else None, "at": r.created_at,
            "source": r.source, "date": d}


def _draft_rows(db: Session, site: Organization, r, balances: dict) -> tuple[list[dict], dict, str | None]:
    """Строки черновика: распознаются один раз и запоминаются в черновике."""
    p = dict(r.payload or {})
    cached = p.get("rows")
    note = None
    if cached is None:
        try:
            data = (MEDIA_DIR.parent / r.file_path).read_bytes()
            out = rz.recognize(db, data, rz.KITCHEN, site.id,
                               mime="image/png" if r.file_path.lower().endswith(".png") else "image/jpeg")
            cached = [{"product_id": x["product_id"], "name": x["name"] or x["raw"], "qty": x["qty"],
                       "unit": x["unit"] if x["product_id"] else "",
                       "question": (x.get("question") or {}).get("text") or "; ".join(x.get("notes") or []) or None}
                      for x in out["rows"]]
            p["rows"] = cached
            r.payload = p
            db.commit()
        except Exception as e:  # noqa: BLE001 — модель недоступна: лист заполняется руками
            cached, note = [], f"Фото не разобралось ({e}). Заполните строки руками, фото рядом."
    lists = {"item_product_id": [], "item_name": [], "item_qty": [], "item_unit": []}
    errors = {}
    for i, x in enumerate(cached):
        lists["item_product_id"].append(str(x["product_id"]) if x.get("product_id") else "")
        lists["item_name"].append(x.get("name") or "")
        lists["item_qty"].append(svc.fmt_qty(x["qty"]) if x.get("qty") is not None else "")
        lists["item_unit"].append(x.get("unit") or "")
        if x.get("question"):
            errors[i] = x["question"]
    if not cached and note is None:
        note = "На фото не нашлось строк. Заполните руками, фото рядом."
    return svc.rows_as_submitted(db, lists, balances, errors), errors, note


@router.get("/kitchen/search")
def kitchen_search(request: Request, q: str = "", db: Session = Depends(get_db)):
    """Подсказка продукта для листа: имя, единица, дробная единица, остаток, мелочь."""
    user = get_current_user(request, db)
    if not user or not q.strip():
        return JSONResponse([])
    site = _site(user, db)
    balances = svc.stock_map(db, site.id) if site else {}
    cands = rank_candidates(db, q.strip(), limit=8, standard_only=False)
    ids = [c["id"] for c in cands]
    products = {p.id: p for p in db.query(Product).filter(Product.id.in_(ids)).all()} if ids else {}
    out = []
    for c in cands:
        p = products.get(c["id"])
        if not p:
            continue
        minor = svc.is_minor(p)
        bal = balances.get(p.id, 0.0)
        sub = svc.sub_unit(p)
        out.append({"id": p.id, "name": p.name, "unit": p.unit or "", "minor": minor,
                    "sub_unit": sub[0] if sub else None,
                    "balance": None if minor else round(bal, 3),
                    "balance_text": "" if minor else f"{svc.fmt_qty(bal)} {p.unit or ''}",
                    "score": c["score"]})
    # точное или по началу слова — первым; среди остальных основные с остатком впереди
    out.sort(key=lambda r: (r["score"] < 95, r["minor"] or (r["balance"] or 0) <= 0, -r["score"]))
    return JSONResponse(out)


@router.get("/kitchen", response_class=HTMLResponse)
def kitchen_form(request: Request, date_: str | None = None, like: int = 0, saved: str | None = None,
                 db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    draft_id = request.query_params.get("draft")
    if draft_id and draft_id.isdigit():
        r = drafts.open_draft(db, {o.id for o in site_orgs(db, site.id)} | {site.id}, int(draft_id), drafts.KITCHEN)
        if r is None:
            return RedirectResponse("/new/receipts", status_code=302)   # уже внесён или отложен
        info = _draft_info(db, r)
        balances = svc.stock_map(db, site.id)
        rows, _errors, note = _draft_rows(db, site, r, balances)
        d = info["date"] or svc.default_day(db, site.id)
        ctx = _ctx(request, user, site, db, d=d, rows=rows, sheet=None, children="", like_date=None, saved=None,
                   next_day=None, error=note, photo_path=None)
        ctx.update({"draft": info, "need_day": info["date"] is None})
        return templates.TemplateResponse("new/kitchen.html", ctx)
    d = _parse_date(request.query_params.get("date")) or svc.default_day(db, site.id)
    balances = svc.stock_map(db, site.id)
    sheet = svc.sheet_for(db, site.id, d)
    like_date = None
    if like:
        like_date, rows = svc.like_last_rows(db, site.id, d, balances)
    elif sheet:
        rows = svc.sheet_rows(db, sheet, balances)
    else:
        rows = []
    saved_d = _parse_date(saved)
    saved_sheet = svc.sheet_for(db, site.id, saved_d) if saved_d else None
    ctx = _ctx(request, user, site, db, d=d, rows=rows, sheet=sheet,
               children=str(sheet.children_count) if sheet and sheet.children_count else "",
               like_date=like_date, saved=saved_d, next_day=None, error=None,
               photo_path=sheet.photo_path if sheet else None)
    if saved_sheet:
        ctx["saved_sheet"] = saved_sheet
        ctx["shortfalls"] = saved_sheet.shortfalls
    return templates.TemplateResponse("new/kitchen.html", ctx)


@router.post("/kitchen", response_class=HTMLResponse)
async def kitchen_submit(request: Request, photo: UploadFile | None = File(None), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Лист кухни вносят сотрудники площадки", status_code=403)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)

    form = await request.form()
    lists = {k: form.getlist(k) for k in ("item_product_id", "item_name", "item_qty", "item_unit")}
    d = _parse_date(form.get("date"))
    children_raw = (form.get("children") or "").strip()
    balances = svc.stock_map(db, site.id)
    sheet = svc.sheet_for(db, site.id, d) if d else None

    draft_raw = str(form.get("draft_id") or "")
    draft = (drafts.open_draft(db, {o.id for o in site_orgs(db, site.id)} | {site.id}, int(draft_raw), drafts.KITCHEN)
             if draft_raw.isdigit() else None)

    def render(error: str | None, errors: dict | None = None, ask_replace: bool = False):
        rows = svc.rows_as_submitted(db, lists, balances, errors)
        ctx = _ctx(request, user, site, db, d=d or date.today(), rows=rows, sheet=None if draft else sheet,
                   children=children_raw, like_date=None, saved=None, next_day=None, error=error,
                   photo_path=sheet.photo_path if sheet and not draft else None)
        if draft is not None:
            ctx.update({"draft": _draft_info(db, draft), "need_day": d is None, "ask_replace": ask_replace})
        return templates.TemplateResponse("new/kitchen.html", ctx)

    if d is None or d > date.today():
        return render("Выберите день, не позже сегодняшнего")
    children = int(children_raw) if children_raw.isdigit() else None
    if children_raw and children is None:
        return render("Едоков — целое число")

    if form.get("action") == "recognize":
        if photo is None or not photo.filename:
            return render("Сначала выберите фото листа")
        data = await photo.read()
        if not data:
            return render("Файл пустой")
        try:
            out = rz.recognize(db, data, rz.KITCHEN, site.id,
                               mime="image/png" if photo.filename.lower().endswith(".png") else "image/jpeg")
        except Exception as e:  # noqa: BLE001
            return render(f"Не удалось разобрать фото: {e}")
        lists = {"item_product_id": [], "item_name": [], "item_qty": [], "item_unit": []}
        errors = {}
        for i, r in enumerate(out["rows"]):
            lists["item_product_id"].append(str(r["product_id"]) if r["product_id"] else "")
            lists["item_name"].append(r["name"] or r["raw"])
            lists["item_qty"].append(svc.fmt_qty(r["qty"]) if r["qty"] is not None else "")
            lists["item_unit"].append(r["unit"] if r["product_id"] else "")
            note = "; ".join(r.get("notes") or [])
            if r.get("question"):
                errors[i] = r["question"]["text"]
            elif note:
                errors[i] = note
        if not out["rows"]:
            return render("На фото не нашлось строк. Заполните руками.")
        return render("Строки заполнены с фото: проверьте каждую и нажмите «Внести»", errors)

    items, errors = svc.resolve_rows(db, lists)
    if errors:
        return render("Поправьте отмеченные строки", errors)
    if not items:
        return render("Добавьте хотя бы одну строку")
    if draft is not None and sheet is not None and form.get("replace_ok") != "1":
        who = sheet.creator.name if getattr(sheet, "creator", None) else "кто-то"
        return render(f"За {d.strftime('%d.%m')} лист уже внесён ({who}). Этот заменит его: нажмите «Внести» ещё раз, "
                      "если так и нужно, или выберите другой день.", ask_replace=True)

    token = once.clean(form.get("form_token"))
    if done := once.done_url(db, token):
        return RedirectResponse(done, status_code=303)
    photo_path = None
    if photo is not None and photo.filename:
        data = await photo.read()
        if data:
            h = compute_hash(data)
            month_dir = MEDIA_DIR / datetime.now().strftime("%Y-%m")
            month_dir.mkdir(parents=True, exist_ok=True)
            suffix = Path(photo.filename).suffix or ".jpg"
            fname = f"{d.isoformat()}_{h[:10]}{suffix}"
            (month_dir / fname).write_bytes(data)
            photo_path = f"kitchen/{datetime.now().strftime('%Y-%m')}/{fname}"

    if photo_path is None and draft is not None:
        photo_path = draft.file_path
    saved = svc.save_sheet(db, user=user, site_org_id=site.id, d=d, items=items,
                           children_count=children, photo_path=photo_path)
    db.flush()
    nxt = svc.next_missing_day(db, site.id, d)
    target = nxt.isoformat() if nxt else d.isoformat()
    url = f"/new/kitchen?date={target}&saved={d.isoformat()}"
    if draft is not None:
        drafts.done(db, draft, user=user, result_type="kitchen_sheet", result_id=saved.id)
        from app.services.bot import owner_copy
        owner_copy(db, f"{user.name}: лист кухни за {d.strftime('%d.%m')} из чата внесён, {len(items)} строк.")
        url = f"/new/receipts?done=kitchen&day={d.isoformat()}"
    once.remember(db, token, user.id, url)
    db.commit()
    return RedirectResponse(url, status_code=303)
