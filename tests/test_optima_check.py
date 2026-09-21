"""Ответ Optima на check: отдельные поля ФИО/объект/группа (просьба банка 21.09)."""
from datetime import date

from app.models import Enrollment, Group, Organization, Student


def _kid(db, pin, name):
    org = Organization(name="Школа тест-оп", type="school")
    db.add(org)
    db.flush()
    g = Group(organization_id=org.id, name="1 класс тест-оп", type="class")
    db.add(g)
    db.flush()
    s = Student(organization_id=org.id, name=name, pin=pin, status="active")
    db.add(s)
    db.flush()
    db.add(Enrollment(student_id=s.id, group_id=g.id, start_date=date(2026, 9, 1)))
    db.flush()
    return s


def test_test_pin_gets_separate_fields(client, db):
    _kid(db, "9888", "Тестов & Тестомир")
    r = client.get("/optima/payment", params={"command": "check", "txn_id": "t-9888", "account": "9888", "sum": "15.00"})
    body = r.text
    assert "<result>0</result>" in body
    assert '<field1 name="fio">Тестов &amp; Тестомир</field1>' in body
    assert '<field2 name="organization">Школа тест-оп Жемчужина</field2>' in body
    assert '<field3 name="group">1 класс тест-оп</field3>' in body
    assert "группа 1 класс тест-оп</comment>" in body          # comment прежний


def test_live_pin_answer_unchanged(client, db):
    _kid(db, "0888", "Живой Ребёнок")
    r = client.get("/optima/payment", params={"command": "check", "txn_id": "t-0888", "account": "0888", "sum": "15.00"})
    assert "<result>0</result>" in r.text and "<fields>" not in r.text
