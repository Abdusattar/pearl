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
from app.services import legacy as lg
from app.services import once
from app.services import purchases as svc
from app.services import recognize as rz
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
# Итоги за период (закуп за месяц, пришло от родителей, фонд зарплаты…) видят
# только учредители, на «Обзоре»; исполнители — текучку и пробелы (владелец 21.09).
FOUNDER_ROLES = ("owner", "founder")


def is_founder(user) -> bool:
    return bool(user and user.role in FOUNDER_ROLES)


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


def plural(n, one: str, few: str, many: str) -> str:
    """1 ребёнок, 2 ребёнка, 5 детей."""
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


templates.env.filters["plural"] = plural


def _site(user: User, db: Session) -> Organization | None:
    org = resolve_org(None, user, db)
    if org is None:
        return None
    return db.get(Organization, org.site_org_id)


def _base_ctx(request: Request, user: User, site: Organization, db: Session, page: str) -> dict:
    return {
        "request": request, "current_user": user, "site": site, "active_page": page, "founder": is_founder(user),
        "site_orgs": svc.site_orgs(db, site.id),
    }


def _form_ctx(request: Request, user: User, site: Organization, db: Session, *,
              supplier: Supplier | None, other: bool, rows: list[dict], last_date: date | None,
              tx_date: date, payment: str | None, for_org: str, payer_id: int | None,
              account_org_id: int | None, founder_id: int | None, paid_amount: str,
              note: str, photo_receipt_id: int | None, dup: dict | None, error: str | None,
              ask_for_org: bool = False, replaces: Purchase | None = None, legacy: dict | None = None) -> dict:
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
        "payer_id": payer_id or svc.default_pocket(db, site.id, user), "account_org_id": account_org_id, "founder_id": founder_id,
        "paid_amount": paid_amount, "note": note, "photo_receipt_id": photo_receipt_id,
        "pockets": svc.pocket_users(db, site.id), "founders": svc.founders(db),
        "categories": db.query(ProductCategory).order_by(ProductCategory.sort_order).all(),
        "units": UNITS, "supplier_debt": supplier_debt, "total": total,
        "dup": dup, "error": error, "replaces": replaces, "legacy": legacy,
        "paid_warning": _paid_warning(db, replaces) if replaces else False,
    })
    return ctx


def _paid_warning(db: Session, old: Purchase) -> bool:
    """По долгу этой покупки уже платили: смена «в долг» на «из кассы» даст переплату."""
    if old.payment not in ("debt", "part"):
        return False
    unpaid = float(old.total) - float(old.paid_amount or 0)
    return float(get_supplier_balance(db, old.supplier_id)) < unpaid - 0.5


def _live_purchase(db: Session, site: Organization, purchase_id: int | None) -> Purchase | None:
    p = db.get(Purchase, purchase_id) if purchase_id else None
    if p is None or p.site_org_id != site.id or p.deleted_at is not None:
        return None
    return p


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


OPEN_RECEIPT = ("pending", "processed")   # чек с фото ещё не внесён и не отложен


def open_receipt(db: Session, site: Organization, receipt_id: int | None) -> Receipt | None:
    """Чек с фото, который ждёт внесения: с телефона, из чата, брошенный на полпути."""
    r = db.get(Receipt, receipt_id) if receipt_id else None
    if r is None or r.file_path == "manual" or r.ocr_status not in OPEN_RECEIPT:
        return None
    if r.organization_id not in {o.id for o in svc.site_orgs(db, site.id)} | {site.id}:
        return None
    return r


def _draft(db: Session, r: Receipt) -> dict:
    who = db.get(User, r.created_by) if r.created_by else None
    return {"id": r.id, "path": r.file_path, "by": who.name if who else None,
            "date": r.created_at.date() if r.created_at else None}


