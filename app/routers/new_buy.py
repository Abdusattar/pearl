"""Новый вход `/new`: экран «Купили» и карточка покупки.

Макет версии 7, блок 2б/2в (утверждён владельцем 15.09), план
`context/revision/07_buy_screen.md`. Та же база, что у старого входа; здесь
только другая поверхность: одна форма на любой закуп, единица из карточки,
фасовка, вопросы вместо молчаливой записи (цена, похожий товар, повтор).
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, resolve_org
from app.models import Organization, Product, ProductCategory, Purchase, Receipt, Supplier, User
from app.services import purchases as svc
from app.services.ocr import compute_hash
from app.services.price_check import fmt_money, usual_price
from app.services.products import UNITS, rank_candidates
from app.services.supplier_ledger import get_supplier_balance

router = APIRouter(prefix="/new", tags=["new"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
templates.env.filters["money"] = lambda v: fmt_money(float(v or 0))
# количество: 5.0 → «5», 0.2 → «0,2»
templates.env.filters["qty"] = lambda v: fmt_money(float(v)) if v is not None else ""

MEDIA_DIR = Path(__file__).parent.parent.parent / "media" / "receipts"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

WRITE_ROLES = ("owner", "staff", "manager", "director")
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def human_date(d: date | None, with_weekday: bool = False) -> str:
    if d is None:
        return ""
    s = f"{d.day} {MONTHS[d.month - 1]}"
    if with_weekday:
        return f"{WEEKDAYS[d.weekday()]}, {s}"
    if d == date.today():
        return f"сегодня, {s}"
    return s


templates.env.filters["human_date"] = human_date


def _site(user: User, db: Session) -> Organization | None:
    org = resolve_org(None, user, db)
    if org is None:
        return None
    return db.get(Organization, org.site_org_id)


def _base_ctx(request: Request, user: User, site: Organization, db: Session, page: str) -> dict:
    return {
        "request": request, "current_user": user, "site": site, "active_page": page,
        "site_orgs": svc.site_orgs(db, site.id),
    }


def _form_ctx(request: Request, user: User, site: Organization, db: Session, *,
              supplier: Supplier | None, other: bool, rows: list[dict], last_date: date | None,
              tx_date: date, payment: str | None, for_org: str, payer_id: int | None,
              account_org_id: int | None, founder_id: int | None, paid_amount: str,
              note: str, photo_receipt_id: int | None, dup: dict | None, error: str | None,
              ask_for_org: bool = False) -> dict:
    ctx = _base_ctx(request, user, site, db, "expenses")
    chips = svc.suggest_suppliers(db, site.id)
    if supplier and supplier.id not in {s.id for s in chips}:
        chips.append(supplier)
    supplier_debt = float(get_supplier_balance(db, supplier.id)) if supplier else 0.0
    if payment is None:
        payment = svc.default_payment(db, supplier.id) if supplier else "cash"
    if not rows or rows[-1]["name"]:
        rows = rows + [svc._row_dict(None)]
    total = sum(float(r["total_price"].replace(",", ".")) for r in rows if r.get("total_price"))
    ctx.update({
        "chips": chips, "supplier": supplier, "other": other or (supplier is None and not chips),
        "all_suppliers": db.query(Supplier).order_by(Supplier.name).all(),
        "rows": rows, "last_date": last_date, "tx_date": tx_date, "today": date.today(),
        "payment": payment, "for_org": for_org, "ask_for_org": ask_for_org,
        "payer_id": payer_id or user.id, "account_org_id": account_org_id, "founder_id": founder_id,
        "paid_amount": paid_amount, "note": note, "photo_receipt_id": photo_receipt_id,
        "pockets": svc.pocket_users(db, site.id), "founders": svc.founders(db),
        "categories": db.query(ProductCategory).order_by(ProductCategory.sort_order).all(),
        "units": UNITS, "supplier_debt": supplier_debt, "total": total,
        "dup": dup, "error": error,
    })
    return ctx


@router.get("/", response_class=HTMLResponse)
def new_root():
    return RedirectResponse("/new/today", status_code=302)


@router.get("/products/search")
def search_products(q: str = "", db: Session = Depends(get_db)):
    """Подсказка в строке: имя, единица, фасовка, обычная цена, уровень."""
    if not q.strip():
        return JSONResponse([])
    cands = rank_candidates(db, q.strip(), limit=6, standard_only=False)
    ids = [c["id"] for c in cands]
    products = {p.id: p for p in db.query(Product).filter(Product.id.in_(ids)).all()} if ids else {}
    out = []
    for c in cands:
        p = products.get(c["id"])
        if p is None:
            continue
        usual = usual_price(db, p.id)
        out.append({
            "id": p.id, "name": p.name, "unit": p.unit or "", "score": c["score"],
            "pack_name": p.pack_name if p.pack_qty else None,
            "pack_qty": float(p.pack_qty) if p.pack_qty else None,
            "usual": fmt_money(usual) if usual else None,
            "minor": bool(p.product_category and p.product_category.is_minor),
        })
    return JSONResponse(out)


@router.get("/buy", response_class=HTMLResponse)
def buy_form(request: Request, supplier: int | None = None, other: int = 0, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    sup = db.get(Supplier, supplier) if supplier else None
    rows, last_date = [], None
    if sup:
        last_date, rows = svc.prefill_from_last(db, sup.id)
    ctx = _form_ctx(request, user, site, db, supplier=sup, other=bool(other), rows=rows, last_date=last_date,
                    tx_date=date.today(), payment=None, for_org="shared", payer_id=None, account_org_id=None,
                    founder_id=None, paid_amount="", note="", photo_receipt_id=None, dup=None, error=None)
    return templates.TemplateResponse("new/buy.html", ctx)


def _int_or_none(v) -> int | None:
    v = (v or "").strip() if isinstance(v, str) else v
    return int(v) if isinstance(v, str) and v.isdigit() else (v if isinstance(v, int) else None)


@router.post("/buy", response_class=HTMLResponse)
async def buy_submit(request: Request, photo: UploadFile | None = File(None), db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Покупки записывают сотрудники площадки", status_code=403)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)

    form = await request.form()
    lists = {k: form.getlist(k) for k in (
        "item_name", "item_product_id", "item_qty", "item_unit", "item_unit_price",
        "item_price_ok", "item_new", "item_category_id",
    )}
    supplier_id = _int_or_none(form.get("supplier_id"))
    new_supplier_name = (form.get("new_supplier_name") or "").strip()
    other = form.get("other") == "1"
    date_str = (form.get("date") or "").strip()
    tx_date = date.fromisoformat(date_str) if date_str else date.today()
    payment = form.get("payment") or "cash"
    for_org = form.get("for_org") or "shared"
    payer_id = _int_or_none(form.get("payer_id")) or user.id
    account_org_id = _int_or_none(form.get("account_org_id"))
    founder_id = _int_or_none(form.get("founder_id"))
    paid_amount = (form.get("paid_amount") or "").strip()
    note = (form.get("note") or "").strip() or None
    photo_receipt_id = _int_or_none(form.get("photo_receipt_id"))
    dup_ok = form.get("dup_ok") == "1"

    supplier = db.get(Supplier, supplier_id) if supplier_id else None
    if supplier is None and new_supplier_name:
        supplier = db.query(Supplier).filter(Supplier.name == new_supplier_name).first()
        if supplier is None:
            # Сразу в базу: форма может вернуться с вопросами, поставщик не должен пропасть.
            supplier = Supplier(name=new_supplier_name, phone="0000")
            db.add(supplier)
            db.commit()

    def render(error: str | None = None, questions: dict | None = None, dup: dict | None = None,
               ask_for_org: bool = False):
        rows = svc.buy_rows_as_submitted(db, lists, questions)
        _, last_date = None, None
        ctx = _form_ctx(request, user, site, db, supplier=supplier, other=other or supplier is None, rows=rows,
                        last_date=last_date, tx_date=tx_date, payment=payment, for_org=for_org,
                        payer_id=payer_id, account_org_id=account_org_id, founder_id=founder_id,
                        paid_amount=paid_amount, note=note or "", photo_receipt_id=photo_receipt_id,
                        dup=dup, error=error, ask_for_org=ask_for_org)
        return templates.TemplateResponse("new/buy.html", ctx)

    if supplier is None:
        return render("Выберите, у кого купили")

    # Фото: сохраняем сразу и держим по id, пока идут вопросы (файл в форме не переживает перерисовку)
    file_hash = None
    if photo is not None and photo.filename:
        data = await photo.read()
        if data:
            file_hash = compute_hash(data)
            existing = db.query(Receipt).filter(Receipt.file_hash == file_hash).first()
            if existing is not None and existing.id != photo_receipt_id:
                if not dup_ok:
                    return render(dup={"kind": "photo", "date": existing.created_at.date() if existing.created_at else None,
                                       "total": float(existing.amount_confirmed or existing.amount_detected or 0)})
                photo_receipt_id = existing.id
            elif existing is None:
                month_dir = MEDIA_DIR / datetime.now().strftime("%Y-%m")
                month_dir.mkdir(parents=True, exist_ok=True)
                suffix = Path(photo.filename).suffix or ".jpg"
                fname = f"{file_hash[:12]}{suffix}"
                (month_dir / fname).write_bytes(data)
                receipt = Receipt(organization_id=site.id, file_path=f"receipts/{datetime.now().strftime('%Y-%m')}/{fname}",
                                  file_hash=file_hash, ocr_status="pending", created_by=user.id)
                db.add(receipt)
                db.commit()
                photo_receipt_id = receipt.id

    items, questions, error = svc.resolve_buy_rows(db, lists, tx_date)
    if error:
        return render(error)
    if questions:
        return render("Ответьте на вопросы в отмеченных строках и запишите снова", questions)
    if not items:
        return render("Добавьте хотя бы одну строку")

    for_org_id = None
    if for_org != "shared":
        for_org_id = _int_or_none(for_org)
        if for_org_id not in {o.id for o in svc.site_orgs(db, site.id)}:
            return render("Выберите, для кого покупка")
    elif svc.needs_for_org(db, items) and form.get("for_org_confirmed") != "1":
        return render("В списке только ремонт или стройматериалы. Для кого: школа или садик?", ask_for_org=True)

    if payment not in svc.PAYMENTS:
        return render("Выберите, как оплатили")
    paid_val = None
    if payment == "part":
        try:
            paid_val = float(paid_amount.replace(",", ".")) if paid_amount else None
        except ValueError:
            paid_val = None
        if paid_val is None or paid_val <= 0:
            return render("Укажите, сколько заплатили сейчас")
    if payment == "account" and account_org_id not in {o.id for o in svc.site_orgs(db, site.id)}:
        return render("Со счёта садика или школы? Выберите")
    if payment == "founder" and not founder_id:
        return render("Кто из учредителей заплатил?")

    total = round(sum(it["total"] for it in items), 2)
    if not dup_ok:
        dup = svc.find_duplicate(db, supplier.id, tx_date, total,
                                 {it["product"].id for it in items if it.get("product")})
        if dup:
            return render(dup=dup)

    purchase = svc.record_purchase(
        db, user=user, site_org_id=site.id, supplier_id=supplier.id, tx_date=tx_date, items=items,
        payment=payment, paid_amount=paid_val, payer_id=payer_id, account_org_id=account_org_id,
        founder_id=founder_id, for_org_id=for_org_id, receipt_id=photo_receipt_id, note=note, dup_confirmed=dup_ok,
    )
    db.commit()
    return RedirectResponse(f"/new/buy/{purchase.id}?saved=1", status_code=303)


@router.get("/buy/{purchase_id}", response_class=HTMLResponse)
def purchase_card(purchase_id: int, request: Request, saved: int = 0, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    purchase = db.get(Purchase, purchase_id)
    if site is None or purchase is None or purchase.site_org_id != site.id:
        return HTMLResponse("Покупка не найдена", status_code=404)
    ctx = _base_ctx(request, user, site, db, "expenses")
    receipt = db.get(Receipt, purchase.receipt_id) if purchase.receipt_id else None
    ctx.update({
        "p": purchase, "lines": svc.purchase_lines(db, purchase), "saved": bool(saved),
        "supplier_debt": float(get_supplier_balance(db, purchase.supplier_id)),
        "photo": receipt.file_path if receipt and receipt.file_path != "manual" else None,
        "can_write": user.role in WRITE_ROLES,
    })
    return templates.TemplateResponse("new/purchase.html", ctx)


@router.post("/buy/{purchase_id}/remove")
def purchase_remove(purchase_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Нет прав", status_code=403)
    site = _site(user, db)
    purchase = db.get(Purchase, purchase_id)
    if site is None or purchase is None or purchase.site_org_id != site.id:
        return HTMLResponse("Покупка не найдена", status_code=404)
    if purchase.deleted_at is None:
        svc.remove_purchase(db, purchase, user)
        db.commit()
    return RedirectResponse(f"/new/buy/{purchase.id}", status_code=303)
