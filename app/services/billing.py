from datetime import date

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import Student, StudentService, Service, Charge, Transaction


def _tuition_fee(student: Student) -> float:
    if student.extra and student.extra.get("monthly_fee"):
        return float(student.extra["monthly_fee"])
    return 0.0


def _active_services(db: Session, student_id: int) -> list[StudentService]:
    return (
        db.query(StudentService)
        .filter(StudentService.student_id == student_id, StudentService.end_date.is_(None))
        .all()
    )


def generate_monthly_charges(db: Session) -> int:
    """Начисляет учёбу + подключённые услуги за текущий месяц каждому активному
    ребёнку — один раз в месяц. Без cron: вызывается при заходе на страницу."""
    period = date.today().replace(day=1)

    already_charged = {
        sid for (sid,) in db.query(Charge.student_id)
        .filter(Charge.date == period, Charge.description == "Начисление за месяц")
        .distinct()
    }

    students = db.query(Student).filter(Student.status == "active").all()
    created = 0
    for student in students:
        if student.id in already_charged:
            continue

        tuition = _tuition_fee(student)
        services = _active_services(db, student.id)
        services_total = sum(float(ss.service.price) for ss in services)
        total = tuition + services_total
        if total <= 0:
            continue

        parts = []
        if tuition:
            parts.append(f"учёба {tuition:,.0f}".replace(",", " "))
        for ss in services:
            parts.append(f"{ss.service.name} {float(ss.service.price):,.0f}".replace(",", " "))

        db.add(Charge(
            student_id=student.id,
            amount=total,
            description="Начисление за месяц",
            date=period,
        ))
        created += 1

    if created:
        db.flush()
    return created


def get_balance(db: Session, student_id: int) -> float:
    """Долг (>0) или переплата (<0) ребёнка: начисления минус оплаты."""
    charged = db.query(func.coalesce(func.sum(Charge.amount), 0)).filter(
        Charge.student_id == student_id
    ).scalar()
    paid = db.query(func.coalesce(func.sum(Transaction.amount), 0)).filter(
        Transaction.student_id == student_id, Transaction.type == "income"
    ).scalar()
    return float(charged) - float(paid)


def get_ledger(db: Session, student_id: int) -> list[dict]:
    """Единая лента начислений и оплат ребёнка, по дате — новые сверху.
    Начисление увеличивает долг, оплата уменьшает — на карточке нужно видеть
    и то, и то вместе, а не только итоговую цифру баланса."""
    charges = (
        db.query(Charge)
        .filter(Charge.student_id == student_id)
        .all()
    )
    payments = (
        db.query(Transaction)
        .filter(
            Transaction.student_id == student_id,
            Transaction.type == "income",
            Transaction.deleted_at.is_(None),
        )
        .all()
    )
    ledger = [
        {"date": c.date, "amount": float(c.amount), "description": c.description, "kind": "charge"}
        for c in charges
    ] + [
        {
            "date": p.date,
            "amount": float(p.amount),
            "description": p.description,
            "kind": "payment",
            "source": "Optima" if p.external_txn_id else "Вручную",
        }
        for p in payments
    ]
    ledger.sort(key=lambda row: row["date"], reverse=True)
    return ledger


def get_balances(db: Session, student_ids: list[int]) -> dict[int, float]:
    if not student_ids:
        return {}
    charges = dict(
        db.query(Charge.student_id, func.coalesce(func.sum(Charge.amount), 0))
        .filter(Charge.student_id.in_(student_ids))
        .group_by(Charge.student_id)
        .all()
    )
    payments = dict(
        db.query(Transaction.student_id, func.coalesce(func.sum(Transaction.amount), 0))
        .filter(Transaction.student_id.in_(student_ids), Transaction.type == "income")
        .group_by(Transaction.student_id)
        .all()
    )
    return {
        sid: float(charges.get(sid, 0)) - float(payments.get(sid, 0))
        for sid in student_ids
    }


def set_student_services(db: Session, student_id: int, service_ids: list[int]) -> None:
    """Синхронизирует подключённые услуги ребёнка со списком выбранных.
    Закрывает те, что сняли, открывает новые — история (start/end date) сохраняется."""
    current = _active_services(db, student_id)
    current_ids = {ss.service_id for ss in current}
    wanted_ids = set(service_ids)

    for ss in current:
        if ss.service_id not in wanted_ids:
            ss.end_date = date.today()

    for sid in wanted_ids - current_ids:
        db.add(StudentService(student_id=student_id, service_id=sid, start_date=date.today()))

    db.flush()
