"""Пересчёт склада как сессия (09.09).

Ключевые свойства, которые тесты держат:
  - до «Завершить» остатки не меняются вообще;
  - «не отмечено» и «насчитали ноль» — разные вещи;
  - разница считается от остатка на момент применения, а движение, прошедшее
    во время обхода, видно отдельно, а не проглатывается;
  - сигнал «что-то не так» не трогает карточку товара.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import (
    Organization, Product, StockCount, StockCountLine, User,
    WarehouseReceipt, WriteOff,
)
from app.services import stock_count


@pytest.fixture()
def org(db):
    u = User(name="__счётчик__", role="staff")
    db.add(u)
    db.flush()
    o = Organization(name="__Склад-тест__", type="садик")
    db.add(o)
    db.flush()
    o._user_id = u.id
    return o


def _product(db, name, unit="кг", category="крупы"):
    p = Product(name=name, unit=unit, category=category)
    db.add(p)
    db.flush()
    return p


def _receipt(db, org, product, qty, cost, on_date=None):
    db.add(WarehouseReceipt(
        date=on_date or date.today() - timedelta(days=3), product_id=product.id,
        quantity=Decimal(qty), price_per_unit=Decimal(cost) / Decimal(qty),
        total_cost=Decimal(cost), organization_id=org.id,
    ))
    db.flush()


def _writeoff(db, org, product, qty, on_date=None):
    db.add(WriteOff(
        date=on_date or date.today() - timedelta(days=2), product_id=product.id,
        quantity=Decimal(qty), organization_id=org.id, reason="тест",
    ))
    db.flush()


def _start(db, org):
    return stock_count.start(db, org.id, {org.id}, org._user_id)


def _line(db, count, product):
    return (db.query(StockCountLine)
            .filter(StockCountLine.count_id == count.id,
                    StockCountLine.product_id == product.id)
            .one())


def _balance(db, org, product):
    return stock_count._balance_map(db, {org.id})[product.id]["balance"]


# --- состав рабочего списка --------------------------------------------------

def test_working_set_takes_recent_and_nonzero_only(db, org):
    recent = _product(db, "__Свежий закуп__")
    _receipt(db, org, recent, "10", "1000")

    stale = _product(db, "__Давно не брали__")
    _receipt(db, org, stale, "5", "500", on_date=date.today() - timedelta(days=200))
    _writeoff(db, org, stale, "5", on_date=date.today() - timedelta(days=199))

    never = _product(db, "__Ни разу не трогали__")

    names = {i["name"] for i in stock_count.working_set(db, {org.id})}
    assert "__Свежий закуп__" in names
    assert "__Давно не брали__" not in names, "нулевой остаток и старый закуп — в список не берём"
    assert "__Ни разу не трогали__" not in names


def test_negative_balance_gets_into_the_list(db, org):
    """Старая форма грузила только товары с остатком > 0 — именно минусы,
    ради которых пересчёт и нужен, в неё не попадали."""
    p = _product(db, "__Ушёл в минус__")
    _writeoff(db, org, p, "40", on_date=date.today() - timedelta(days=200))

    names = {i["name"] for i in stock_count.working_set(db, {org.id})}
    assert "__Ушёл в минус__" in names


# --- сессия ------------------------------------------------------------------

def test_start_creates_a_line_per_product(db, org):
    a = _product(db, "__Товар А__")
    b = _product(db, "__Товар Б__")
    _receipt(db, org, a, "10", "1000")
    _receipt(db, org, b, "20", "2000")

    count = _start(db, org)
    assert db.query(StockCountLine).filter(StockCountLine.count_id == count.id).count() == 2
    assert stock_count.progress(db, count.id) == {"total": 2, "marked": 0, "left": 2}


def test_start_twice_returns_the_same_session(db, org):
    _receipt(db, org, _product(db, "__Товар__"), "10", "1000")
    first = _start(db, org)
    second = _start(db, org)
    assert first.id == second.id, "одна активная сессия на объект"


def test_marking_does_not_touch_stock(db, org):
    """Главное свойство: до завершения склад не двигается."""
    p = _product(db, "__Товар__")
    _receipt(db, org, p, "10", "1000")
    count = _start(db, org)

    stock_count.mark(db, _line(db, count, p), stock_count.MODE_NUMBER,
                     Decimal("3"), Decimal("10"), org._user_id)
    db.flush()

    assert _balance(db, org, p) == Decimal("10"), "остаток не должен меняться до «Завершить»"


def test_unmarked_is_not_the_same_as_zero(db, org):
    """В старой форме пустая строка и «ноль» выглядели одинаково — позиция
    молча пропускалась, и отличить пропуск от честного нуля было нельзя."""
    a = _product(db, "__Не дошли__")
    b = _product(db, "__Насчитали ноль__")
    _receipt(db, org, a, "10", "1000")
    _receipt(db, org, b, "10", "1000")
    count = _start(db, org)

    stock_count.mark(db, _line(db, count, b), stock_count.MODE_ZERO, None,
                     Decimal("10"), org._user_id)
    db.flush()

    assert _line(db, count, a).actual_qty is None
    assert _line(db, count, b).actual_qty == Decimal("0")
    assert stock_count.progress(db, count.id)["left"] == 1


def test_unmark_returns_line_to_untouched(db, org):
    p = _product(db, "__Товар__")
    _receipt(db, org, p, "10", "1000")
    count = _start(db, org)
    line = _line(db, count, p)

    stock_count.mark(db, line, stock_count.MODE_SAME, None, Decimal("10"), org._user_id)
    db.flush()
    assert stock_count.progress(db, count.id)["marked"] == 1

    stock_count.unmark(db, line)
    db.flush()
    assert stock_count.progress(db, count.id)["marked"] == 0
    assert line.actual_qty is None


# --- применение --------------------------------------------------------------

def test_apply_creates_receipt_and_writeoff(db, org):
    up = _product(db, "__Стало больше__")
    down = _product(db, "__Стало меньше__")
    _receipt(db, org, up, "10", "1000")     # 100 сом/кг
    _receipt(db, org, down, "10", "2000")
    count = _start(db, org)

    stock_count.mark(db, _line(db, count, up), stock_count.MODE_NUMBER,
                     Decimal("15"), Decimal("10"), org._user_id)
    stock_count.mark(db, _line(db, count, down), stock_count.MODE_NUMBER,
                     Decimal("4"), Decimal("10"), org._user_id)
    db.flush()
    stock_count.apply(db, count, {org.id}, org._user_id)
    db.flush()

    assert _balance(db, org, up) == Decimal("15")
    assert _balance(db, org, down) == Decimal("4")
    assert count.status == "applied"

    # Приход по недостаче оценён средней ценой товара, а не нулём
    extra = (db.query(WarehouseReceipt)
             .filter(WarehouseReceipt.product_id == up.id,
                     WarehouseReceipt.supplier_name == "Пересчёт склада").one())
    assert extra.quantity == Decimal("5.000")
    assert extra.total_cost == Decimal("500.00")


def test_apply_skips_unmarked_lines(db, org):
    touched = _product(db, "__Отметили__")
    skipped = _product(db, "__Пропустили__")
    _receipt(db, org, touched, "10", "1000")
    _receipt(db, org, skipped, "7", "700")
    count = _start(db, org)

    stock_count.mark(db, _line(db, count, touched), stock_count.MODE_NUMBER,
                     Decimal("2"), Decimal("10"), org._user_id)
    db.flush()
    stock_count.apply(db, count, {org.id}, org._user_id)
    db.flush()

    assert _balance(db, org, touched) == Decimal("2")
    assert _balance(db, org, skipped) == Decimal("7"), "непройденную позицию не трогаем"


def test_apply_pulls_product_out_of_minus(db, org):
    """Ради этого случая всё и затевалось: Сахар −38, по факту 20."""
    p = _product(db, "__Сахар-тест__")
    _writeoff(db, org, p, "38")
    count = _start(db, org)

    stock_count.mark(db, _line(db, count, p), stock_count.MODE_NUMBER,
                     Decimal("20"), Decimal("-38"), org._user_id)
    db.flush()
    stock_count.apply(db, count, {org.id}, org._user_id)
    db.flush()

    assert _balance(db, org, p) == Decimal("20")


def test_no_price_products_are_listed_in_summary(db, org):
    """Товар, который ни разу не приходовался, придёт с нулевой стоимостью —
    об этом должно быть сказано до применения, а не после."""
    p = _product(db, "__Без цены__")
    _writeoff(db, org, p, "5")
    count = _start(db, org)

    stock_count.mark(db, _line(db, count, p), stock_count.MODE_NUMBER,
                     Decimal("10"), Decimal("-5"), org._user_id)
    db.flush()

    s = stock_count.summary(db, count, {org.id})
    assert [r["name"] for r in s["no_price"]] == ["__Без цены__"]


def test_movement_during_the_count_is_surfaced(db, org):
    """Пока позицию считали, по ней провели приход — на завершении это видно."""
    p = _product(db, "__Приехало по ходу__")
    _receipt(db, org, p, "10", "1000")
    count = _start(db, org)

    stock_count.mark(db, _line(db, count, p), stock_count.MODE_NUMBER,
                     Decimal("12"), Decimal("10"), org._user_id)
    db.flush()
    _receipt(db, org, p, "50", "5000", on_date=date.today())

    s = stock_count.summary(db, count, {org.id})
    assert len(s["moved"]) == 1
    assert s["moved"][0]["was_at_marking"] == Decimal("10.000")
    assert s["moved"][0]["current"] == Decimal("60")


# --- сигналы и отмена --------------------------------------------------------

def test_flag_does_not_change_the_product(db, org):
    """Единицу задним числом не меняем — это переписало бы смысл всей истории."""
    p = _product(db, "__Кинза-тест__", unit="кг")
    _receipt(db, org, p, "50", "1020")
    count = _start(db, org)
    line = _line(db, count, p)

    stock_count.flag(db, line, "unit", "считаем пучками, не килограммами", org._user_id)
    db.flush()

    assert line.issue == "unit"
    assert p.unit == "кг", "карточка товара меняться не должна"
    assert stock_count.summary(db, count, {org.id})["issues"][0]["name"] == "__Кинза-тест__"


def test_cancel_keeps_stock_untouched(db, org):
    p = _product(db, "__Товар__")
    _receipt(db, org, p, "10", "1000")
    count = _start(db, org)
    stock_count.mark(db, _line(db, count, p), stock_count.MODE_NUMBER,
                     Decimal("3"), Decimal("10"), org._user_id)
    db.flush()

    stock_count.cancel(db, count, org._user_id, "начали не тем числом")
    db.flush()

    assert count.status == "cancelled"
    assert _balance(db, org, p) == Decimal("10")
    assert stock_count.get_active(db, org.id) is None


def test_new_session_can_start_after_cancel(db, org):
    _receipt(db, org, _product(db, "__Товар__"), "10", "1000")
    first = _start(db, org)
    stock_count.cancel(db, first, org._user_id, "ошиблись")
    db.flush()

    second = _start(db, org)
    assert second.id != first.id
    assert second.status == "active"


# --- лист пересчёта как приложение (10.09) ---------------------------------
# Бумажная тетрадь — основание для цифр: без неё через месяц не проверить,
# откуда взялось «сахар 1 270 кг». Снимок живёт при сессии, а не «где-то в
# расходах», и переживает завершение пересчёта.

def test_photos_attach_to_session(db, org):
    from app.models import StockCountPhoto

    count = _start(db, org)
    db.add(StockCountPhoto(count_id=count.id, file_path="stock_counts/2026-09/a.jpg",
                           caption="страница 1", uploaded_by=org._user_id))
    db.add(StockCountPhoto(count_id=count.id, file_path="stock_counts/2026-09/b.jpg",
                           caption="страница 2", uploaded_by=org._user_id))
    db.flush()
    db.refresh(count)

    assert [p.caption for p in count.photos] == ["страница 1", "страница 2"]


def test_photos_survive_apply(db, org):
    """Приложение нужно именно после применения — тогда по нему и сверяются."""
    from app.models import StockCountPhoto

    p = _product(db, "__Сахар-тест__")
    _receipt(db, org, p, "10", "1000")
    count = _start(db, org)
    db.add(StockCountPhoto(count_id=count.id, file_path="stock_counts/2026-09/c.jpg",
                           uploaded_by=org._user_id))
    stock_count.mark(db, _line(db, count, p), stock_count.MODE_NUMBER,
                     Decimal("4"), Decimal("10"), org._user_id)
    db.flush()

    stock_count.apply(db, count, {org.id}, org._user_id)
    db.flush()
    db.refresh(count)

    assert count.status == "applied"
    assert len(count.photos) == 1
    assert _balance(db, org, p) == Decimal("4")


def test_photos_go_away_with_session(db, org):
    """Удалили пересчёт — приложения не остаются висеть сиротами."""
    from app.models import StockCountPhoto

    count = _start(db, org)
    db.add(StockCountPhoto(count_id=count.id, file_path="stock_counts/2026-09/d.jpg",
                           uploaded_by=org._user_id))
    db.flush()
    count_id = count.id

    db.delete(count)
    db.flush()

    left = db.query(StockCountPhoto).filter_by(count_id=count_id).count()
    assert left == 0
