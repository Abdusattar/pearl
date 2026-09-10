"""Списать больше, чем лежит на складе, нельзя (10.09).

Реальный случай: Махабат списала на кухню 4 кг моркови, введя 4000 — граммы
в поле килограммов. Остаток ушёл в минус на 3 988 кг, и склад, вычищенный
пересчётом тем же утром, снова стал неверным.

Проверка стоит на сервере, а не только на форме: клиентская подсветка помогает
человеку, но не защищает данные.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import Organization, Product, WarehouseReceipt, WriteOff
from app.routers import warehouse


@pytest.fixture()
def org(db):
    o = Organization(name="__Склад-лимит__", type="садик")
    db.add(o)
    db.flush()
    return o


def _product(db, name="__Морковь-тест__", unit="кг"):
    p = Product(name=name, unit=unit, category="овощи")
    db.add(p)
    db.flush()
    return p


def _receipt(db, org, product, qty, cost="1000"):
    db.add(WarehouseReceipt(
        date=date.today() - timedelta(days=1), product_id=product.id,
        quantity=Decimal(qty), price_per_unit=Decimal(cost) / Decimal(qty),
        total_cost=Decimal(cost), organization_id=org.id,
    ))
    db.flush()


def _balance(db, org, product):
    recv = sum(Decimal(r.quantity) for r in db.query(WarehouseReceipt)
               .filter_by(product_id=product.id, organization_id=org.id)
               if r.deleted_at is None)
    woff = sum(Decimal(w.quantity) for w in db.query(WriteOff)
               .filter_by(product_id=product.id, organization_id=org.id)
               if w.deleted_at is None)
    return recv - woff


def test_balance_map_is_the_source_of_truth(db, org):
    """Остаток для проверки берётся тем же расчётом, что показан на форме."""
    p = _product(db)
    _receipt(db, org, p, "11.9")
    available = warehouse._get_balance_map(db, {org.id})[p.id]["balance"]
    assert available == pytest.approx(11.9)


def test_over_balance_is_rejected(db, org):
    """4000 при остатке 11,9 — та самая ошибка с морковью."""
    p = _product(db)
    _receipt(db, org, p, "11.9")
    available = warehouse._get_balance_map(db, {org.id})[p.id]["balance"]

    assert 4000 > available + 0.001, "запрос должен признаваться превышением"
    assert not (4 > available + 0.001), "нормальное списание проходить обязано"


def test_two_rows_of_one_product_are_summed(db, org):
    """Порознь каждая строка проходит, вместе — уводят склад в минус.

    Один товар легко попадает в форму дважды: повар взял утром и добавил
    в обед. Проверять построчно означало бы пропустить это."""
    p = _product(db)
    _receipt(db, org, p, "10")

    rows = [{"product_id": p.id, "qty": 6.0}, {"product_id": p.id, "qty": 6.0}]
    wanted = {}
    for item in rows:
        wanted[item["product_id"]] = wanted.get(item["product_id"], 0.0) + item["qty"]

    available = warehouse._get_balance_map(db, {org.id})[p.id]["balance"]
    assert wanted[p.id] == 12.0
    assert wanted[p.id] > available + 0.001, "сумма строк должна ловиться"


def test_exact_balance_passes(db, org):
    """Списать всё под ноль — законно, впритык это не ошибка."""
    p = _product(db)
    _receipt(db, org, p, "1.1")
    available = warehouse._get_balance_map(db, {org.id})[p.id]["balance"]
    assert not (1.1 > available + 0.001)


def test_writeoff_still_lowers_stock(db, org):
    """Защита не должна мешать обычному списанию."""
    p = _product(db)
    _receipt(db, org, p, "11.9")
    db.add(WriteOff(date=date.today(), product_id=p.id, quantity=Decimal("4"),
                    organization_id=org.id, reason="питание детей",
                    meal_type="Завтрак"))
    db.flush()
    assert _balance(db, org, p) == Decimal("7.9")
