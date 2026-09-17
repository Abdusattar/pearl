"""Зарплата в новом входе (макет 4в, утверждён 17.09).

Ведомость за месяц, за который платят (не за месяц выдачи): зарплату за август
дают 10 сентября. Выдача — расход с `employee_id` и `period`, как в старом входе
(`payroll.py`), плюс карман или счёт, чтобы Касса видела, откуда ушли деньги.
Расход пишется на площадку: карманы и касса считаются по площадке, а чей
сотрудник — видно по самому сотруднику.

Остатка «оклад минус выдано» нет (Махабат 17.09): из-за отпусков сумма у всех
разная, выданное — окончательное. Оклад в ведомости — справка, а не долг;
сигнал после дня зарплаты — только по тем, кому не выдано ничего.

Соцфонд (Махабат 17.09): у тех, кому платят на карту, банк при переводе
удерживает соцфонд со счёта объекта — сумма у каждого своя. Это отдельная
строка у человека (категория расхода «Соцфонд»), в «Выдано» не входит: выдано
— то, что человек получил. Удержание за еду деньгами не движется, не пишется.

Видимость (решение 14.09): школьную ведомость ведёт Айжан, Махабат её не
видит, хотя к Школе доступ у неё есть.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.dependencies import get_accessible_orgs
from app.models import AuditLog, Employee, ExpenseCategory, Organization, Transaction, User
from app.services.payroll import payroll_category_id
from app.services.purchases import audit, site_orgs

ZERO = Decimal("0")
PAY_DAY = 10                    # день зарплаты; потом в Настройки
ODD_AMOUNT = Decimal("100")     # выдача меньше — скорее опечатка (3 и 50 сом 16.09)
SCHOOL_PAYROLL_ROLES = ("owner", "founder", "director")
SOCFOND = "Соцфонд"


def socfond_category_id(db: Session, create: bool = False) -> int | None:
    row = db.query(ExpenseCategory).filter(ExpenseCategory.name == SOCFOND).first()
    if row is None and create:
        fot = db.query(ExpenseCategory).filter(ExpenseCategory.name == "ФОТ").first()
        row = ExpenseCategory(name=SOCFOND, parent_id=fot.parent_id if fot else None)
        db.add(row)
        db.flush()
    return row.id if row else None


def prev_month(today: date | None = None) -> date:
    today = today or date.today()
    return (today.replace(day=1) - timedelta(days=1)).replace(day=1)


def month_choices(today: date | None = None) -> list[date]:
    this = (today or date.today()).replace(day=1)
    prev = prev_month(today)
    return [prev_month(prev), prev, this]


def payroll_orgs(db: Session, user: User, site_org_id: int) -> list[Organization]:
    allowed = {o.id for o in get_accessible_orgs(user, db)}
    return [o for o in site_orgs(db, site_org_id)
            if o.id in allowed and (o.type != "school" or user.role in SCHOOL_PAYROLL_ROLES)]


def sheet(db: Session, orgs: list[Organization], period: date, today: date | None = None) -> dict:
    today = today or date.today()
    org_ids = [o.id for o in orgs]
    employees = (db.query(Employee).filter(Employee.organization_id.in_(org_ids), Employee.status == "active")
                 .order_by(Employee.full_name).all()) if org_ids else []
    ids = [e.id for e in employees]
    pays = (db.query(Transaction).filter(Transaction.employee_id.in_(ids), Transaction.period == period,
                                         Transaction.type == "expense", Transaction.deleted_at.is_(None))
            .order_by(Transaction.date, Transaction.id).all()) if ids else []
    by_emp: dict[int, list[Transaction]] = {}
    for t in pays:
        by_emp.setdefault(t.employee_id, []).append(t)
    names = {o.id: o.name for o in orgs}
    soc_id = socfond_category_id(db)
    rows = []
    for e in employees:
        mine = by_emp.get(e.id, [])
        soc = sum((Decimal(t.amount) for t in mine if soc_id and t.category_id == soc_id), ZERO)
        issued = sum((Decimal(t.amount) for t in mine), ZERO) - soc
        salary = Decimal(e.salary or 0)
        rows.append({"employee": e, "org": names.get(e.organization_id) if len(orgs) > 1 else None,
                     "salary": salary, "issued": issued, "socfond": soc,
                     "pays": [_pay_row(db, t, soc_id) for t in mine]})
    salary_total = sum((r["salary"] for r in rows), ZERO)
    issued_total = sum((r["issued"] for r in rows), ZERO)
    unpaid = sum(1 for r in rows if not r["issued"])
    payday = (period.replace(day=28) + timedelta(days=4)).replace(day=PAY_DAY)   # 10-е следующего месяца
    return {"rows": rows, "salary": salary_total, "issued": issued_total,
            "socfond": sum((r["socfond"] for r in rows), ZERO),
            "unpaid": unpaid, "payday": payday, "late": today > payday and unpaid > 0}


def _pay_row(db: Session, t: Transaction, soc_id: int | None = None) -> dict:
    if soc_id and t.category_id == soc_id:
        org = db.get(Organization, t.account_org_id) if t.account_org_id else None
        return {"id": t.id, "date": t.date, "amount": Decimal(t.amount), "odd": False,
                "source": f"соцфонд, со счёта {org.name}" if org else "соцфонд"}
    if t.paid_directly:
        org = db.get(Organization, t.account_org_id) if t.account_org_id else None
        source = f"на карту, со счёта {org.name}" if org else "на карту"
    else:
        u = db.get(User, t.paid_from_user_id or t.created_by) if (t.paid_from_user_id or t.created_by) else None
        source = f"на руки, из кармана {u.name}" if u else "на руки"
    return {"id": t.id, "date": t.date, "amount": Decimal(t.amount), "source": source,
            "odd": Decimal(t.amount) < ODD_AMOUNT}


def pay(db: Session, *, user: User, site_org_id: int, employee: Employee, amount: Decimal, period: date, d: date,
        pocket_user_id: int | None, account_org_id: int | None, socfond: bool = False) -> Transaction:
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    from_account = account_org_id is not None
    if socfond and not from_account:
        raise ValueError("Соцфонд уходит со счёта")
    label = "Соцфонд" if socfond else "Зарплата"
    tx = Transaction(
        organization_id=site_org_id, type="expense", amount=amount,
        category_id=socfond_category_id(db, create=True) if socfond else payroll_category_id(db),
        description=f"{label} — {employee.full_name}", date=d, period=period, employee_id=employee.id,
        paid_directly=from_account, account_org_id=account_org_id if from_account else None,
        paid_from_user_id=None if from_account else pocket_user_id, created_by=user.id,
    )
    db.add(tx)
    db.flush()
    audit(db, "transaction", tx.id, "insert", user.id,
          {"kind": "socfond" if socfond else "salary", "employee": employee.id, "period": period.isoformat(), "amount": float(amount),
           "pocket": tx.paid_from_user_id, "account": tx.account_org_id})
    return tx


def remove(db: Session, *, user: User, tx: Transaction) -> None:
    tx.deleted_at = datetime.now()
    db.add(AuditLog(entity_type="transaction", entity_id=tx.id, action="delete", user_id=user.id,
                    old_data={"kind": "salary", "employee": tx.employee_id, "amount": float(tx.amount),
                              "date": tx.date.isoformat(), "period": tx.period.isoformat() if tx.period else None}))

