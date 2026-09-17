"""Зарплата нового входа (17.09, макет 4в)."""
from datetime import date
from decimal import Decimal

import pytest

from app.models import AuditLog, Employee, Organization, Transaction, User
from app.routers import new_buy as buy_router
from app.routers import new_salary as sal_router
from app.services import cash, podotchet
from app.services import salary as svc


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень зп", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-зп", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    school = Organization(name="Школа тест-зп", type="school", parent_id=root.id, site_id=sadik.id)
    db.add(school)
    db.flush()
    return sadik, school


@pytest.fixture()
def people(db, site):
    sadik, school = site
    e1 = Employee(organization_id=sadik.id, full_name="Маралова тест-зп", role="повар", salary=40000)
    e2 = Employee(organization_id=sadik.id, full_name="Абдисаламова тест-зп", role="воспитатель", salary=40000)
    e3 = Employee(organization_id=school.id, full_name="Учитель тест-зп", role="учитель", salary=50000)
    m = User(name="Махабат тест-зп", role="staff", organization_id=sadik.id)
    db.add_all([e1, e2, e3, m])
    db.flush()
    return e1, e2, e3, m


@pytest.fixture()
def as_makhabat(db, site, people, monkeypatch):
    m = people[3]
    monkeypatch.setattr(sal_router, "get_current_user", lambda request, db: m)
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: site[0])
    monkeypatch.setattr(svc, "get_accessible_orgs", lambda user, db, all_orgs=None: list(site))
    return m


AUG = date(2026, 8, 1)


def _pay(client, emp, amount, source, **extra):
    data = {"employee_id": emp.id, "month": "2026-08", "amount": amount, "date": date.today().isoformat(),
            "source": source}
    data.update(extra)
    return client.post("/new/salary/pay", data=data, follow_redirects=False)


def test_school_payroll_hidden_from_makhabat(client, db, site, people, as_makhabat):
    e1, e2, e3, m = people
    page = client.get("/new/salary?month=2026-08")
    assert page.status_code == 200
    assert e1.full_name in page.text and e3.full_name not in page.text
    assert _pay(client, e3, "1000", f"pocket:{m.id}").status_code == 404


def test_pay_from_pocket_goes_to_august_and_leaves_pocket(client, db, site, people, as_makhabat):
    sadik, _ = site
    e1, e2, _, m = people
    pocket0 = cash.pocket_balance(db, sadik.id, m.id)
    r = _pay(client, e1, "32 000", f"pocket:{m.id}")
    assert r.status_code == 303, r.text[:300]
    tx = db.query(Transaction).filter_by(employee_id=e1.id).one()
    assert tx.period == AUG and tx.date == date.today() and tx.paid_from_user_id == m.id and not tx.paid_directly
    assert cash.pocket_balance(db, sadik.id, m.id) == pocket0 - Decimal(32000)
    sheet = svc.sheet(db, [sadik], AUG)
    row = next(x for x in sheet["rows"] if x["employee"].id == e1.id)
    assert row["issued"] == 32000 and row["left"] == 8000
    assert _pay(client, e1, "8000", f"pocket:{m.id}").status_code == 303   # частями складывается
    row = next(x for x in svc.sheet(db, [sadik], AUG)["rows"] if x["employee"].id == e1.id)
    assert row["left"] == 0


def test_pay_from_school_account_reduces_school_account_not_cash(client, db, site, people, as_makhabat):
    sadik, school = site
    e1, _, _, m = people
    acc_school = podotchet.get_expected_balance(db, school.id, date.today())["expected"]
    acc_sadik = podotchet.get_expected_balance(db, sadik.id, date.today())["expected"]
    cash0 = podotchet.get_cash_state(db, sadik.id)["net"]
    assert _pay(client, e1, "5000", f"account:{school.id}").status_code == 303
    assert podotchet.get_expected_balance(db, school.id, date.today())["expected"] == acc_school - Decimal(5000)
    assert podotchet.get_expected_balance(db, sadik.id, date.today())["expected"] == acc_sadik
    assert podotchet.get_cash_state(db, sadik.id)["net"] == cash0


def test_same_payout_again_asks_and_token_repeat_writes_once(client, db, site, people, as_makhabat):
    e1, _, _, m = people
    count = lambda: db.query(Transaction).filter_by(employee_id=e1.id, deleted_at=None).count()
    token = "tok-salary-00000000000001"
    assert _pay(client, e1, "3000", f"pocket:{m.id}", form_token=token).status_code == 303
    assert _pay(client, e1, "3000", f"pocket:{m.id}", form_token=token).status_code == 303
    assert count() == 1
    r = _pay(client, e1, "3000", f"pocket:{m.id}", form_token="tok-salary-00000000000002")
    assert r.status_code == 200 and "Такое уже записано: выдача 3 000" in r.text and count() == 1
    assert _pay(client, e1, "3000", f"pocket:{m.id}", repeat_ok="1").status_code == 303
    assert count() == 2


def test_remove_odd_payout_is_soft_and_audited(client, db, site, people, as_makhabat):
    sadik, _ = site
    e1, _, _, m = people
    _pay(client, e1, "3", f"pocket:{m.id}")
    tx = db.query(Transaction).filter_by(employee_id=e1.id).one()
    page = client.get(f"/new/salary?month=2026-08&open={e1.id}")
    assert "похоже на опечатку" in page.text
    r = client.post(f"/new/salary/{tx.id}/remove", follow_redirects=False)
    assert r.status_code == 303
    db.refresh(tx)
    assert tx.deleted_at is not None
    assert db.query(AuditLog).filter_by(entity_type="transaction", entity_id=tx.id, action="delete", user_id=m.id).count() == 1
    assert svc.sheet(db, [sadik], AUG)["issued"] == 0


def test_late_after_payday(db, site, people):
    sadik, _ = site
    assert svc.sheet(db, [sadik], AUG, today=date(2026, 9, 11))["late"] is True
    assert svc.sheet(db, [sadik], AUG, today=date(2026, 9, 10))["late"] is False
    assert svc.prev_month(date(2026, 9, 17)) == AUG
