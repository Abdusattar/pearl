"""Защита единиц на приходе (решение владельца 15.09, старая система).

На проде яйцо приходило «по 12» и «по 420», укроп «по 12» и «по 180»: человек
считал в лотках и пучках, а карточка — в штуках. Единицу теперь не вводят
(она из карточки), а цена за единицу сверяется с обычной: расхождение больше
чем в 2 раза возвращает форму с вопросом, ничего не записывая.
"""
from datetime import date, timedelta

import pytest

from app.models import Organization, Product, Supplier, Transaction, User, WarehouseReceipt
from app.routers import expenses as expenses_router
from app.routers.expenses import resolve_item_product
from app.services.price_check import price_anomaly_hint, usual_price


@pytest.fixture()
def egg(db):
    p = Product(name="Яйцо тест-ед", unit="шт", is_standard=True)
    db.add(p)
    db.flush()
    return p


@pytest.fixture()
def org(db):
    o = Organization(name="Садик тест-ед", type="kindergarten")
    db.add(o)
    db.flush()
    return o


def _receipt(db, product, org, price, days_ago=1, qty=30):
    r = WarehouseReceipt(
        date=date.today() - timedelta(days=days_ago), product_id=product.id,
        quantity=qty, price_per_unit=price, total_cost=qty * price,
        organization_id=org.id,
    )
    db.add(r)
    db.flush()
    return r


# ── цена ────────────────────────────────────────────────────────────────

def test_no_history_no_question(db, egg):
    assert price_anomaly_hint(db, egg, 420) is None


def test_median_not_mean(db, egg, org):
    """В истории уже лежит один «лоток по 420» — медиана его не замечает."""
    for p in (12, 12, 13, 420):
        _receipt(db, egg, org, p)
    assert usual_price(db, egg.id) == 12.5
    assert price_anomaly_hint(db, egg, 14) is None
    hint = price_anomaly_hint(db, egg, 420)
    assert hint == "Яйцо тест-ед по 420, обычно 12,5 за шт. Так?"


def test_too_cheap_is_also_a_question(db, egg, org):
    """Мука по 2500 за «кг» — это мешок; но и 0,05 вместо 50 — это граммы."""
    for p in (50, 52):
        _receipt(db, egg, org, p)
    assert price_anomaly_hint(db, egg, 0.05) is not None
    assert price_anomaly_hint(db, egg, 99) is None       # меньше ×2 от 51 — не спрашиваем
    assert price_anomaly_hint(db, egg, 102) is not None  # ровно ×2 — спрашиваем


def test_old_and_deleted_receipts_ignored(db, egg, org):
    _receipt(db, egg, org, 12, days_ago=90)
    dead = _receipt(db, egg, org, 12, days_ago=2)
    dead.deleted_at = date.today()
    db.flush()
    assert usual_price(db, egg.id) is None


def test_own_receipt_excluded_on_edit(db, egg, org):
    """При правке чека его же старый приход не считается «обычной ценой»."""
    tx = Transaction(organization_id=org.id, type="expense", amount=1260, date=date.today())
    db.add(tx)
    db.flush()
    own = _receipt(db, egg, org, 420)
    own.transaction_id = tx.id
    _receipt(db, egg, org, 12)
    db.flush()
    assert usual_price(db, egg.id) == 216          # без исключения — медиана из двух
    assert usual_price(db, egg.id, exclude_tx_ids=[tx.id]) == 12


# ── единица ─────────────────────────────────────────────────────────────

def test_existing_product_unit_comes_from_card(db, egg):
    product, err = resolve_item_product(db, "Яйцо тест-ед", str(egg.id), "")
    assert err is None and product.id == egg.id and product.unit == "шт"


def test_existing_product_other_unit_is_error_not_update(db, egg):
    product, err = resolve_item_product(db, "Яйцо тест-ед", "", "уп")
    assert product is None
    assert "считается в шт" in err
    assert db.get(Product, egg.id).unit == "шт"


def test_new_product_needs_unit(db):
    product, err = resolve_item_product(db, "Чёрная смородина тест-ед", "", "")
    assert product is None and "новый товар" in err
    product, err = resolve_item_product(db, "Чёрная смородина тест-ед", "", "кг")
    assert err is None and product.unit == "кг" and product.is_standard is False


# ── форма закупа целиком ────────────────────────────────────────────────

@pytest.fixture()
def owner(db, org, monkeypatch):
    u = User(name="Тест владелец", role="owner", organization_id=org.id)
    db.add(u)
    db.flush()
    monkeypatch.setattr(expenses_router, "get_current_user", lambda request, db: u)
    return u


@pytest.fixture()
def supplier(db):
    s = Supplier(name="Рынок тест-ед", phone="0700000000")
    db.add(s)
    db.flush()
    return s


def _post_add(client, org, supplier, egg, price, price_ok="", unit=""):
    return client.post("/expenses/add", data={
        "org_id": org.id, "amount": "1260", "paid_full": "1",
        "date": date.today().isoformat(), "supplier_id": str(supplier.id),
        "item_name": ["Яйцо тест-ед"], "item_product_id": [str(egg.id)],
        "item_unit": [unit], "item_qty": ["3"], "item_unit_price": [str(price)],
        "item_price_ok": [price_ok],
    }, follow_redirects=False)


def test_add_asks_then_writes(client, db, org, supplier, egg, owner):
    for p in (12, 12, 13):
        _receipt(db, egg, org, p)
    before = db.query(Transaction).count()

    r = _post_add(client, org, supplier, egg, 420)
    assert r.status_code == 200
    html = r.text
    assert "обычно 12 за шт. Так?" in html
    assert "да, так" in html
    assert 'value="420.0"' in html or 'value="420"' in html     # введённое не потеряно
    assert db.query(Transaction).count() == before
    # по тестовому товару, не по всей базе: на копии прода яйцо по 420 уже есть
    assert db.query(WarehouseReceipt).filter_by(product_id=egg.id, price_per_unit=420).count() == 0

    r = _post_add(client, org, supplier, egg, 420, price_ok="1")
    assert r.status_code == 303
    assert db.query(Transaction).count() == before + 1
    assert db.query(WarehouseReceipt).filter_by(product_id=egg.id, price_per_unit=420).count() == 1


def test_add_rejects_foreign_unit(client, db, org, supplier, egg, owner):
    before = db.query(Transaction).count()
    r = _post_add(client, org, supplier, egg, 420, unit="уп")
    assert r.status_code == 200
    assert "считается в шт" in r.text
    assert db.query(Transaction).count() == before
    assert db.get(Product, egg.id).unit == "шт"
