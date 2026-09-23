"""Черновики из чата и с фото (шаг 1 модуля 11_bot_inbox.md, 21.09).

Всё, что прислали в чат «Жемчужина» или боту в личку, — строка `receipts`
с видом (чек, лист кухни). Черновик ничего не меняет в деньгах и складе.
Махабат открывает его той же формой, что вносит руками (Купили, Лист кухни),
уже заполненной, проверяет, правит и вносит — или «Не вносить» с причиной.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Receipt, User
from app.services.ocr import compute_hash
from app.services.purchases import audit

MEDIA_ROOT = Path(__file__).parent.parent.parent / "media"
RECEIPT, KITCHEN, SERVICE = "receipt", "kitchen", "service"
# 23.09: остаток склада и передача в другой садик — из текста или фото, подтверждает Махабат
COUNT, TRANSFER = "count", "transfer"
OPEN = ("pending", "processed")          # ждёт проверки
KIND_FROM_BOT = {"purchase": RECEIPT, "kitchen": KITCHEN, "service": SERVICE, "count": COUNT}   # чек товара, лист кухни, услуга


def find_same(db: Session, file_hash: str | None, file_unique_id: str | None) -> Receipt | None:
    """То же фото уже лежит черновиком или чеком (по отпечатку файла или id снимка в Telegram)."""
    if file_hash:
        r = db.query(Receipt).filter(Receipt.file_hash == file_hash).first()
        if r is not None:
            return r
    if file_unique_id:
        return (db.query(Receipt).filter(Receipt.payload["file_unique_id"].astext == file_unique_id)
                .order_by(Receipt.id).first())
    return None


def _json(info: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in info.items()}


def create(db: Session, *, site_org_id: int, author: User | None, data: bytes, kind: str, source: str,
           info: dict | None = None, file_unique_id: str | None = None, ext: str = ".jpg") -> Receipt:
    """Сохранить фото и завести черновик. Дубль проверяет вызывающий (find_same)."""
    h = compute_hash(data)
    month = datetime.now().strftime("%Y-%m")
    sub = "kitchen/inbox" if kind == KITCHEN else ("stock/inbox" if kind in (COUNT, TRANSFER) else "receipts")
    folder = MEDIA_ROOT / sub / month
    folder.mkdir(parents=True, exist_ok=True)
    fname = f"{h[:12]}{ext}"
    (folder / fname).write_bytes(data)
    payload = _json(info or {})
    if file_unique_id:
        payload["file_unique_id"] = file_unique_id
    r = Receipt(organization_id=site_org_id, file_path=f"{sub}/{month}/{fname}", file_hash=h, ocr_status="pending",
                created_by=author.id if author else None, kind=kind, source=source, payload=payload)
    db.add(r)
    db.flush()
    audit(db, "receipt", r.id, "insert", author.id if author else None, {"from": source, "kind": kind})
    return r


def create_text(db: Session, *, site_org_id: int, author: User | None, text: str, kind: str, source: str,
                info: dict | None = None) -> Receipt:
    """Черновик из текста (остаток или передача строкой в чат): текст сохраняем файлом
    сразу — человек может удалить сообщение, а Telegram боту об этом не скажет."""
    return create(db, site_org_id=site_org_id, author=author, data=text.encode("utf-8"), kind=kind, source=source,
                  info={**(info or {}), "text": text[:4000]}, ext=".txt")


def is_text(r: Receipt) -> bool:
    return (r.file_path or "").endswith(".txt")


def open_draft(db: Session, site_org_ids: set[int], draft_id: int | None, kind: str | None = None) -> Receipt | None:
    r = db.get(Receipt, draft_id) if draft_id else None
    if r is None or r.file_path == "manual" or r.ocr_status not in OPEN or r.organization_id not in site_org_ids:
        return None
    if kind is not None and (r.kind or RECEIPT) != kind:
        return None
    return r


def done(db: Session, r: Receipt, *, user: User, result_type: str, result_id: int) -> None:
    r.ocr_status = "confirmed"
    r.result_type, r.result_id = result_type, result_id
    r.decided_by, r.decided_at = user.id, datetime.now()
    r.confirmed_by, r.confirmed_at = user.id, datetime.now()


def reject(db: Session, r: Receipt, *, user: User, reason: str) -> None:
    r.ocr_status = "rejected"
    r.reject_reason = reason
    r.decided_by, r.decided_at = user.id, datetime.now()
    audit(db, "receipt", r.id, "update", user.id, {"ocr_status": "rejected", "reason": reason})


def title(r: Receipt) -> dict:
    """Одна фраза «что это» для списка: {"t", "s", "warn"}."""
    p = r.payload or {}
    money = lambda v: f"{v:,.0f}".replace(",", " ") if isinstance(v, (int, float)) else None
    d = p.get("date")
    try:
        d = date.fromisoformat(d) if isinstance(d, str) and len(d) == 10 else None
    except ValueError:
        d = None
    from app.services.stock import _day
    if r.kind in (COUNT, TRANSFER):
        n = len(p.get("rows") or [])
        head = "Остаток склада" if r.kind == COUNT else "Передали в другой садик"
        return {"t": head + (f", {n} строк" if n else ""), "s": "из текста в чате" if is_text(r) else "лист с фото",
                "warn": False}
    if (r.kind or RECEIPT) == KITCHEN:
        rows = p.get("rows")
        n = len(rows or [])
        word = "строка" if n % 10 == 1 and n % 100 != 11 else ("строки" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "строк")
        t = "Лист кухни" + (f", {n} {word}" if n else "")
        return {"t": t, "s": f"за {_day(d)}" if d else "день на листе не прочитан, выберете при внесении",
                "warn": d is None}
    if (r.kind or RECEIPT) == SERVICE:
        parts = ["Услуга: " + (p.get("supplier_name") or p.get("supplier") or "кому — не прочитано")]
        if d:
            parts.append(_day(d))
        if money(p.get("amount")):
            parts.append(money(p.get("amount")))
        return {"t": ", ".join(parts), "s": "расход без чека, не на склад", "warn": False}
    parts = [p.get("supplier_name") or p.get("supplier") or "Чек"]
    if d:
        parts.append(_day(d))
    if money(p.get("amount")):
        parts.append(money(p.get("amount")))
    match = p.get("match")
    return {"t": ", ".join(parts), "s": match or ("фото чека" if not p else "в системе нет"),
            "warn": bool(match and match.startswith("похоже"))}


def kitchen_rows(db: Session, r: Receipt, site_org_id: int) -> list[dict]:
    """Строки листа кухни с фото: распознаются один раз и запоминаются в черновике.
    Бросает исключение, если модель недоступна — вызывающий решает, что показать."""
    p = dict(r.payload or {})
    if p.get("rows") is not None:
        return p["rows"]
    from app.services import recognize as rz
    data = (MEDIA_ROOT / r.file_path).read_bytes()
    out = rz.recognize(db, data, rz.KITCHEN, site_org_id,
                       mime="image/png" if r.file_path.lower().endswith(".png") else "image/jpeg")
    rows = [{"product_id": x["product_id"], "name": x["name"] or x["raw"], "qty": x["qty"],
             "unit": x["unit"] if x["product_id"] else "",
             "question": (x.get("question") or {}).get("text") or "; ".join(x.get("notes") or []) or None}
            for x in out["rows"]]
    p["rows"] = rows
    r.payload = p
    return rows


def receipt_rows(db: Session, r: Receipt, site_org_id: int, supplier_id: int) -> dict:
    """Строки чека с фото под поставщика: распознаются один раз и запоминаются.
    Возвращает ответ распознавания ({"rows", "amount"}) в виде, который ждёт «Купили»."""
    import json
    p = dict(r.payload or {})
    cached = p.get("receipt_rec")
    if cached is not None and cached.get("supplier_id") == supplier_id:
        return cached
    from app.services import recognize as rz
    data = (MEDIA_ROOT / r.file_path).read_bytes()
    out = rz.recognize(db, data, rz.RECEIPT, site_org_id, supplier_id,
                       mime="image/png" if r.file_path.lower().endswith(".png") else "image/jpeg")
    rec = json.loads(json.dumps({"rows": out["rows"], "amount": out.get("amount")}, default=float))
    rec["supplier_id"] = supplier_id
    p["receipt_rec"] = rec
    r.payload = p
    return rec


def stock_rows(db: Session, r: Receipt, site_org_id: int) -> list[dict]:
    """Строки остатка/передачи: распознаются один раз и запоминаются в черновике.
    Товар — из каталога склада, количество — в единице карточки (граммы → кг и т.п.)."""
    p = dict(r.payload or {})
    if p.get("rows") is not None:
        return p["rows"]
    from app.services import recognize as rz
    if is_text(r):
        text = p.get("text") or (MEDIA_ROOT / r.file_path).read_text(encoding="utf-8")
        out = rz.recognize_text(db, text, rz.COUNT if r.kind == COUNT else rz.TRANSFER, site_org_id)
    else:
        data = (MEDIA_ROOT / r.file_path).read_bytes()
        out = rz.recognize(db, data, rz.COUNT, site_org_id,
                           mime="image/png" if r.file_path.lower().endswith(".png") else "image/jpeg")
    rows = [{"product_id": x["product_id"], "name": x["name"] or x["raw"], "raw": x["raw"], "qty": x["qty"],
             "unit": x["unit"] if x["product_id"] else "", "minor": x.get("minor", False),
             "note": (x.get("question") or {}).get("text") or "; ".join(x.get("notes") or []) or None}
            for x in out["rows"]]
    p["rows"] = rows
    r.payload = p
    return rows


def prepare_pending(limit: int = 5) -> int:
    """Разобрать строки свежих черновиков сразу, как фото пришло (фоном после ответа
    Telegram и раз в минуту): лист кухни — всегда, чек — если поставщик узнан.
    Махабат открывает уже готовый черновик, без ожидания."""
    from app.database import SessionLocal
    db = SessionLocal()
    done_n = 0
    try:
        todo = (db.query(Receipt).filter(Receipt.kind.in_((KITCHEN, RECEIPT, COUNT, TRANSFER)), Receipt.ocr_status.in_(OPEN),
                                         Receipt.source.isnot(None))
                .order_by(Receipt.id).all())
        need = [x for x in todo if (x.kind in (KITCHEN, COUNT, TRANSFER) and (x.payload or {}).get("rows") is None)
                or (x.kind == RECEIPT and (x.payload or {}).get("supplier_id") and (x.payload or {}).get("receipt_rec") is None)]
        for r in need[:limit]:
            try:
                if r.kind == KITCHEN:
                    kitchen_rows(db, r, r.organization_id)
                elif r.kind in (COUNT, TRANSFER):
                    stock_rows(db, r, r.organization_id)
                else:
                    receipt_rows(db, r, r.organization_id, int(r.payload["supplier_id"]))
                db.commit()
                done_n += 1
            except Exception:  # noqa: BLE001 — не вышло сейчас: разберётся при открытии
                db.rollback()
    finally:
        db.close()
    return done_n
