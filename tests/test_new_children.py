"""Дети нового входа (16.09, макет блок 5)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import CashFunding, Charge, Enrollment, Group, Organization, Service, Student, Transaction, User
from app.routers import new_buy as buy_router
from app.routers import new_children as ch_router
from app.services import cash, children as svc, podotchet


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень дт", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-дт", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    db.add(Service(organization_id=sadik.id, name="Тариф тест-дт", price=10000, is_tuition=True, is_recurring=True))
    db.flush()
    return sadik


@pytest.fixture()
def staff(db, site, monkeypatch):
    u = User(name="Махабат тест-дт", role="owner", organization_id=site.id)
    db.add(u)
    db.flush()
    monkeypatch.setattr(ch_router, "get_current_user", lambda request, db: u)
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: site)
    monkeypatch.setattr(ch_router, "get_accessible_orgs", lambda user, db, all_orgs=None: [site])
    monkeypatch.setattr(ch_router, "generate_monthly_charges", lambda db: 0)
    return u


@pytest.fixture()
def amir(db, site):
    g = Group(organization_id=site.id, name="Средняя тест-дт", type="kindergarten_group")
    db.add(g)
    db.flush()
    s = Student(organization_id=site.id, name="Капаров Амир тест-дт", pin="9977", status="active", discount_amount=500,
                discount_reason="второй ребёнок")
    db.add(s)
    db.flush()
    db.add(Enrollment(student_id=s.id, group_id=g.id, start_date=date(2026, 7, 1)))
    this = date.today().replace(day=1)
    prev = (this - timedelta(days=1)).replace(day=1)
    db.add(Charge(student_id=s.id, amount=9500, description="Начисление за месяц", date=prev))
    db.add(Charge(student_id=s.id, amount=9500, description="Начисление за месяц", date=this))
    db.add(Transaction(organization_id=site.id, type="income", amount=9500, student_id=s.id, date=prev + timedelta(days=4),
                       external_txn_id="opt-test-дт-1"))
    db.flush()
    return s


def test_list_shows_group_debt_and_unpaid_month(client, db, site, staff, amir):
    data = svc.children_list(db, site)
    assert data["count"] == 1 and data["debt_total"] == 9500 and data["old_total"] == 0
    row = data["groups"][0]["rows"][0]
    assert data["groups"][0]["name"] == "Средняя тест-дт"
    assert row["balance"] == 9500 and row["status"] == svc.month_name(date.today()) and row["kind"] == "debt"
    assert "скидка 500" in row["sub"]
    page = client.get(f"/new/children?org={site.id}")
    assert page.status_code == 200 and amir.name in page.text and "9 500" in page.text


def test_two_unpaid_months_is_bad(db, site, staff, amir):
    db.query(Transaction).filter_by(student_id=amir.id).delete()
    db.flush()
    row = svc.children_list(db, site)["groups"][0]["rows"][0]
    assert row["kind"] == "bad" and " и " in row["status"] and row["balance"] == 19000
    assert svc.children_list(db, site)["old_total"] == 9500


def test_card_events_and_months(client, db, site, staff, amir):
    card = svc.child_card(db, amir)
    assert card["balance"] == 9500 and card["kind"] == "debt"
    assert card["months"][0]["left"] == 9500 and card["months"][1]["left"] == 0 and card["months"][1]["paid"] == 9500
    texts = [e["text"] for e in card["events"]]
    assert any("Оплата через банк" in t for t in texts) and any(t.startswith("Начислено за") for t in texts)
    assert any(t.startswith("Зачислен") for t in texts)
    page = client.get(f"/new/children/{amir.id}")
    assert page.status_code == 200 and "Долг 9 500" in page.text and "Принять наличные" in page.text


def test_accept_cash_closes_oldest_debt_and_fills_pocket(client, db, site, staff, amir):
    pocket0 = cash.pocket_balance(db, site.id, staff.id)
    acc0 = podotchet.get_expected_balance(db, site.id, date.today())["expected"]
    r = client.post(f"/new/children/{amir.id}/cash", data={"amount": "9 500", "what": "садик за сентябрь",
                                                          "pay_date": date.today().isoformat(), "pocket_user_id": staff.id},
                    follow_redirects=False)
    assert r.status_code == 303
    card = svc.child_card(db, amir)
    assert card["balance"] == 0 and card["kind"] == "ok"
    assert cash.pocket_balance(db, site.id, staff.id) == pocket0 + Decimal(9500)
    assert podotchet.get_expected_balance(db, site.id, date.today())["expected"] == acc0   # на счёт не попадало
    f = db.query(CashFunding).filter(CashFunding.comment.like("%Капаров Амир тест-дт%")).one()
    assert f.source_transaction_id is not None and f.accountable_user_id == staff.id


def test_no_tariff_means_no_debt_text(db, site, staff):
    school = Organization(name="Школа тест-дт", type="school", parent_id=site.parent_id, site_id=site.id)
    db.add(school)
    db.flush()
    db.add(Student(organization_id=school.id, name="Ученик тест-дт", pin="9978", status="active"))
    db.flush()
    data = svc.children_list(db, school)
    assert data["tariff"] is None and data["count"] == 1
