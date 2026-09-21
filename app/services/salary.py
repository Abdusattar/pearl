"""Зарплата в новом входе (макет 4в, утверждён 17.09).

Ведомость за месяц, за который платят (не за месяц выдачи): зарплату за август
дают 10 сентября. Выдача — расход с `employee_id` и `period`, как в старом входе
(`payroll.py`), плюс карман или счёт, чтобы Касса видела, откуда ушли деньги.
Расход пишется на площадку: карманы и касса считаются по площадке, а чей
сотрудник — видно по самому сотруднику.

Остатка «оклад минус выдано» нет (Махабат 17.09): из-за отпусков сумма у всех
разная, выданное — окончательное. Оклад в ведомости — справка, а не долг;
сигнал после дня зарплаты — только по тем, кому не выдано ничего.

Соцфонд и подоходный (Махабат и владелец 17.09): у официально оформленных,
кому платят на карту, при переводе со счёта объекта уходят и удержания. Сумму
система считает от «на карту» и подставляет, человек проверяет и записывает. Это отдельная
строка у человека (категория расхода «Соцфонд»), в «Выдано» не входит: выдано
— то, что человек получил. Удержание за еду деньгами не движется, не пишется.

Видимость (решение 14.09): школьную ведомость ведёт Айжан, Махабат её не
видит, хотя к Школе доступ у неё есть.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.dependencies import get_accessible_orgs
from app.models import AuditLog, Employee, EmployeeSalary, ExpenseCategory, Organization, Transaction, User
from app.services import rules
from app.services.payroll import payroll_category_id
from app.services.purchases import audit, site_orgs

ZERO = Decimal("0")
PAY_DAY = 10                    # день зарплаты; потом в Настройки
ODD_AMOUNT = Decimal("100")     # выдача меньше — скорее опечатка (3 и 50 сом 16.09)
SCHOOL_PAYROLL_ROLES = ("owner", "founder", "director")
SOCFOND = "Соцфонд и подоходный"
# Удержания с официально оформленных (КР, проверено на ведомости августа:
# 25 985 на карту → 32 000 начислено → 6 015; 23 555 → 29 000 → 5 445).
# Потом в Настройки: софт пойдёт другим садикам.
SOCFOND_RATE = Decimal("0.10")        # соцфонд с работника
INCOME_TAX_RATE = Decimal("0.10")     # подоходный, с суммы после соцфонда и вычета
TAX_DEDUCTION = Decimal("650")        # стандартный вычет


def withholding_from_card(card: Decimal, rates: dict | None = None) -> dict | None:
    """Сколько банк удержал, если на карту пришло `card`. Обратный счёт:
    на карту = начислено − соцфонд − подоходный."""
    if card <= 0:
        return None
    soc_rate = rates["soc"] if rates else SOCFOND_RATE
    tax_rate = rates["tax"] if rates else INCOME_TAX_RATE
    deduction = rates["deduction"] if rates else TAX_DEDUCTION
    keep = (1 - soc_rate) * (1 - tax_rate)
    gross = ((card - tax_rate * deduction) / keep).quantize(Decimal("1"))
    soc = (gross * soc_rate).quantize(Decimal("1"))
    tax = gross - card - soc
    return {"gross": gross, "soc": soc, "tax": tax, "total": gross - card}


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



def staff_for_month(db: Session, org_ids: list[int], period: date) -> list[Employee]:
    """Кто работал в месяце ведомости: уволенный после начала месяца в ней остаётся,
    принятый после его конца — ещё нет (21.09: «Уволен» не стирает прошлое)."""
    if not org_ids:
        return []
    nxt = (period.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (db.query(Employee)
            .filter(Employee.organization_id.in_(org_ids),
                    or_(Employee.ended_on >= period, and_(Employee.ended_on.is_(None), Employee.status == "active")),
                    or_(Employee.started_on.is_(None), Employee.started_on < nxt))
            .order_by(Employee.full_name).all())


def salary_map(db: Session, ids: list[int], period: date) -> dict[int, Decimal]:
    """Оклад на месяц: последняя строка истории с from_month ≤ месяца; нет — Employee.salary."""
    out: dict[int, Decimal] = {}
    if not ids:
        return out
    for row in (db.query(EmployeeSalary).filter(EmployeeSalary.employee_id.in_(ids), EmployeeSalary.from_month <= period)
                .order_by(EmployeeSalary.from_month, EmployeeSalary.id).all()):
        out[row.employee_id] = Decimal(row.amount)
    return out

def sheet(db: Session, orgs: list[Organization], period: date, today: date | None = None) -> dict:
    today = today or date.today()
    org_ids = [o.id for o in orgs]
    employees = staff_for_month(db, org_ids, period)
    salaries = salary_map(db, [e.id for e in employees], period)
    rates = rules.tax_rates(db)
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
        salary = salaries.get(e.id, Decimal(e.salary or 0))
        card = sum((Decimal(t.amount) for t in mine if t.paid_directly and not (soc_id and t.category_id == soc_id)), ZERO)
        calc = withholding_from_card(card, rates) if not soc else None   # подсказка, пока удержание не записано
        rows.append({"employee": e, "org": names.get(e.organization_id) if len(orgs) > 1 else None,
                     "salary": salary, "issued": issued, "socfond": soc, "card": card, "calc": calc,
                     "pays": [_pay_row(db, t, soc_id) for t in mine]})
    salary_total = sum((r["salary"] for r in rows), ZERO)
    issued_total = sum((r["issued"] for r in rows), ZERO)
    unpaid = sum(1 for r in rows if not r["issued"])
    payday = (period.replace(day=28) + timedelta(days=4)).replace(day=rules.pay_day(db))   # из Настроек, по умолчанию 10-е
    return {"rows": rows, "salary": salary_total, "issued": issued_total,
            "socfond": sum((r["socfond"] for r in rows), ZERO),
            "unpaid": unpaid, "payday": payday, "late": today > payday and unpaid > 0}


def _pay_row(db: Session, t: Transaction, soc_id: int | None = None) -> dict:
    if soc_id and t.category_id == soc_id:
        org = db.get(Organization, t.account_org_id) if t.account_org_id else None
        return {"id": t.id, "date": t.date, "amount": Decimal(t.amount), "odd": False,
                "source": f"соцфонд и подоходный, со счёта {org.name}" if org else "соцфонд и подоходный"}
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
    label = "Соцфонд и подоходный" if socfond else "Зарплата"
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



# ── сотрудники и оклады (21.09, из старого /employees в Расходы → Зарплата) ──

def staff_list(db: Session, orgs: list[Organization]) -> list[dict]:
    """Работающие и уволенные за последние три месяца: оклад на сегодня, как платим."""
    org_ids = [o.id for o in orgs]
    if not org_ids:
        return []
    this = date.today().replace(day=1)
    since = prev_month(prev_month(this))
    emps = (db.query(Employee).filter(Employee.organization_id.in_(org_ids),
                                      or_(and_(Employee.ended_on.is_(None), Employee.status == "active"),
                                          Employee.ended_on >= since))
            .order_by(Employee.ended_on.isnot(None), Employee.full_name).all())
    sal = salary_map(db, [e.id for e in emps], this)
    last_pay = {}
    for t in (db.query(Transaction).filter(Transaction.employee_id.in_([e.id for e in emps]), Transaction.type == "expense",
                                           Transaction.deleted_at.is_(None)).order_by(Transaction.date).all() if emps else []):
        last_pay[t.employee_id] = t
    names = {o.id: o.name for o in orgs}
    out = []
    for e in emps:
        t = last_pay.get(e.id)
        how = ("на карту" if t.paid_directly else "на руки") if t is not None else ""
        hist = (db.query(EmployeeSalary).filter(EmployeeSalary.employee_id == e.id)
                .order_by(EmployeeSalary.from_month.desc()).all())
        out.append({"e": e, "salary": sal.get(e.id, Decimal(e.salary or 0)), "how": how,
                    "org": names.get(e.organization_id) if len(orgs) > 1 else None, "history": hist})
    return out


def add_employee(db: Session, *, user: User, org: Organization, name: str, role: str, salary_raw: str,
                 started: date | None) -> Employee:
    name, role = name.strip(), role.strip()
    if not name:
        raise ValueError("Имя обязательно")
    amount = _money_in(salary_raw, "Оклад")
    e = Employee(organization_id=org.id, full_name=name, role=role or None, salary=amount, status="active",
                 started_on=started, created_by=user.id)
    db.add(e)
    db.flush()
    audit(db, "employee", e.id, "insert", user.id, {"name": name, "salary": float(amount), "started": started.isoformat() if started else None})
    return e


def set_salary(db: Session, *, user: User, e: Employee, amount_raw: str, from_month: date) -> None:
    """Оклад с месяца: прошлые ведомости не меняются. С текущего месяца и раньше —
    ещё и Employee.salary (его читают старые экраны)."""
    amount = _money_in(amount_raw, "Оклад")
    if not db.query(EmployeeSalary.id).filter(EmployeeSalary.employee_id == e.id).first():
        # первая смена: прежний оклад — строкой «с начала», иначе прошлые месяцы возьмут новый
        db.add(EmployeeSalary(employee_id=e.id, amount=Decimal(e.salary or 0), from_month=date(2000, 1, 1),
                              created_by=user.id))
    db.add(EmployeeSalary(employee_id=e.id, amount=amount, from_month=from_month, created_by=user.id))
    if from_month <= date.today().replace(day=1):
        e.salary = amount
    audit(db, "employee_salary", e.id, "insert", user.id, {"amount": float(amount), "from": from_month.isoformat()})


def end_employee(db: Session, *, user: User, e: Employee, d: date) -> None:
    e.status, e.ended_on = "terminated", d
    audit(db, "employee", e.id, "update", user.id, {"ended_on": d.isoformat()})


def _money_in(raw: str, what: str) -> Decimal:
    try:
        v = Decimal((raw or "").replace(" ", "").replace(",", "."))
    except Exception:
        raise ValueError(f"{what} — числом, например 30 000")
    if v <= 0:
        raise ValueError(f"{what} — больше нуля")
    return v