@router.get("/buy", response_class=HTMLResponse)
def buy_form(request: Request, supplier: int | None = None, other: int = 0, receipt: int | None = None,
             db: Session = Depends(get_db)):
    """«Купили». С `receipt` — черновик с фото (21.09): тот же экран, уже
    заполненный распознанным. Этим же экраном Махабат подтверждает чеки из чата."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    if site is None:
        return HTMLResponse("Объект не найден", status_code=404)
    sup = db.get(Supplier, supplier) if supplier else None
    rows, last_date, error, recognized = [], None, None, None
    rc = open_receipt(db, site, receipt)
    if receipt and rc is None:
        return RedirectResponse("/new/today", status_code=302)   # уже внесён или отложен
    if rc is not None and sup is not None:
        path = MEDIA_DIR.parent / rc.file_path
        try:
            out = rz.recognize(db, path.read_bytes(), rz.RECEIPT, site.id, sup.id,
                               mime="image/png" if path.suffix.lower() == ".png" else "image/jpeg")
            lists, questions, hints = svc.rows_from_recognized(out["rows"])
            rows = svc.buy_rows_as_submitted(db, lists, questions or None, hints) if out["rows"] else []
            recognized = out.get("amount")
            if not out["rows"]:
                error = "На фото не нашлось строк с товарами. Заполните руками, фото останется приложенным."
        except Exception as e:  # noqa: BLE001 — модель недоступна: форма остаётся рабочей
            error = f"Не удалось разобрать фото: {e}. Заполните руками, фото останется приложенным."
    elif sup:
        last_date, rows = svc.prefill_from_last(db, sup.id)
    ctx = _form_ctx(request, user, site, db, supplier=sup, other=bool(other), rows=rows, last_date=last_date,
                    tx_date=date.today(), payment=None, for_org="shared", payer_id=None, account_org_id=None,
                    founder_id=None, paid_amount="", note="", photo_receipt_id=rc.id if rc else None, dup=None,
                    error=error)
    ctx["draft"] = _draft(db, rc) if rc else None
    ctx["recognized_amount"] = recognized
    return templates.TemplateResponse("new/buy.html", ctx)


@router.post("/receipt/{receipt_id}/skip")
async def receipt_skip(receipt_id: int, request: Request, db: Session = Depends(get_db)):
    """«Не вносить» чек с фото: уже внесён, дубль, не наш. Причина обязательна,
    чек остаётся в базе с пометкой."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Нет прав", status_code=403)
    site = _site(user, db)
    rc = open_receipt(db, site, receipt_id) if site else None
    form = await request.form()
    reason = (form.get("reason") or "").strip()
    if rc is None:
        return RedirectResponse("/new/today", status_code=303)
    back = str(form.get("back") or "")
    back = back if back.startswith("/new/") else ""
    if not reason:
        return RedirectResponse(f"/new/buy?receipt={rc.id}&skip_error=1", status_code=303)
    rc.ocr_status = "rejected"
    svc.audit(db, "receipt", rc.id, "update", user.id, {"ocr_status": "rejected", "reason": reason})
    db.commit()
    return RedirectResponse(f"{back or '/new/today'}?skipped=1", status_code=303)


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
    payer_id = _int_or_none(form.get("payer_id")) or svc.default_pocket(db, site.id, user)
    account_org_id = _int_or_none(form.get("account_org_id"))
    founder_id = _int_or_none(form.get("founder_id"))
    paid_amount = (form.get("paid_amount") or "").strip()
    note = (form.get("note") or "").strip() or None
    photo_receipt_id = _int_or_none(form.get("photo_receipt_id"))
    dup_ok = form.get("dup_ok") == "1"
    replaces = _live_purchase(db, site, _int_or_none(form.get("replaces_id")))
    if form.get("replaces_id") and replaces is None:
        return HTMLResponse("Эту покупку уже поправили или убрали — откройте её заново", status_code=409)
    legacy_key = _legacy_key(form.get("replaces_legacy"))
    legacy_rows, legacy_ctx = [], None
    if legacy_key is not None:
        legacy_rows = lg.txs(db, site.id, *legacy_key)
        if not legacy_rows:
            return HTMLResponse("Эту запись уже поправили или убрали — откройте её заново", status_code=409)
        legacy_ctx = {"key": f"{legacy_key[0]}:{legacy_key[1]}", "date": legacy_rows[0].date,
                      "total": sum(t.amount for t in legacy_rows)}

    supplier = db.get(Supplier, supplier_id) if supplier_id else None
    if supplier is None and new_supplier_name:
        supplier = db.query(Supplier).filter(Supplier.name == new_supplier_name).first()
        if supplier is None:
            # Сразу в базу: форма может вернуться с вопросами, поставщик не должен пропасть.
            supplier = Supplier(name=new_supplier_name, phone="0000")
            db.add(supplier)
            db.commit()

    def render(error: str | None = None, questions: dict | None = None, dup: dict | None = None,
               ask_for_org: bool = False, hints: dict | None = None, recognized_amount: float | None = None):
        rows = svc.buy_rows_as_submitted(db, lists, questions, hints)
        _, last_date = None, None
        ctx = _form_ctx(request, user, site, db, supplier=supplier, other=other or supplier is None, rows=rows,
                        last_date=last_date, tx_date=tx_date, payment=payment, for_org=for_org,
                        payer_id=payer_id, account_org_id=account_org_id, founder_id=founder_id,
                        paid_amount=paid_amount, note=note or "", photo_receipt_id=photo_receipt_id,
                        dup=dup, error=error, ask_for_org=ask_for_org, replaces=replaces, legacy=legacy_ctx)
        ctx["recognized_amount"] = recognized_amount
        rc = open_receipt(db, site, photo_receipt_id) if replaces is None and legacy_ctx is None else None
        ctx["draft"] = _draft(db, rc) if rc else None
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
            same_legacy = bool(legacy_key and legacy_key[0] == "r" and existing is not None and existing.id == legacy_key[1])
            if existing is not None and existing.id != photo_receipt_id and not (replaces and existing.id == replaces.receipt_id) \
                    and not same_legacy:
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

    if form.get("action") == "recognize":
        # «Заполнить с фото»: единый конвейер (services/recognize.py), ничего не пишет
        if not photo_receipt_id:
            return render("Сначала выберите фото чека")
        receipt = db.get(Receipt, photo_receipt_id)
        path = MEDIA_DIR.parent / receipt.file_path
        try:
            out = rz.recognize(db, path.read_bytes(), rz.RECEIPT, site.id, supplier.id,
                               mime="image/png" if path.suffix.lower() == ".png" else "image/jpeg")
        except Exception as e:  # noqa: BLE001 — модель недоступна: форма остаётся рабочей
            return render(f"Не удалось разобрать фото: {e}")
        lists, questions, hints = svc.rows_from_recognized(out["rows"])
        if not out["rows"]:
            return render("На фото не нашлось строк с товарами. Заполните руками.")
        return render(None, questions or None, hints=hints, recognized_amount=out.get("amount"))

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

    token = once.clean(form.get("form_token"))
    if done := once.done_url(db, token):
        return RedirectResponse(done, status_code=303)
    total = round(sum(it["total"] for it in items), 2)
    if not dup_ok and replaces is None and not legacy_rows:   # правка сама себе не повтор
        dup = svc.find_duplicate(db, supplier.id, tx_date, total,
                                 {it["product"].id for it in items if it.get("product")})
        if dup:
            return render(dup=dup)

    old_tx_ids = [t.id for t in replaces.transactions] if replaces is not None else [t.id for t in legacy_rows]
    if legacy_rows:
        lg.remove(db, legacy_rows, user, "поправлено в новом входе")
        if legacy_key[0] == "r" and photo_receipt_id == legacy_key[1]:
            from app.models import ReceiptItem
            db.query(ReceiptItem).filter(ReceiptItem.receipt_id == photo_receipt_id).delete(synchronize_session=False)
    if replaces is not None:
        kept = svc.replace_purchase(db, replaces, user)
        if photo_receipt_id == replaces.receipt_id:
            photo_receipt_id = kept
    purchase = svc.record_purchase(
        db, user=user, site_org_id=site.id, supplier_id=supplier.id, tx_date=tx_date, items=items,
        payment=payment, paid_amount=paid_val, payer_id=payer_id, account_org_id=account_org_id,
        founder_id=founder_id, for_org_id=for_org_id, receipt_id=photo_receipt_id, note=note, dup_confirmed=dup_ok,
    )
    if replaces is not None:
        purchase.replaces_id = replaces.id
    if old_tx_ids:
        svc.keep_entry_time(db, purchase, old_tx_ids)
    url = f"/new/buy/{purchase.id}?saved=1"
    once.remember(db, token, user.id, url)
    db.commit()
    return RedirectResponse(url, status_code=303)


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
        "previous": db.get(Purchase, purchase.replaces_id) if purchase.replaces_id else None,
        "next_version": db.query(Purchase).filter(Purchase.replaces_id == purchase.id).first(),
        "p": purchase, "lines": svc.purchase_lines(db, purchase), "saved": bool(saved),
        "supplier_debt": float(get_supplier_balance(db, purchase.supplier_id)),
        "photo": receipt.file_path if receipt and receipt.file_path != "manual" else None,
        "can_write": user.role in WRITE_ROLES,
    })
    return templates.TemplateResponse("new/purchase.html", ctx)


