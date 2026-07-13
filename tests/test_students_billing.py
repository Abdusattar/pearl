"""
Карточка ребёнка (/students/{id}/edit) разделена на два независимых
сохранения: личные данные (ФИО, группа) и оплата (тариф, доп.услуги).
Тест фиксирует контракт: каждый эндпоинт трогает только свою часть данных.
"""
import bcrypt

from app.models import Enrollment, Organization, Student, User


def _make_owner(db) -> User:
    org = db.query(Organization).filter(Organization.id == 4).first()
    user = User(
        name="__test_owner__",
        role="owner",
        organization_id=org.id,
        password_hash=bcrypt.hashpw(b"test-pass", bcrypt.gensalt()).decode(),
    )
    db.add(user)
    db.flush()
    return user


def _make_student(db) -> Student:
    student = Student(
        organization_id=4,
        name="__Тест Ребёнок__",
        pin="9990",
        status="active",
    )
    db.add(student)
    db.flush()
    db.add(Enrollment(student_id=student.id, group_id=7, start_date="2026-01-01"))
    db.flush()
    return student


def _login(client, user_id):
    resp = client.post("/login", data={"user_id": user_id, "password": "test-pass"})
    assert resp.status_code in (200, 302)


def test_personal_save_does_not_touch_billing(client, db):
    user = _make_owner(db)
    student = _make_student(db)
    _login(client, user.id)

    resp = client.post(
        f"/students/{student.id}/edit",
        data={"last_name": "__Новая__", "first_name": "__Фамилия__", "group_id": "9"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/students/{student.id}/edit?saved=personal"

    db.expire_all()
    student = db.query(Student).filter(Student.id == student.id).first()
    assert student.name == "__Новая__ __Фамилия__"
    assert student.extra is None  # оплата не тронута

    open_enrollment = db.query(Enrollment).filter(
        Enrollment.student_id == student.id, Enrollment.end_date.is_(None)
    ).first()
    assert open_enrollment.group_id == 9

    closed = db.query(Enrollment).filter(
        Enrollment.student_id == student.id, Enrollment.group_id == 7
    ).first()
    assert closed.end_date is not None  # старая запись группы закрыта, не удалена


def test_billing_save_does_not_touch_personal(client, db):
    user = _make_owner(db)
    student = _make_student(db)
    _login(client, user.id)

    resp = client.post(
        f"/students/{student.id}/billing",
        data={"discount_amount": "1000", "discount_reason": "__тест причина__"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/students/{student.id}/edit?saved=billing"

    db.expire_all()
    student = db.query(Student).filter(Student.id == student.id).first()
    assert float(student.discount_amount) == 1000.0
    assert student.discount_reason == "__тест причина__"
    assert student.name == "__Тест Ребёнок__"  # личные данные не тронуты

    open_enrollment = db.query(Enrollment).filter(
        Enrollment.student_id == student.id, Enrollment.end_date.is_(None)
    ).first()
    assert open_enrollment.group_id == 7  # группа не тронута
