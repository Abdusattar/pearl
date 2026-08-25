"""Оплата разовых услуг (канцелярия и т.п., 25.08) — Service.is_recurring=False.

Не привязана к месяцу, не проходит через generate_monthly_charges. Отметка
оплаты — одно действие, создающее сразу три записи, которые должны жить и
умирать вместе:
  Charge(student, amount)          — начисление
  Transaction(income, student, amount, service_id, charge_id)  — оплата,
    гасит начисление (баланс ребёнка не меняется, см. billing.get_balances)
  CashFunding(direct_cash, source_transaction_id)              — деньги
    физически на руках → сразу в подотчёт (app/services/podotchet.py)

Получатель наличных (кто становится подотчётным) — Organization.cash_recipient_user_id
(настройка на /settings/), не роль и не обязательно тот, кто нажал кнопку —
в жизни деньги всегда попадают одному конкретному человеку (25.08).
"""
from datetime import date as date_cls
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import CashFunding, Charge, Organization, Service, Student, Transaction, User


def academic_year_start(as_of: date_cls) -> date_cls:
    """Учебный год начинается 1 сентября — до этой даты считаем прошлым годом."""
    year = as_of.year if as_of.month >= 9 else as_of.year - 1
    return date_cls(year, 9, 1)


def academic_year_label(as_of: date_cls) -> str:
    start = academic_year_start(as_of)
    return f"{start.year}/{str(start.year + 1)[2:]}"


def get_one_time_service_status(db: Session, service: Service, as_of: date_cls) -> dict:
    """Живой счётчик "N из M оплатили" за текущий учебный год — не хранится,
    считается заново при каждом обращении к /services/."""
    year_start = academic_year_start(as_of)

    paid_student_ids = {
        sid for (sid,) in db.query(Transaction.student_id).filter(
            Transaction.service_id == service.id, Transaction.type == "income",
            Transaction.date >= year_start, Transaction.deleted_at.is_(None),
        ).all()
    }
    paid_sum = Decimal(db.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.service_id == service.id, Transaction.type == "income",
        Transaction.date >= year_start, Transaction.deleted_at.is_(None),
    ).scalar())

    denominator = db.query(Student.id).filter(
        Student.organization_id == service.organization_id,
        Student.status.in_(("active", "frozen")),
    ).count()

    return {
        "paid_count": len(paid_student_ids),
        "denominator": denominator,
        "paid_sum": paid_sum,
        "expected_sum": Decimal(service.price) * denominator,
        "year_label": academic_year_label(as_of),
    }


def create_one_time_payment(
    db: Session, service: Service, student: Student, amount: float,
    date: date_cls, comment: str | None, actor: User,
) -> Transaction:
    org = db.query(Organization).filter(Organization.id == student.organization_id).first()
    recipient_id = (org.cash_recipient_user_id if org else None) or actor.id

    year_label = academic_year_label(date)
    charge = Charge(
        student_id=student.id, amount=amount,
        description=f"{service.name} — {year_label}", date=date,
    )
    db.add(charge)
    db.flush()

    txn = Transaction(
        organization_id=student.organization_id, type="income", amount=amount,
        student_id=student.id, description=service.name, date=date,
        service_id=service.id, charge_id=charge.id, created_by=actor.id,
    )
    db.add(txn)
    db.flush()

    db.add(CashFunding(
        organization_id=student.organization_id, source_type="direct_cash",
        amount=amount, date=date, taken_by=recipient_id, accountable_user_id=recipient_id,
        source_transaction_id=txn.id,
        comment=(comment.strip() if comment and comment.strip() else f"{service.name} — {student.name}"),
        created_by=actor.id,
    ))
    db.commit()
    return txn


def delete_one_time_payment(db: Session, txn: Transaction) -> bool:
    """Откатывает оплату разовой услуги — все три записи вместе, не только
    приход (иначе доход останется в балансе, а деньги пропадут из подотчёта
    молча). Возвращает False, если это не оплата разовой услуги (защита от
    удаления обычного дохода через этот путь)."""
    if txn.service_id is None:
        return False

    txn.deleted_at = func.now()

    if txn.charge_id:
        charge = db.query(Charge).filter(Charge.id == txn.charge_id).first()
        if charge:
            charge.deleted_at = func.now()

    funding = db.query(CashFunding).filter(
        CashFunding.source_transaction_id == txn.id, CashFunding.deleted_at.is_(None),
    ).first()
    if funding:
        funding.deleted_at = func.now()

    db.commit()
    return True
