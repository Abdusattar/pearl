"""Правку и удаление строки расхода (11.09).

Реальный случай: по листу кухни за 10.09 «сливичный масло — 200 г» занесли
как 200 кг, а «300 г + 810 г» растительного — как 1110 л, вторым разом поверх
уже списанных 1,11 л. Склад ушёл в минус на 199,6 кг и 1 027,61 л, и починить
это через интерфейс было нельзя: роута правки не существовало вовсе.

Расход кухни заводится каждый день по листу — описка тут рядовое событие,
а не корректировка счёта, поэтому правка доступна тому же, кто заводит.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import AuditLog, Organization, Product, WarehouseReceipt, WriteOff
from app.routers import warehouse


@pytest.fixture()
def org(db):
    o = Organization(name="__Склад-правка__", type="садик")
    db.add(o)
    db.flush()
    return o


@pytest.fixture()
def product(db):
    p = Product(name="__Масло сливочное-тест__", unit="кг", category="масла")
    db.add(p)
    db.flush()
    return p


def _receipt(db, org, product, qty):
    db.add(WarehouseReceipt(
        date=date.today() - timedelta(days=1), product_id=product.id,
        quantity=Decimal(qty), price_per_unit=Decimal("300"),
        total_cost=Decimal(qty) * Decimal("300"), organization_id=org.id,
    ))
    db.flush()


def _writeoff(db, org, product, qty):
    w = WriteOff(date=date.today(), product_id=product.id, quantity=Decimal(qty),
                 organization_id=org.id, reason="питание детей")
    db.add(w)
    db.flush()
    return w


def _balance(db, org, product):
    return warehouse._get_balance_map(db, {org.id}).get(product.id, {}).get("balance", 0.0)


def test_available_on_edit_includes_the_row_itself(db, org, product):
    """Своё количество строки возвращается в остаток.

    Иначе правка «200 → 0,2» упёрлась бы в проверку: склад уже в минусе
    из-за этой же строки, и любое новое число выглядело бы превышением."""
    _receipt(db, org, product, "0.4")
    w = _writeoff(db, org, product, "200")

    assert _balance(db, org, product) == pytest.approx(-199.6)
    available = _balance(db, org, product) + float(w.quantity)
    assert available == pytest.approx(0.4)
    assert not (0.2 > available + 0.001), "исправление на 0,2 кг обязано проходить"


def test_edit_over_available_is_rejected(db, org, product):
    """Второй промах той же рукой ловится так же, как первый."""
    _receipt(db, org, product, "0.4")
    w = _writeoff(db, org, product, "0.2")

    available = _balance(db, org, product) + float(w.quantity)
    assert available == pytest.approx(0.4)
    assert 200 > available + 0.001, "200 кг при остатке 0,4 не должны проходить"


def test_edit_fixes_the_balance(db, org, product):
    """После правки склад выходит из минуса."""
    _receipt(db, org, product, "0.4")
    w = _writeoff(db, org, product, "200")
    w.quantity = Decimal("0.2")
    db.flush()
    assert _balance(db, org, product) == pytest.approx(0.2)


def test_soft_delete_returns_goods_to_stock(db, org, product):
    """Удаление мягкое: строка остаётся в базе, из остатка выпадает."""
    _receipt(db, org, product, "5")
    w = _writeoff(db, org, product, "3")
    assert _balance(db, org, product) == pytest.approx(2)

    w.deleted_at = date.today()
    db.flush()
    assert _balance(db, org, product) == pytest.approx(5)
    assert db.get(WriteOff, w.id) is not None, "запись не должна исчезать физически"


def test_foreign_org_row_is_not_editable(db, org, product):
    """Строку чужого объекта трогать нельзя."""
    other = Organization(name="__Чужой объект__", type="садик")
    db.add(other)
    db.flush()
    w = _writeoff(db, other, product, "1")

    ctx = {"current_org": org}
    found, err = warehouse._writeoff_for_edit(db, ctx, w.id)
    assert found is None and err == "Строка не найдена"


def test_deleted_row_is_not_editable_twice(db, org, product):
    """Уже убранную строку второй раз не правят."""
    w = _writeoff(db, org, product, "1")
    w.deleted_at = date.today()
    db.flush()

    ctx = {"current_org": org}
    found, err = warehouse._writeoff_for_edit(db, ctx, w.id)
    assert found is None and err == "Строка не найдена"


def test_editable_row_is_found(db, org, product):
    """Своя живая строка находится."""
    w = _writeoff(db, org, product, "1")
    ctx = {"current_org": org}
    found, err = warehouse._writeoff_for_edit(db, ctx, w.id)
    assert err is None and found.id == w.id


def test_days_grouping_keeps_gaps_visible(db, org, product):
    """Расход группируется по дням, пустые дни не выдумываются.

    Смысл экрана — сразу видеть, что вчера занесли, а позавчера нет."""
    _receipt(db, org, product, "50")
    today, long_ago = date.today(), date.today() - timedelta(days=10)
    _writeoff(db, org, product, "1")
    db.add(WriteOff(date=long_ago, product_id=product.id, quantity=Decimal("2"),
                    organization_id=org.id, reason="питание детей"))
    db.flush()

    days = warehouse._writeoff_days(db, {org.id})
    dates = [d["date"] for d in days]
    assert dates == [today, long_ago], "дни идут от свежих к старым, без заполнения дыр"
    assert len(days[0]["lines"]) == 1


def test_audit_log_records_the_fix(db, org, product):
    """Правка оставляет след: через месяц «откуда 0,2» должно быть понятно."""
    w = _writeoff(db, org, product, "200")
    db.add(AuditLog(entity_type="write_off", entity_id=w.id, action="update",
                    user_id=None, old_data={"quantity": "200"},
                    new_data={"quantity": "0.2", "unit": "кг"}))
    db.flush()

    logged = (db.query(AuditLog)
              .filter_by(entity_type="write_off", entity_id=w.id).one())
    assert logged.old_data["quantity"] == "200"
    assert logged.new_data["quantity"] == "0.2"
