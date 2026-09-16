"""Касса с карманами (новый вход, блок 4, 16.09)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import CashFunding, CashTransfer, Organization, Reconciliation, Transaction, User
from app.routers import new_buy as buy_router
from app.routers import new_cash as cash_router
from app.services import cash as svc
from app.services import podotchet


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень кс", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-кс", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    school = Organization(name="Школа тест-кс", type="school", parent_id=root.id, site_id=sadik.id)
    db.add(school)
    db.flush()
    return sadik, school


@pytest.fixture()
def people(db, site):
    sadik, _ = site
    m = User(name="Махабат тест-кс", role="staff", organization_id=sadik.id)
    mu = User(name="Мунара тест-кс", role="manager", organization_id=sadik.id)
    f = User(name="Айдай тест-кс", role="founder", organization_id=sadik.id)
    db.add_all([m, mu, f])
    db.flush()
    return m, mu, f


@pytest.fixture()
def as_makhabat(db, site, people, monkeypatch):
    m, _, _ = people
    monkeypatch.setattr(cash_router, "get_current_user", lambda request, db: m)
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: site[0])
    return m


def test_withdrawal_goes_to_presser_pocket_and_reduces_that_account(client, db, site, people, as_makhabat):
    sadik, school = site
    m, mu, _ = people
    acc0 = podotchet.get_expected_balance(db, school.id, date.today())["expected"]
    r = client.post("/new/cash/withdraw", data={"account_org_id": school.id, "amount": "40 000",
                                                "date": date.today().isoformat(), "pocket_user_id": m.id},
                    follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    assert svc.pocket_balance(db, sadik.id, m.id) == Decimal(40000)
    assert svc.pocket_balance(db, sadik.id, mu.id) == 0
    assert podotchet.get_expected_balance(db, school.id, date.today())["expected"] == acc0 - Decimal(40000)
    assert podotchet.get_cash_state(db, sadik.id)["net"] == Decimal(40000)  # касса площадки выросла


def test_expense_leaves_payer_pocket(client, db, site, people, as_makhabat):
    sadik, _ = site
    m, mu, _ = people
    svc.withdraw(db, user=mu, site_org_id=sadik.id, account_org_id=sadik.id, amount=Decimal(10000), d=date.today())
    db.add(Transaction(organization_id=sadik.id, type="expense", amount=3000, date=date.today(),
                       paid_from_user_id=m.id, created_by=m.id))
    db.add(Transaction(organization_id=sadik.id, type="expense", amount=500, date=date.today(), created_by=mu.id))  # без кармана → кто завёл
    db.flush()
    assert svc.pocket_balance(db, sadik.id, mu.id) == Decimal(9500)
    assert svc.pocket_balance(db, sadik.id, m.id) == Decimal(-3000)
    p = svc.pockets(db, sadik.id)
    assert p["total"] == Decimal(6500) and abs(p["unassigned"]) < 1


def test_transfer_moves_between_pockets(client, db, site, people, as_makhabat):
    sadik, _ = site
    m, mu, _ = people
    svc.withdraw(db, user=mu, site_org_id=sadik.id, account_org_id=sadik.id, amount=Decimal(10000), d=date.today())
    r = client.post("/new/cash/transfer", data={"from_user_id": mu.id, "to_user_id": m.id, "amount": "7000",
                                                "date": date.today().isoformat()}, follow_redirects=False)
    assert r.status_code == 303
    assert svc.pocket_balance(db, sadik.id, mu.id) == Decimal(3000)
    assert svc.pocket_balance(db, sadik.id, m.id) == Decimal(7000)
    r = client.post("/new/cash/transfer", data={"from_user_id": m.id, "to_user_id": m.id, "amount": "1",
                                                "date": date.today().isoformat()}, follow_redirects=False)
    assert r.status_code == 200 and "только другому" in r.text


def test_recount_sets_new_base_and_keeps_delta(client, db, site, people, as_makhabat):
    sadik, _ = site
    m, _, _ = people
    svc.withdraw(db, user=m, site_org_id=sadik.id, account_org_id=sadik.id, amount=Decimal(10000), d=date.today() - timedelta(days=2))
    # разница выше порога без причины — отказ
    r = client.post("/new/cash/recount", data={"pocket_user_id": m.id, "amount": "8000", "date": date.today().isoformat(), "reason": ""},
                    follow_redirects=False)
    assert r.status_code == 200 and "напишите" in r.text
    r = client.post("/new/cash/recount", data={"pocket_user_id": m.id, "amount": "8000", "date": date.today().isoformat(),
                                               "reason": "не записала такси"}, follow_redirects=False)
    assert r.status_code == 303
    rec = db.query(Reconciliation).filter_by(kind="pocket", subject_id=m.id).one()
    assert rec.expected_amount == 10000 and rec.actual_amount == 8000 and rec.delta == -2000
    assert svc.pocket_balance(db, sadik.id, m.id) == Decimal(8000)
    # движение после пересчёта считается от новой базы. created_at явно: в одной
    # тестовой транзакции now() у Postgres не меняется, а граница «после
    # пересчёта» идёт по времени занесения
    from datetime import datetime
    db.add(Transaction(organization_id=sadik.id, type="expense", amount=1000, date=date.today(),
                       paid_from_user_id=m.id, created_by=m.id, created_at=datetime.now()))
    db.flush()
    assert svc.pocket_balance(db, sadik.id, m.id) == Decimal(7000)


def test_founder_fund_and_withdraw(client, db, site, people, as_makhabat):
    sadik, _ = site
    m, _, f = people
    r = client.post("/new/cash/founder", data={"direction": "fund", "founder_id": f.id, "pocket_user_id": m.id,
                                               "amount": "100000", "date": date.today().isoformat()}, follow_redirects=False)
    assert r.status_code == 303
    assert svc.pocket_balance(db, sadik.id, m.id) == Decimal(100000)
    fund = db.query(CashFunding).filter_by(source_founder_id=f.id).one()
    assert fund.accountable_user_id == m.id and fund.source_type == "direct_cash"
    r = client.post("/new/cash/founder", data={"direction": "withdraw", "founder_id": f.id, "pocket_user_id": m.id,
                                               "amount": "30000", "date": date.today().isoformat()}, follow_redirects=False)
    assert r.status_code == 303
    assert svc.pocket_balance(db, sadik.id, m.id) == Decimal(70000)
    assert podotchet.get_cash_state(db, sadik.id)["net"] == Decimal(70000)


def test_internal_funding_is_a_pocket_transfer(db, site, people, as_makhabat):
    """Пополнение с source_organization_id = та же касса (переход 15.09) —
    перевод между карманами: сумма карманов равна кассе объекта."""
    sadik, _ = site
    m, mu, _ = people
    svc.withdraw(db, user=m, site_org_id=sadik.id, account_org_id=sadik.id, amount=Decimal(250000), d=date.today())
    db.add(CashFunding(organization_id=sadik.id, source_type="direct_cash", amount=34375, date=date.today(),
                       taken_by=m.id, accountable_user_id=mu.id, source_organization_id=sadik.id, created_by=m.id))
    db.flush()
    p = svc.pockets(db, sadik.id)
    by = {r["user"].id: r["balance"] for r in p["rows"]}
    assert by[m.id] == Decimal(250000 - 34375) and by[mu.id] == Decimal(34375)
    assert p["total"] == Decimal(250000) and abs(p["unassigned"]) < 1


def test_cash_page_renders(client, db, site, people, as_makhabat):
    page = client.get("/new/cash")
    assert page.status_code == 200 and "Наличные" in page.text and "На счетах" in page.text
