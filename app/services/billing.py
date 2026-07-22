from calendar import monthrange
from datetime import date

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import Student, StudentService, Service, Charge, Transaction, Enrollment, Organization


def get_tuition_service(db: Session, organization_id: int) -> Service | None:
    """Базовая услуга «Обучение» объекта — цена редактируется на /services/,
    не захардкожена в коде (тариф будет расти). Применяется автоматически
    всем активным детям объекта, не через чекбокс StudentService."""
    return (
        db.query(Service)
        .filter(Service.organization_id == organization_id, Service.is_tuition.is_(True), Service.deleted_at.is_(None))
        .first()
    )


def _tuition_fee(db: Session, student: Student) -> float:
    tuition_service = get_tuition_service(db, student.organization_id)
    base = float(tuition_service.price) if tuition_service else 0.0
    if student.legacy_tariff_amount is not None:
        org = db.query(Organization).get(student.organization_id)
        if org and org.legacy_tariff_until and date.today() <= org.legacy_tariff_until:
            base = float(student.legacy_tariff_amount)
    discount = float(student.discount_amount or 0)
    return max(0.0, base - discount)


def _proration_factor(db: Session, student_id: int, period: date) -> float:
    """1.0 обычно. Меньше — если ребёнок впервые зачислен в этом же месяце (Enrollment
    ещё не было до начала периода): начисляем только за дни с даты старта до конца
    месяца, а не за весь месяц. Первое зачисление ищем по всем группам ребёнка —
    перевод между группами без разрыва не считается новым стартом."""
    first_start = db.query(func.min(Enrollment.start_date)).filter(
        Enrollment.student_id == student_id
    ).scalar()
    if not first_start or first_start <= period:
        return 1.0
    if first_start.year != period.year or first_start.month != period.month:
        return 1.0
    days_in_month = monthrange(period.year, period.month)[1]
    days_active = days_in_month - first_start.day + 1
    return max(0.0, min(1.0, days_active / days_in_month))


def _active_services(db: Session, student_id: int) -> list[StudentService]:
    return (
        db.query(StudentService)
        .filter(StudentService.student_id == student_id, StudentService.end_date.is_(None))
        .all()
    )


def generate_monthly_charges(db: Session) -> int:
    """Начисляет учёбу + подключённые услуги за текущий месяц каждому активному
    ребёнку — один раз в месяц. Без cron: вызывается при заходе на страницу.
    Статус "frozen" (место держится, ребёнок не ходит) — только процент от тарифа
    (Organization.frozen_discount_percent, настройка объекта, не константа), без
    доп.услуг и без пропорции по дате: ребёнок ими не пользуется, пока заморожен."""
    period = date.today().replace(day=1)

    already_charged = {
        sid for (sid,) in db.query(Charge.student_id)
        .filter(Charge.date == period, Charge.description == "Начисление за месяц")
        .distinct()
    }

    org_frozen_percent = dict(db.query(Organization.id, Organization.frozen_discount_percent).all())

    students = db.query(Student).filter(Student.status.in_(("active", "frozen"))).all()
    created = 0
    for student in students:
        if student.id in already_charged:
            continue

        tuition = _tuition_fee(db, student)  # уже с учётом скидки на тариф (Student.discount_amount)

        if student.status == "frozen":
            percent = float(org_frozen_percent.get(student.organization_id) or 0)
            total = round(tuition * percent / 100, 2)
        else:
            services = _active_services(db, student.id)
            services_total = sum(float(ss.service.price) for ss in services)
            factor = _proration_factor(db, student.id, period)
            total = round((tuition + services_total) * factor, 2)
        if total <= 0:
            continue

        # description намеренно фиксированная строка, не расшифровка состава —
        # именно по ней already_charged проверяет идемпотентность выше;
        # причина скидки видна на карточке ребёнка (Student.discount_reason),
        # а не в этом тексте.
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
