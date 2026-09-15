"""Слияние площадки: склад и касса Школы → Садик Сокулук (схема 06, 15.09)."""
from datetime import date
from decimal import Decimal

import pytest

from app.models import (CashFunding, Organization, Product, StockCount, StockCountLine, User,
                        WarehouseReceipt, WriteOff)
from app.services.podotchet import get_cash_state
from app.services.transition import _source_stock, merge_site
from app.services.warehouse import get_product_balances

CUTOFF = date(2026, 9, 9)
REASON = "слияние складов: тест"


@pytest.fixture()
def orgs(db):
    school = Organization(name="Школа тест-слияние", type="school")
    sadik = Organization(name="Садик тест-слияние", type="kindergarten")
    db.add_all([school, sadik])
    db.flush()
    owner = User(name="Владелец тест-слияние", role="owner", organization_id=sadik.id)
    aizhan = User(name="Айжан тест-слияние", role="director", organization_id=school.id)
    db.add_all([owner, aizhan])
    db.flush()
    return school, sadik, owner, aizhan


def _receipt(db, org, product, qty, price, when=date(2026, 6, 29)):
    db.add(WarehouseReceipt(date=when, product_id=product.id, quantity=qty, price_per_unit=price,
                            total_cost=qty * price, organization_id=org.id))
    db.flush()


def _balance(db, org, product):
    rows = {r["product"].id: r for r in get_product_balances(db, {org.id})}
    return Decimal(str(rows[product.id]["balance"])) if product.id in rows else Decimal(0)


def test_stock_moves_and_target_balance_unchanged(db, orgs):
    school, sadik, owner, _ = orgs
    potato = Product(name="Картофель тест-слияние", unit="кг")
    db.add(potato)
    db.flush()
    _receipt(db, school, potato, 175, 35)                       # июнь, Школа
    _receipt(db, sadik, potato, 100, 30, date(2026, 9, 1))      # Садик
    db.add(WriteOff(date=date(2026, 9, 5), product_id=potato.id, quantity=72, organization_id=sadik.id))
    db.flush()
    before = _balance(db, sadik, potato)
    assert before == 28

    lines = merge_site(db, school.id, sadik.id, CUTOFF, REASON, owner.id, dry_run=False)
    text = "\n".join(lines)
    assert "остаток Школа тест-слияние 175 кг списан датой 09.09" in text
    assert "приходов Школа тест-слияние → Садик тест-слияние: 1" in text
    assert _balance(db, sadik, potato) == before
    assert _balance(db, school, potato) == 0
    assert db.query(WarehouseReceipt).filter_by(organization_id=school.id).count() == 0
    comp = db.query(WriteOff).filter_by(organization_id=sadik.id, reason=REASON).one()
    assert comp.quantity == 175 and comp.date == CUTOFF and comp.created_by == owner.id
    assert db.get(Organization, school.id).site_id == sadik.id


def test_cash_loan_becomes_pocket(db, orgs):
    school, sadik, owner, aizhan = orgs
    db.add(CashFunding(organization_id=sadik.id, source_type="withdrawal", amount=250000,
                       date=date(2026, 9, 9), taken_by=owner.id, accountable_user_id=owner.id))
    db.add(CashFunding(organization_id=school.id, source_type="direct_cash", amount=34375,
                       date=date(2026, 9, 9), taken_by=owner.id, accountable_user_id=aizhan.id,
                       source_organization_id=sadik.id))
    db.flush()
    assert get_cash_state(db, sadik.id)["net"] == Decimal("215625")   # 250 000 − отдано Школе

    lines = merge_site(db, school.id, sadik.id, CUTOFF, REASON, owner.id, dry_run=False)
    assert any("больше не заём" in l for l in lines)
    assert get_cash_state(db, sadik.id)["net"] == Decimal("284375")   # 250 000 + 34 375, заём исчез
    assert get_cash_state(db, school.id)["net"] == 0
    f = db.query(CashFunding).filter_by(accountable_user_id=aizhan.id).one()
    assert f.organization_id == sadik.id and f.source_organization_id is None


def test_dangling_count_cancelled_only_when_empty(db, orgs):
    school, sadik, owner, _ = orgs
    empty = StockCount(organization_id=sadik.id, count_date=date(2026, 9, 11), started_by=owner.id)
    db.add(empty)
    db.flush()
    p = Product(name="Лук тест-слияние", unit="кг")
    db.add(p)
    db.flush()
    db.add(StockCountLine(count_id=empty.id, product_id=p.id))
    db.flush()
    lines = merge_site(db, school.id, sadik.id, CUTOFF, REASON, owner.id, dry_run=False)
    assert any(l.startswith("× пересчёт") for l in lines)
    assert empty.status == "cancelled" and empty.cancelled_by == owner.id

    # пересчёт с отметками не трогается
    school2 = Organization(name="Школа-2 тест-слияние", type="school")
    db.add(school2)
    db.flush()
    marked = StockCount(organization_id=sadik.id, count_date=date(2026, 9, 12), started_by=owner.id)
    db.add(marked)
    db.flush()
    db.add(StockCountLine(count_id=marked.id, product_id=p.id, actual_qty=3, mode="number"))
    db.flush()
    lines = merge_site(db, school2.id, sadik.id, CUTOFF, REASON, owner.id, dry_run=False)
    assert any(l.startswith("! пересчёт") for l in lines)
    assert marked.status == "active"


def test_dry_run_writes_nothing_and_apply_is_idempotent(db, orgs):
    school, sadik, owner, _ = orgs
    p = Product(name="Морковь тест-слияние", unit="кг")
    db.add(p)
    db.flush()
    _receipt(db, school, p, Decimal("45.2"), 30)
    lines = merge_site(db, school.id, sadik.id, CUTOFF, REASON, owner.id, dry_run=True)
    assert len(lines) == 3          # площадка, списание, приходы
    db.expire_all()
    assert db.get(Organization, school.id).site_id is None
    assert db.query(WarehouseReceipt).filter_by(organization_id=school.id).count() == 1
    assert _source_stock(db, school.id) == {p.id: Decimal("45.200")}

    merge_site(db, school.id, sadik.id, CUTOFF, REASON, owner.id, dry_run=False)
    again = merge_site(db, school.id, sadik.id, CUTOFF, REASON, owner.id, dry_run=False)
    assert again == []
