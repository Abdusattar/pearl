"""Настройки нового входа и сотрудники с окладами по месяцам (21.09)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import AppSetting, Charge, Employee, EmployeeSalary, Organization, Service, Student, Transaction, User
from app.routers import new_buy as buy_router
from app.routers import new_salary as salary_router
from app.routers import new_settings as settings_router
from app.services import billing, kitchen, rules, salary
from app.services import settings_view as sv


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень нс", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-нс", type="kindergarten", parent_id=root.id, frozen_discount_percent=50)
    db.add(sadik)
    db.flush()
    return sadik


@pytest.fixture()
def owner(db, site, monkeypatch):
    u = User(name="Владелец тест-нс", role="owner", organization_id=site.id)
    db.add(u)
    db.flush()
    for m in (settings_router, salary_router):
        monkeypatch.setattr(m, "get_current_user", lambda request, db: u)
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: site)
    return u


def test_only_owner_opens_settings(client, db, site, owner, monkeypatch):
    assert client.get("/new/settings").status_code == 200
    staff = User(name="Учётчик тест-нс", role="staff", organization_id=site.id)
    db.add(staff)
    db.flush()
    monkeypatch.setattr(settings_router, "get_current_user", lambda request, db: staff)
    r = client.get("/new/settings", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/new/today"


def test_rules_have_defaults_and_change(client, db, site, owner):
    assert rules.debt_old_days(db) == 30 and rules.pocket_delta_threshold(db) == 500
    r = client.post("/new/settings/rule", data={"key": "debt_old_days", "value": "45"}, follow_redirects=False)
    assert "saved=" in r.headers["location"]
    assert rules.debt_old_days(db) == 45
    r = client.post("/new/settings/rule", data={"key": "pay_day", "value": "40"}, follow_redirects=False)
    assert "err=" in r.headers["location"] and rules.pay_day(db) == 10
    client.post("/new/settings/rule", data={"key": "kitchen_weekdays", "day": ["0", "1", "2", "3", "4", "5"]})
    assert rules.kitchen_weekdays(db) == {0, 1, 2, 3, 4, 5}


def test_kitchen_weekdays_rule_drives_missing_days(db, site, owner):
    rules.put(db, user=owner, key="kitchen_weekdays", value=[0])
    assert all(d.weekday() == 0 for d in kitchen.missing_days(db, site.id))


def test_school_tariff_now_and_pending(client, db, site, owner):
    this = date.today().replace(day=1)
    nxt = (this.replace(day=28) + timedelta(days=4)).replace(day=1)
    r = client.post(f"/new/settings/tariff/{site.id}", data={"price": "7 000", "month": nxt.strftime("%Y-%m")},
                    follow_redirects=False)
    assert "saved=" in r.headers["location"]
    assert sv.tuition(db, site.id) is None                       # ещё не включился
    assert rules.pending_tariffs(db)[0]["price"] == 7000
    rules.apply_pending_tariffs(db, nxt)                          # начало следующего месяца
    t = sv.tuition(db, site.id)
    assert t is not None and Decimal(t.price) == 7000 and rules.pending_tariffs(db) == []


def test_tariff_current_month_blocked_after_charges(client, db, site, owner):
    this = date.today().replace(day=1)
    db.add(Service(organization_id=site.id, name="Обучение", price=10000, is_tuition=True, is_recurring=True))
    kid = Student(organization_id=site.id, name="Ребёнок тест-нс", pin="8971", status="active")
    db.add(kid)
    db.flush()
    db.add(Charge(student_id=kid.id, amount=10000, description="Начисление за месяц", date=this))
    db.flush()
    assert this not in sv.tariff_months(db, site.id)
    r = client.post(f"/new/settings/tariff/{site.id}", data={"price": "12000", "month": this.strftime("%Y-%m")},
                    follow_redirects=False)
    assert "err=" in r.headers["location"] and Decimal(sv.tuition(db, site.id).price) == 10000


def test_frozen_and_holder(client, db, site, owner):
    client.post("/new/settings/frozen", data={"org_id": site.id, "percent": "40"})
    client.post("/new/settings/holder", data={"org_id": site.id, "user_id": owner.id})
    db.refresh(site)
    assert float(site.frozen_discount_percent) == 40 and site.cash_recipient_user_id == owner.id


def test_salary_change_keeps_past_sheet_and_fired_stays(client, db, site, owner):
    this = date.today().replace(day=1)
    prev = salary.prev_month(this)
    e = Employee(organization_id=site.id, full_name="Айгерим тест-нс", salary=30000, status="active")
    db.add(e)
    db.flush()
    r = client.post(f"/new/salary/staff/{e.id}/salary", data={"salary": "35 000", "month": this.strftime("%Y-%m")},
                    follow_redirects=False)
    assert "saved=" in r.headers["location"]
    past = {x["employee"].id: x["salary"] for x in salary.sheet(db, [site], prev)["rows"]}
    now = {x["employee"].id: x["salary"] for x in salary.sheet(db, [site], this)["rows"]}
    assert past[e.id] == Decimal(30000) and now[e.id] == Decimal(35000)
    client.post(f"/new/salary/staff/{e.id}/end", data={"ended": (this - timedelta(days=1)).isoformat()})
    assert e.id in {x["employee"].id for x in salary.sheet(db, [site], prev)["rows"]}      # прошлый месяц — был
    assert e.id not in {x["employee"].id for x in salary.sheet(db, [site], this)["rows"]}  # этот — уже нет


def test_add_employee_and_page(client, db, site, owner):
    r = client.post("/new/salary/staff", data={"name": "Гульзат тест-нс", "role": "повар", "salary": "28000",
                                               "started": date.today().isoformat()}, follow_redirects=False)
    assert "saved=" in r.headers["location"]
    e = db.query(Employee).filter_by(full_name="Гульзат тест-нс").one()
    assert e.started_on == date.today() and Decimal(e.salary) == 28000
    page = client.get("/new/salary/staff")
    assert page.status_code == 200 and "Гульзат тест-нс" in page.text and "/employees" not in page.text.split("<main")[1]


def test_withholding_uses_rates(db):
    base = salary.withholding_from_card(Decimal(25985))
    same = salary.withholding_from_card(Decimal(25985), {"soc": Decimal("0.10"), "tax": Decimal("0.10"), "deduction": Decimal(650)})
    assert base == same and base["gross"] == 32000
