"""Долг поставщику: сверка как точка отсчёта и её граница по времени (09.09).

Сверка долга поставщику заменяет всё, что было до её даты — человек называет
долг «на сегодня», значит прошлые закупы в эту сумму уже вошли. Граница до
09.09 считалась по одной дате, и это съедало закупы, заведённые задним числом
уже ПОСЛЕ сверки: у Халимы сверку сохранили в 15:08, а в 15:17 занесли два
вчерашних закупа на 6 140,50 — они исчезли из долга, хотя человек, называя
сумму, знать о них не мог.

Тесты фиксируют обе стороны границы и то, что батч-версия
(_bulk_ledger_buckets, ею считается список расходов) отвечает так же, как
поштучная — иначе карточка поставщика и список расходов разошлись бы в цифрах.
"""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.models import Organization, Reconciliation, Supplier, SupplierPayment, Transaction
from app.services import supplier_ledger

RECONCILED_AT = datetime(2026, 9, 9, 15, 8)


@pytest.fixture()
def org(db):
    o = Organization(name="Тестовый объект", type="садик")
    db.add(o)
    db.flush()
    return o


@pytest.fixture()
def supplier(db):
    s = Supplier(name="Халима Овощи", phone="0700000000")
    db.add(s)
    db.flush()
    return s


def _purchase(db, org, supplier, amount, on_date, created_at, paid=0):
    t = Transaction(
        organization_id=org.id, type="expense", supplier_id=supplier.id,
        amount=Decimal(amount), amount_paid=Decimal(paid), date=on_date,
        paid_directly=False, created_at=created_at,
    )
    db.add(t)
    db.flush()
    return t


def _reset(db, org, supplier, actual, on_date, created_at=RECONCILED_AT):
    r = Reconciliation(
        organization_id=org.id, kind="supplier_debt", subject_id=supplier.id,
        date=on_date, expected_amount=Decimal("0"), actual_amount=Decimal(actual),
        delta=Decimal("0"), created_at=created_at,
    )
    db.add(r)
    db.flush()
    return r


def _both_balances(db, supplier):
    """Поштучный и батч-расчёт — должны совпадать всегда."""
    single = supplier_ledger.get_supplier_balance(db, supplier.id)
    bulk = supplier_ledger.get_supplier_balances_bulk(db, [supplier.id])[supplier.id]
    assert single == bulk, f"поштучно {single}, батчем {bulk}"
    return single


def test_purchase_before_reconciliation_is_swallowed(db, org, supplier):
    """Закуп, заведённый до сверки — человек его видел, называя долг."""
    _purchase(db, org, supplier, "5000", date(2026, 9, 8), datetime(2026, 9, 8, 10, 0))
    _reset(db, org, supplier, "8000", date(2026, 9, 9))

    assert _both_balances(db, supplier) == Decimal("8000")


def test_backdated_purchase_entered_after_reconciliation_survives(db, org, supplier):
    """Случай Халимы: закуп за вчера, занесён через 9 минут после сверки.

    Дата раньше сверки, но в названную сумму он войти не мог — значит долг
    равен подтверждённому плюс этот закуп.
    """
    _purchase(db, org, supplier, "6140.50", date(2026, 9, 8), datetime(2026, 9, 9, 15, 17))
    _reset(db, org, supplier, "8000", date(2026, 9, 9))

    assert _both_balances(db, supplier) == Decimal("14140.50")


def test_purchase_same_day_before_reconciliation_is_swallowed(db, org, supplier):
    """Тот же день, но занесён раньше сверки — уже внутри суммы."""
    _purchase(db, org, supplier, "7000", date(2026, 9, 9), datetime(2026, 9, 9, 12, 5))
    _reset(db, org, supplier, "8000", date(2026, 9, 9))

    assert _both_balances(db, supplier) == Decimal("8000")


def test_purchase_after_reconciliation_adds_to_debt(db, org, supplier):
    """Обычный случай: закуп позже сверки."""
    _purchase(db, org, supplier, "2000", date(2026, 9, 10), datetime(2026, 9, 10, 9, 0))
    _reset(db, org, supplier, "8000", date(2026, 9, 9))

    assert _both_balances(db, supplier) == Decimal("10000")


def test_backdated_payment_entered_after_reconciliation_reduces_debt(db, org, supplier):
    """Платёж по той же границе: занесён после сверки — гасит долг.

    Иначе деньги, отданные поставщику и записанные вечером, просто пропали бы.
    """
    _reset(db, org, supplier, "8000", date(2026, 9, 9))
    db.add(SupplierPayment(
        supplier_id=supplier.id, amount=Decimal("3000"), date=date(2026, 9, 8),
        created_at=datetime(2026, 9, 9, 16, 0),
    ))
    db.flush()

    assert _both_balances(db, supplier) == Decimal("5000")


def test_payment_before_reconciliation_is_already_inside(db, org, supplier):
    """Платёж, сделанный и занесённый до сверки, второй раз долг не гасит."""
    _reset(db, org, supplier, "8000", date(2026, 9, 9))
    db.add(SupplierPayment(
        supplier_id=supplier.id, amount=Decimal("3000"), date=date(2026, 9, 7),
        created_at=datetime(2026, 9, 7, 11, 0),
    ))
    db.flush()

    assert _both_balances(db, supplier) == Decimal("8000")


def test_cancelled_reconciliation_restores_full_history(db, org, supplier):
    """Отменённая сверка перестаёт быть точкой отсчёта — долг снова из закупов.

    Ровно то, что должно произойти после отмены записи Халимы: 8 000 уходят,
    закупы возвращаются.
    """
    _purchase(db, org, supplier, "5000", date(2026, 9, 8), datetime(2026, 9, 8, 10, 0))
    rec = _reset(db, org, supplier, "8000", date(2026, 9, 9))
    assert _both_balances(db, supplier) == Decimal("8000")

    rec.cancelled_at = datetime(2026, 9, 9, 18, 0)
    rec.cancel_reason = "старый долг занесён неверно"
    db.flush()

    assert _both_balances(db, supplier) == Decimal("5000")


def test_opening_balance_adds_to_purchases_without_swallowing_them(db, org, supplier):
    """Правильный инструмент для старого долга — начальное сальдо.

    В отличие от сверки, оно прибавляется к закупам, а не заменяет их: история
    остаётся видна построчно. Это и есть развязка случая Халимы —
    8 000 старого долга плюс закупы, а не 8 000 вместо них.
    """
    supplier.opening_balance = Decimal("8000")
    supplier.opening_balance_date = date(2026, 5, 31)
    _purchase(db, org, supplier, "5000", date(2026, 9, 8), datetime(2026, 9, 8, 10, 0))
    db.flush()

    assert _both_balances(db, supplier) == Decimal("13000")

    rows = supplier_ledger.get_supplier_ledger(db, supplier.id)
    kinds = [r["kind"] for r in rows]
    assert "opening" in kinds and "purchase" in kinds
