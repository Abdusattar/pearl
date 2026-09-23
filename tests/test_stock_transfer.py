"""Передача продуктов в другой садик (23.09: Сокулук → Кожомкул, 12 л молока).
Со склада уходит, но не расход кухни; стоимость по цене закупки; в Обзоре — из нашей еды."""
from datetime import date
from decimal import Decimal

import pytest

from app.models import Organization, Product, Transaction, User, WarehouseReceipt
from app.routers import new_stock as stock_router
from app.services import stock
from app.services.unit_economics import monthly_food_cost
from app.services.warehouse import get_balance_map


@pytest.fixture()
def world(db, monkeypatch):
    root = Organization(name="Корень тест-пд", type="root")
    db.add(root)
    db.flush()
    group = Organization(name="Садики тест-пд", type="kindergarten", parent_id=root.id)
    db.add(group)
    db.flush()
    home = Organization(name="Садик дом тест-пд", type="kindergarten", parent_id=group.id)
    other = Organization(name="Садик другой тест-пд", type="kindergarten", parent_id=group.id)
    db.add_all([home, other])
    db.flush()
    u = User(name="Учётчик тест-пд", role="staff", organization_id=home.id)
    milk = Product(name="Молоко тест-пд", unit="л")
    db.add_all([u, milk])
    db.flush()
    t = Transaction(organization_id=home.id, type="expense", amount=4000, date=date.today())
    db.add(t)
    db.flush()
    db.add(WarehouseReceipt(date=date.today(), product_id=milk.id, quantity=50, price_per_unit=80, total_cost=4000,
                            organization_id=home.id, transaction_id=t.id))
    db.flush()
    monkeypatch.setattr(stock_router, "get_current_user", lambda request, db: u)
    monkeypatch.setattr(stock_router, "_site", lambda user, db: home)
    return {"home": home, "other": other, "group": group, "milk": milk, "u": u}


def test_targets_exclude_own_site_and_group_folders(db, world):
    ids = {o.id for o in stock.transfer_targets(db, world["home"].id)}
    assert world["other"].id in ids and world["home"].id not in ids and world["group"].id not in ids


def test_transfer_leaves_stock_not_food_cost_and_priced_by_purchase(client, db, world):
    r = client.post("/new/stock/transfer", data={"to_org_id": str(world["other"].id), "date": date.today().isoformat(),
                                                 "product_id": str(world["milk"].id), "qty": "12"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/new/stock?saved=transfer"
    assert get_balance_map(db, {world["home"].id})[world["milk"].id]["balance"] == pytest.approx(38)
    assert stock.transfers_value(db, to_id=world["other"].id) == Decimal("960")    # 12 × 80
    assert monthly_food_cost(db, world["home"].id, date.today().replace(day=1)) == 0
    card = stock.product_card(db, world["home"].id, world["milk"])
    assert f"Передали: {world['other'].name}" in [m["title"] for m in card["moves"]]


def test_transfer_needs_target(db, world):
    with pytest.raises(ValueError):
        stock.transfer_out(db, user=world["u"], site_id=world["home"].id, to_org_id=world["home"].id,
                           items=[(world["milk"].id, Decimal("1"))])
