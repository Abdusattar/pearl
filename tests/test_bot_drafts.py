"""Бот → черновик → проверка Махабат, шаг 1 (21.09, context/revision/11_bot_inbox.md).
Фото из чата — черновик; в деньги и склад ничего не пишется, пока Махабат не внесёт формой."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import KitchenSheet, Organization, Product, Receipt, User, WarehouseReceipt, WriteOff
from app.routers import new_buy as buy_router
from app.routers import new_kitchen as kitchen_router
from app.routers import new_today as today_router
from app.routers import new_expenses as exp_router
from app.services import bot as svc
from app.services import bot_group as grp
from app.services import drafts
from app.services import recognize as rz
from app.services.transition import ensure_categories

GROUP = -1001234567891


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень чр", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-чр", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    return sadik


@pytest.fixture()
def people(db, site, monkeypatch, tmp_path):
    m = User(name="Махабаттест Чр", role="staff", organization_id=site.id, tg_id=900000000201)
    n = User(name="Мунаратест Чр", role="manager", organization_id=site.id, tg_id=900000000202)
    db.add_all([m, n])
    db.flush()
    monkeypatch.setattr(svc, "site_for_bot", lambda db: site)
    monkeypatch.delenv(svc.TOKEN_ENV, raising=False)
    monkeypatch.setenv(svc.GROUP_ENV, str(GROUP))
    monkeypatch.setattr(svc, "download_file", lambda file_id: b"photo-" + file_id.encode())
    monkeypatch.setattr(drafts, "MEDIA_ROOT", tmp_path)
    monkeypatch.setattr(kitchen_router, "MEDIA_DIR", tmp_path / "kitchen")
    for r in (kitchen_router, today_router, buy_router, exp_router):
        monkeypatch.setattr(r, "get_current_user", lambda request, db: m)
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: site)
    return m, n


@pytest.fixture()
def carrot(db, site):
    cats = ensure_categories(db)
    p = Product(name="Морковь тест-чр", unit="кг", is_standard=True, category_id=cats["овощи и фрукты"].id)
    db.add(p)
    db.flush()
    db.add(WarehouseReceipt(date=date.today() - timedelta(days=5), product_id=p.id, quantity=50, price_per_unit=30,
                            total_cost=1500, organization_id=site.id))
    db.flush()
    return p


def _model(monkeypatch, answer):
    monkeypatch.setattr(grp, "ask_model", lambda prompt, image=None, mime="image/jpeg": answer)


def _upd(user, photo_id, message_id=1, caption=None):
    msg = {"message_id": message_id, "chat": {"id": GROUP, "type": "supergroup"}, "from": {"id": user.tg_id},
           "photo": [{"file_id": photo_id, "file_unique_id": "u-" + photo_id, "file_size": 10}]}
    if caption:
        msg["caption"] = caption
    return {"message": msg}


def test_kitchen_photo_from_chat_becomes_draft_not_a_writeoff(db, site, people, monkeypatch):
    m, n = people
    _model(monkeypatch, {"kind": "kitchen", "sure": True})
    reply = svc.handle_update(db, _upd(n, "k1"))
    assert "Черновик у Махабат" in reply
    r = db.query(Receipt).filter_by(organization_id=site.id, kind="kitchen").one()
    assert r.source == "chat" and r.created_by == n.id and r.ocr_status == "pending"
    assert db.query(WriteOff).filter_by(organization_id=site.id).count() == 0     # склад не тронут
    again = svc.handle_update(db, _upd(n, "k1", message_id=2))                    # то же фото ещё раз
    assert "уже присылали" in again and db.query(Receipt).filter_by(kind="kitchen", organization_id=site.id).count() == 1


def test_purchase_photo_draft_knows_supplier_and_match(db, site, people, monkeypatch):
    from app.models import Supplier
    m, n = people
    s = Supplier(name="Халиматест Чр", phone="0000")
    db.add(s)
    db.flush()
    _model(monkeypatch, {"kind": "purchase", "supplier": "Халиматест Чр", "amount": 4350, "date": date.today().isoformat()})
    svc.handle_update(db, _upd(m, "p1"))
    r = db.query(Receipt).filter_by(organization_id=site.id, kind="receipt").one()
    assert r.payload["supplier_id"] == s.id and r.payload["match"] == "в системе нет"


def test_salary_or_bank_photo_is_not_a_draft(db, site, people, monkeypatch):
    m, _ = people
    _model(monkeypatch, {"kind": "salary"})
    svc.handle_update(db, _upd(m, "z1"))
    assert db.query(Receipt).filter_by(organization_id=site.id).count() == 0


def test_service_photo_becomes_nocheck_draft_and_enters(client, db, site, people, monkeypatch):
    from app.models import Purchase
    m, n = people
    _model(monkeypatch, {"kind": "service", "supplier": "Такси тест-чр", "amount": 200, "date": date.today().isoformat()})
    svc.handle_update(db, _upd(n, "s1"))
    r = db.query(Receipt).filter_by(organization_id=site.id, kind="service").one()
    page = client.get(f"/new/nocheck?draft={r.id}")
    assert page.status_code == 200 and "Услуга из чата" in page.text and 'value="200"' in page.text and "Такси тест-чр" in page.text
    res = client.post("/new/nocheck", data={"amount": "200", "kind": "delivery", "what": "такси", "supplier_name": "Такси тест-чр",
                                            "for_org": "shared", "payment": "cash", "payer_id": str(m.id),
                                            "date": date.today().isoformat(), "draft_id": str(r.id)}, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"].startswith("/new/receipts?done=service")
    db.refresh(r)
    purchase = db.get(Purchase, r.result_id)
    assert r.ocr_status == "confirmed" and r.result_type == "purchase" and purchase.receipt_id is None   # остаётся «без чека»
    card = client.get(f"/new/buy/{purchase.id}")
    assert r.file_path in card.text                                                     # фото видно на карточке


def test_receipt_rows_recognized_once_before_opening(client, db, site, people, monkeypatch):
    from app.models import Supplier
    m, n = people
    s = Supplier(name="Халиматест2 Чр", phone="0000")
    db.add(s)
    db.flush()
    _model(monkeypatch, {"kind": "purchase", "supplier": "Халиматест2 Чр", "amount": 300})
    svc.handle_update(db, _upd(m, "p2"))
    r = db.query(Receipt).filter_by(organization_id=site.id, kind="receipt").one()
    calls = []

    def fake(db_, data, kind, site_id, supplier_id=None, mime="image/jpeg"):
        calls.append(kind)
        return {"rows": [{"product_id": None, "name": "Лук", "raw": "лук", "qty": 10, "unit": "кг", "price": 30,
                          "total": 300, "question": None, "notes": [], "is_new": True}], "amount": 300}
    monkeypatch.setattr(rz, "recognize", fake)
    drafts.receipt_rows(db, r, site.id, s.id)                       # как prepare_pending фоном
    page = client.get(f"/new/buy?receipt={r.id}&supplier={s.id}")
    assert page.status_code == 200 and "Лук" in page.text and calls == ["receipt"]   # при открытии модель не звали


def test_makhabat_checks_edits_and_enters_kitchen_draft(client, db, site, people, carrot, monkeypatch):
    m, n = people
    _model(monkeypatch, {"kind": "kitchen", "sure": True})
    svc.handle_update(db, _upd(n, "k2"))
    r = db.query(Receipt).filter_by(organization_id=site.id, kind="kitchen").one()
    calls = []

    def fake_recognize(db_, data, kind, site_id, supplier_id=None, mime="image/jpeg"):
        calls.append(kind)
        return {"rows": [{"product_id": carrot.id, "name": carrot.name, "raw": "морк", "qty": 3.5, "unit": "кг",
                          "question": {"text": "«морк» — это Морковь?"}, "notes": []}]}
    monkeypatch.setattr(rz, "recognize", fake_recognize)
    page = client.get(f"/new/kitchen?draft={r.id}")
    assert page.status_code == 200 and "Из чата" in page.text and "день на листе не прочитан" in page.text
    assert carrot.name in page.text and "это Морковь?" in page.text
    client.get(f"/new/kitchen?draft={r.id}")
    assert calls == ["kitchen"]                          # распознаётся один раз, дальше из черновика
    d = kitchen_router.svc.missing_days(db, site.id)[0]
    form = {"date": d.isoformat(), "draft_id": str(r.id), "children": "",
            "item_product_id": [str(carrot.id)], "item_name": [carrot.name], "item_qty": ["4"], "item_unit": ["кг"]}
    res = client.post("/new/kitchen", data=form, follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"].startswith("/new/receipts?done=kitchen")
    db.refresh(r)
    sheet = db.query(KitchenSheet).filter_by(site_org_id=site.id, date=d).one()
    assert r.ocr_status == "confirmed" and r.result_type == "kitchen_sheet" and r.result_id == sheet.id
    assert sheet.photo_path == r.file_path and r.decided_by == m.id
    w = db.query(WriteOff).filter_by(sheet_id=sheet.id).one()
    assert float(w.quantity) == 4                         # внесено то, что поправила Махабат, а не 3,5 с фото
    assert client.get(f"/new/kitchen?draft={r.id}", follow_redirects=False).headers["location"] == "/new/receipts"


def test_draft_over_existing_sheet_asks_before_replace(client, db, site, people, carrot, monkeypatch):
    m, n = people
    d = kitchen_router.svc.missing_days(db, site.id)[0]
    db.add(KitchenSheet(site_org_id=site.id, date=d, created_by=m.id))
    db.flush()
    _model(monkeypatch, {"kind": "kitchen", "sure": True})
    svc.handle_update(db, _upd(n, "k3"))
    r = db.query(Receipt).filter_by(organization_id=site.id, kind="kitchen").one()
    monkeypatch.setattr(rz, "recognize", lambda *a, **k: {"rows": []})
    form = {"date": d.isoformat(), "draft_id": str(r.id), "children": "",
            "item_product_id": [str(carrot.id)], "item_name": [carrot.name], "item_qty": ["2"], "item_unit": ["кг"]}
    res = client.post("/new/kitchen", data=form, follow_redirects=False)
    assert res.status_code == 200 and "лист уже внесён" in res.text and 'name="replace_ok"' in res.text
    res = client.post("/new/kitchen", data={**form, "replace_ok": "1"}, follow_redirects=False)
    assert res.status_code == 303


def test_skip_kitchen_draft_keeps_reason_and_list_shows_drafts(client, db, site, people, monkeypatch):
    m, n = people
    _model(monkeypatch, {"kind": "kitchen", "sure": True})
    svc.handle_update(db, _upd(n, "k4"))
    r = db.query(Receipt).filter_by(organization_id=site.id, kind="kitchen").one()
    page = client.get("/new/receipts")
    assert page.status_code == 200 and "Лист кухни" in page.text and f"/new/kitchen?draft={r.id}" in page.text
    res = client.post(f"/new/receipt/{r.id}/skip", data={"reason": "дубль", "back": "/new/receipts"}, follow_redirects=False)
    assert res.headers["location"].startswith("/new/receipts")
    db.refresh(r)
    assert r.ocr_status == "rejected" and r.reject_reason == "дубль" and r.decided_by == m.id


@pytest.fixture(autouse=True)
def _sheet_required_mode(monkeypatch):
    """Эти тесты — про режим «лист кухни обязателен» (до 23.09 он был единственным)."""
    from app.services import rules as _rules
    monkeypatch.setattr(_rules, "kitchen_sheet_required", lambda db: True)