@router.get("/buy/{purchase_id}/edit", response_class=HTMLResponse)
def purchase_edit(purchase_id: int, request: Request, db: Session = Depends(get_db)):
    """«Поправить»: та же форма «Купили», заполненная этой покупкой (17.09)."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    old = _live_purchase(db, site, purchase_id) if site else None
    if old is None:
        return HTMLResponse("Покупка не найдена или уже поправлена", status_code=404)
    rows = svc.edit_rows(db, old)
    if not rows:
        return RedirectResponse(f"/new/nocheck?edit={old.id}", status_code=302)
    receipt = db.get(Receipt, old.receipt_id) if old.receipt_id else None
    ctx = _form_ctx(request, user, site, db, supplier=old.supplier, other=True, rows=rows, last_date=None,
                    tx_date=old.date, payment=old.payment, for_org=str(old.for_org_id) if old.for_org_id else "shared",
                    payer_id=old.paid_from_user_id, account_org_id=old.account_org_id, founder_id=old.founder_id,
                    paid_amount=(f"{float(old.paid_amount):g}" if old.payment == "part" else ""), note=old.note or "",
                    photo_receipt_id=receipt.id if receipt and receipt.file_path != "manual" else None,
                    dup=None, error=None, replaces=old)
    return templates.TemplateResponse("new/buy.html", ctx)


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


# ── записи старого входа одной карточкой (21.09) ─────────────────────────

def _legacy_key(v) -> tuple[str, int] | None:
    v = (v or "").strip() if isinstance(v, str) else ""
    if len(v) > 2 and v[0] in "rt" and v[1] == ":" and v[2:].isdigit():
        return v[0], int(v[2:])
    return None


@router.get("/record/{kind}/{item_id}", response_class=HTMLResponse)
def legacy_card(kind: str, item_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    rows = lg.txs(db, site.id, kind, item_id) if site else []
    if not rows:
        return HTMLResponse("Запись не найдена: её убрали или поправили. Откройте ленту Расходов заново.", status_code=404)
    ctx = _base_ctx(request, user, site, db, "expenses")
    c = lg.card(db, kind, item_id, rows)
    ctx.update({"c": c, "can_write": user.role in WRITE_ROLES,
                "supplier_debt": float(get_supplier_balance(db, c["supplier"].id)) if c["supplier"] else 0.0})
    return templates.TemplateResponse("new/record.html", ctx)


@router.get("/record/{kind}/{item_id}/edit", response_class=HTMLResponse)
def legacy_edit(kind: str, item_id: int, request: Request, db: Session = Depends(get_db)):
    """«Поправить» запись старого входа: с позициями — «Купили», без — «Расход без чека»."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = _site(user, db)
    rows = lg.txs(db, site.id, kind, item_id) if site else []
    if not rows:
        return HTMLResponse("Запись не найдена или уже поправлена", status_code=404)
    lines = lg.edit_rows(db, kind, item_id, rows)
    if not lines:
        return RedirectResponse(f"/new/nocheck?legacy={kind}:{item_id}", status_code=302)
    c = lg.card(db, kind, item_id, rows)
    receipt = db.get(Receipt, item_id) if kind == "r" else None
    ctx = _form_ctx(request, user, site, db, supplier=c["supplier"], other=True, rows=lines, last_date=None,
                    tx_date=c["date"], payment=c["payment"], for_org="shared",
                    payer_id=c["payer"].id if c["payer"] else None, account_org_id=c["account_org_id"], founder_id=None,
                    paid_amount=(f"{float(c['paid']):g}" if c["payment"] == "part" else ""), note=c["note"] or "",
                    photo_receipt_id=receipt.id if receipt and receipt.file_path != "manual" else None,
                    dup=None, error=None,
                    legacy={"key": f"{kind}:{item_id}", "date": c["date"], "total": c["total"]})
    return templates.TemplateResponse("new/buy.html", ctx)


@router.post("/record/{kind}/{item_id}/remove")
def legacy_remove(kind: str, item_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role not in WRITE_ROLES:
        return HTMLResponse("Нет прав", status_code=403)
    site = _site(user, db)
    rows = lg.txs(db, site.id, kind, item_id) if site else []
    if rows:
        lg.remove(db, rows, user, "убрано в новом входе")
        db.commit()
    return RedirectResponse("/new/expenses?removed=1", status_code=303)
