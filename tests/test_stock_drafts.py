"""Остаток и передача текстом в чат → черновик → Махабат подтверждает одной кнопкой (23.09).
Живой пример — строка Махабат «остаток склада на сегодня молоко 80л, …»."""
from datetime import date

import pytest

from app.models import MealCount, Organization, Product, Receipt, StockCount, User, WriteOff
from app.routers import new_stock as stock_router
from app.services import bot, drafts, recognize

LINE = ("остаток склада на сегодня   молоко 80л, кефир 30л, рис 30кг, сахар 1230кг, "
        "черный перец 1 кг, соль 5,250")


@pytest.fixture()
def world(db, monkeypatch):
    root = Organization(name="Корень тест-чр", type="root")
    db.add(root)
    db.flush()
    group = Organization(name="Садики тест-чр", type="kindergarten", parent_id=root.id)
    db.add(group)
    db.flush()
    home = Organization(name="Садик тест-чр", type="kindergarten", parent_id=group.id)
    other = Organization(name="Кожомкул тест-чр", type="kindergarten", parent_id=group.id)
    db.add_all([home, other])
    db.flush()
    u = User(name="Учётчик тест-чр", role="staff", organization_id=home.id, tg_id=556001)
    milk, rice, pepper = Product(name="Молоко тест-чр", unit="л"), Product(name="Рис тест-чр", unit="кг"), \
        Product(name="Перец тест-чр", unit="г")
    db.add_all([u, milk, rice, pepper])
    db.flush()
    monkeypatch.setattr(bot, "site_for_bot", lambda db: home)
    monkeypatch.delenv(bot.TOKEN_ENV, raising=False)
    monkeypatch.setenv(bot.GROUP_ENV, "-100556")
    monkeypatch.setattr(stock_router, "get_current_user", lambda request, db: u)
    monkeypatch.setattr(stock_router, "_site", lambda user, db: home)

    def fake(db, text, kind, site_org_id, model=None):
        if kind == recognize.TRANSFER:
            return {"rows": [{"raw": "молоко", "product_id": milk.id, "name": milk.name, "unit": "л", "qty": 12.0}]}
        return {"rows": [
            {"raw": "молоко", "product_id": milk.id, "name": milk.name, "unit": "л", "qty": 80.0},
            {"raw": "рис", "product_id": rice.id, "name": rice.name, "unit": "кг", "qty": 30.0},
            {"raw": "черный перец", "product_id": pepper.id, "name": pepper.name, "unit": "г", "qty": 1000.0,
             "notes": ["на листе 1 кг, перевели в г"]},
            {"raw": "соль", "product_id": None, "name": "соль", "unit": "", "qty": 5.25},
        ]}
    monkeypatch.setattr(recognize, "recognize_text", fake)
    return {"home": home, "other": other, "u": u, "milk": milk, "rice": rice, "pepper": pepper}


def _say(db, text, mid=1):
    return bot.handle_update(db, {"message": {"message_id": mid, "chat": {"id": -100556, "type": "supergroup"},
                                              "from": {"id": 556001}, "text": text}})


def test_kind_detection():
    assert bot.stock_text_kind(LINE) == "count"
    assert bot.stock_text_kind("из остатка 12 л молока переданы в второй филиал Кожомкул") == "transfer"
    assert bot.stock_text_kind("остаток на счёте 20 000") is None     # одно число — не остаток склада
    assert bot.stock_text_kind("школа 328, садик 97, персонал 43") is None


def test_count_text_becomes_draft_and_one_button_applies(client, db, world):
    reply = _say(db, LINE)
    rc = db.query(Receipt).filter_by(kind="count", created_by=world["u"].id).one()
    assert f"/new/stock/count?draft={rc.id}" in reply and "3 продуктов" in reply
    assert rc.file_path.endswith(".txt") and (drafts.MEDIA_ROOT / rc.file_path).read_text(encoding="utf-8") == LINE
    page = client.get(f"/new/stock/count?draft={rc.id}")
    assert page.status_code == 200 and "По листу, 3" in page.text and 'value="80"' in page.text and "соль" in page.text
    r = client.post("/new/stock/count", data={"draft_id": str(rc.id), "cat": "",
                                              "product_id": [str(world["milk"].id), str(world["rice"].id)],
                                              "actual": ["80", "30"]}, follow_redirects=False)
    assert r.status_code == 303
    db.refresh(rc)
    assert rc.ocr_status == "confirmed" and rc.result_type == "stock_count"


def test_transfer_text_becomes_transfer_draft(client, db, world):
    reply = _say(db, "из остатка 12 л молока переданы в второй филиал Кожомкул", mid=2)
    rc = db.query(Receipt).filter_by(kind="transfer", created_by=world["u"].id).one()
    assert f"/new/stock/transfer?draft={rc.id}" in reply
    page = client.get(f"/new/stock/transfer?draft={rc.id}")
    assert page.status_code == 200 and 'value="12"' in page.text


def test_confirmed_words_are_learned(db, world):
    from app.models import ProductAlias
    rc = drafts.create_text(db, site_org_id=world["home"].id, author=world["u"], text="томатище-чр 3,600", kind="count",
                            source="chat")
    rc.payload = {**rc.payload, "rows": [{"product_id": world["rice"].id, "raw": "томатище-чр 3,600"},
                                         {"product_id": world["milk"].id, "raw": "Уточнения Махабат: масло для выпечки да"}]}
    assert drafts.learn_words(db, rc, {world["rice"].id, world["milk"].id}) == 1
    assert db.query(ProductAlias).filter_by(raw_text="томатище-чр", product_id=world["rice"].id).count() == 1


def test_short_clarification_goes_into_same_draft(db, world):
    _say(db, LINE, mid=5)
    reply = _say(db, "80л до после -12л после  остаток 68л", mid=6)
    assert reply.startswith("Добавил к остатку")
    rows = db.query(Receipt).filter_by(kind="count", created_by=world["u"].id).all()
    assert len(rows) == 1 and "68л" in rows[0].payload["text"]


def test_no_link_when_bot_understood_nothing(db, world, monkeypatch):
    monkeypatch.setattr(recognize, "recognize_text", lambda db, text, kind, s, model=None: {"rows": [
        {"raw": "до", "product_id": None, "name": "до", "qty": 80.0}]})
    assert _say(db, "остаток: 80 до, 12 после, 68 итого", mid=7) is None or "draft=" not in (_say(db, "x", mid=8) or "")
    assert db.query(Receipt).filter_by(kind="count", created_by=world["u"].id, ocr_status="pending").count() == 0
