"""Дети: добавление с защитой от дублей, скидка, статус, группа (16.09)."""
from datetime import date

import pytest

from app.models import AuditLog, Enrollment, Group, Organization, Service, Student, User
from app.routers import new_buy as buy_router
from app.routers import new_children as ch_router
from app.services import children as svc


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень дд", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-дд", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    db.add(Service(organization_id=sadik.id, name="Тариф тест-дд", price=10000, is_tuition=True, is_recurring=True))
    db.flush()
    return sadik


@pytest.fixture()
def staff(db, site, monkeypatch):
    u = User(name="Махабат тест-дд", role="staff", organization_id=site.id)
    db.add(u)
    db.flush()
    monkeypatch.setattr(ch_router, "get_current_user", lambda request, db: u)
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: site)
    monkeypatch.setattr(ch_router, "get_accessible_orgs", lambda user, db, all_orgs=None: [site])
    monkeypatch.setattr(ch_router, "generate_monthly_charges", lambda db: 0)
    return u


@pytest.fixture()
def group(db, site):
    g = Group(organization_id=site.id, name="Средняя тест-дд", type="kindergarten_group")
    db.add(g)
    db.flush()
    return g


def _add(client, site, group, **kw):
    data = {"org_id": site.id, "last_name": "Капаров", "first_name": "Амир", "patronymic": "", "group_id": group.id,
            "parent_name": "Капарова Айгуль", "parent_contact": "0700", "inn": "", "start": date.today().isoformat()}
    data.update(kw)
    return client.post("/new/children/add", data=data, follow_redirects=False)


def test_add_child_then_duplicate_is_a_question(client, db, site, staff, group):
    r = _add(client, site, group)
    assert r.status_code == 303, r.text[:300]
    s = db.query(Student).filter_by(last_name="Капаров", first_name="Амир", organization_id=site.id).one()
    assert s.pin and s.status == "active" and s.parent_name == "Капарова Айгуль"
    assert db.query(Enrollment).filter_by(student_id=s.id, group_id=group.id).count() == 1
    # тот же ребёнок ещё раз (с опечаткой) — вопрос, не вторая карточка
    r = _add(client, site, group, first_name="Амирр")
    assert r.status_code == 200 and "уже есть" in r.text and "Это другой ребёнок?" in r.text
    assert db.query(Student).filter(Student.last_name == "Капаров", Student.organization_id == site.id).count() == 1
    r = _add(client, site, group, first_name="Амирр", dup_ok="1")
    assert r.status_code == 303
    assert db.query(Student).filter(Student.last_name == "Капаров", Student.organization_id == site.id).count() == 2


def test_same_inn_is_a_duplicate(client, db, site, staff, group):
    _add(client, site, group, inn="20101234567890")
    r = _add(client, site, group, last_name="Другой", first_name="Мальчик", inn="20101234567890")
    assert r.status_code == 200 and "тот же ИНН" in r.text


def test_discount_needs_reason_and_is_audited(client, db, site, staff, group):
    _add(client, site, group)
    s = db.query(Student).filter_by(last_name="Капаров", organization_id=site.id).one()
    r = client.post(f"/new/children/{s.id}/discount", data={"amount": "500", "reason": ""}, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
    r = client.post(f"/new/children/{s.id}/discount", data={"amount": "500", "reason": "второй ребёнок"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=3" in r.headers["location"]
    db.expire_all()
    assert float(s.discount_amount) == 500 and s.discount_reason == "второй ребёнок" and s.discount_set_by == staff.id
    assert db.query(AuditLog).filter_by(entity_type="student_discount", entity_id=s.id).count() == 1
    r = client.post(f"/new/children/{s.id}/discount", data={"amount": "99999", "reason": "x"}, follow_redirects=False)
    assert "err=" in r.headers["location"]


def test_freeze_leave_return(client, db, site, staff, group):
    _add(client, site, group)
    s = db.query(Student).filter_by(last_name="Капаров", organization_id=site.id).one()
    r = client.post(f"/new/children/{s.id}/status", data={"status": "frozen", "on_date": date.today().isoformat(), "reason": "уехали"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert s.status == "frozen"
    assert db.query(Enrollment).filter_by(student_id=s.id, end_date=None).count() == 1   # группа держится
    r = client.post(f"/new/children/{s.id}/status", data={"status": "inactive", "on_date": date.today().isoformat(), "reason": ""}, follow_redirects=False)
    db.expire_all()
    assert s.status == "inactive"
    assert db.query(Enrollment).filter_by(student_id=s.id, end_date=None).count() == 0   # группа закрыта
    r = client.post(f"/new/children/{s.id}/status", data={"status": "active"}, follow_redirects=False)
    db.expire_all()
    assert s.status == "active"
    assert db.query(AuditLog).filter_by(entity_type="student_status", entity_id=s.id).count() == 3


def test_move_group_keeps_history(client, db, site, staff, group):
    _add(client, site, group)
    s = db.query(Student).filter_by(last_name="Капаров", organization_id=site.id).one()
    g2 = Group(organization_id=site.id, name="Старшая тест-дд", type="kindergarten_group")
    db.add(g2)
    db.flush()
    r = client.post(f"/new/children/{s.id}/group", data={"group_id": g2.id, "on_date": date.today().isoformat()}, follow_redirects=False)
    assert r.status_code == 303
    enr = db.query(Enrollment).filter_by(student_id=s.id).order_by(Enrollment.id).all()
    assert len(enr) == 2 and enr[0].end_date == date.today() and enr[1].group_id == g2.id and enr[1].end_date is None
    page = client.get(f"/new/children/{s.id}")
    assert page.status_code == 200 and "Старшая тест-дд" in page.text
