"""Выдача зарплаты по каждому сотруднику (07.09).

Раньше ФОТ проводился одной суммой на всех — по такой проводке не видно, кто
сколько получил и кому ещё должны. А выдают по факту: аванс, часть, неполный
месяц. Теперь каждая выдача — отдельный расход с `employee_id` и `period`
(месяц, за который выдано), а «сколько выдано» собирается из этих расходов,
а не из окладов.

Оклад остаётся тем, что бизнес должен за месяц; выдача — тем, что реально
вышло из кассы. Разница между ними и есть долг перед сотрудником.
"""
from datetime import date as date_cls
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Employee, Transaction

ZERO = Decimal("0")

# Категория «ФОТ» (родитель — «Ежемесячные расходы»). Заводится сидом, на
# проде id 190; ищем по имени, чтобы не зависеть от конкретного id в базе.
PAYROLL_CATEGORY_NAME = "ФОТ"


def payroll_category_id(db: Session) -> int | None:
    from app.models import ExpenseCategory
    row = (
        db.query(ExpenseCategory.id)
        .filter(ExpenseCategory.name == PAYROLL_CATEGORY_NAME)
        .first()
    )
    return row[0] if row else None


def month_start(d: date_cls | None = None) -> date_cls:
    d = d or date_cls.today()
    return d.replace(day=1)


def issued_by_employee(db: Session, organization_id: int, period: date_cls) -> dict[int, Decimal]:
    """employee_id -> сколько уже выдано этому человеку за месяц."""
    rows = (
        db.query(Transaction.employee_id, func.coalesce(func.sum(Transaction.amount), 0))
        .filter(
            Transaction.organization_id == organization_id,
            Transaction.type == "expense",
            Transaction.employee_id.isnot(None),
            Transaction.period == period,
            Transaction.deleted_at.is_(None),
        )
        .group_by(Transaction.employee_id)
        .all()
    )
    return {emp_id: Decimal(total) for emp_id, total in rows}


def month_sheet(db: Session, organization_id: int, period: date_cls) -> list[dict]:
    """Ведомость за месяц: по строке на активного сотрудника — оклад, сколько
    уже выдано, сколько осталось."""
    employees = (
        db.query(Employee)
        .filter(Employee.organization_id == organization_id, Employee.status == "active")
        .order_by(Employee.full_name)
        .all()
    )
    issued = issued_by_employee(db, organization_id, period)
    sheet = []
    for e in employees:
        salary = Decimal(e.salary or 0)
        paid = issued.get(e.id, ZERO)
        sheet.append({
            "employee": e,
            "salary": salary,
            "issued": paid,
            "left": max(ZERO, salary - paid),
        })
    return sheet


def totals(sheet: list[dict]) -> dict:
    accrued = sum((r["salary"] for r in sheet), ZERO)
    issued = sum((r["issued"] for r in sheet), ZERO)
    return {"accrued": accrued, "issued": issued, "left": max(ZERO, accrued - issued)}


def pay(db: Session, *, organization_id: int, employee: Employee, amount: Decimal,
        period: date_cls, on_date: date_cls, user_id: int) -> Transaction:
    """Одна выдача. Обычный расход из кассы — `paid_directly=False`, значит
    списывается с подотчёта объекта, как любой наличный расход."""
    tx = Transaction(
        organization_id=organization_id,
        type="expense",
        amount=amount,
        category_id=payroll_category_id(db),
        description=f"Зарплата — {employee.full_name}",
        date=on_date,
        period=period,
        employee_id=employee.id,
        paid_directly=False,
        created_by=user_id,
    )
    db.add(tx)
    return tx
