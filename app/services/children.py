"""Дети (новый вход, макет блок 5, принят 15.09): список по группам с долгом,
карточка с лентой событий и таблицей по месяцам, приём наличных в карман.

Баланс считается из начислений и оплат (billing), оплата ложится на самый
старый долг (решение владельца 15.09): поэтому у ребёнка видно не только
сумму, но и «за какие месяцы». Школа без тарифа показывает «тариф не
заведён», а не выдуманный долг.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models import CashFunding, Charge, Enrollment, Group, Organization, Student, Transaction, User
from app.services import billing
from app.services.purchases import audit

MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
              "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
MONTHLY = "Начисление за месяц"


def month_name(d: date) -> str:
    return MONTHS_NOM[d.month - 1]


def _allocate(charges: list[Charge], paid_total: float) -> list[dict]:
    """Оплаты ложатся на самые старые начисления. Возвращает начисления с остатком."""
    rows = []
    pool = paid_total
    for c in sorted(charges, key=lambda c: (c.date, c.id)):
        amt = float(c.amount)
        applied = min(amt, pool) if pool > 0 else 0.0
        pool -= applied
        rows.append({"charge": c, "amount": amt, "paid": applied, "left": round(amt - applied, 2)})
    return rows


def _status(alloc: list[dict], balance: float, today: date) -> tuple[str, str]:
    """Текст статуса и его вид: ok | debt | bad | over."""
    if balance < -0.5:
        return f"переплата {abs(balance):,.0f}".replace(",", " "), "over"
    unpaid = [r for r in alloc if r["left"] > 0.5]
    if not unpaid:
        cur = [r for r in alloc if r["charge"].date.replace(day=1) == today.replace(day=1)]
        return ("оплачен " + month_name(today)) if cur else "без долга", "ok"
    months = []
    for r in unpaid:
        m = month_name(r["charge"].date) if r["charge"].description == MONTHLY else (r["charge"].description or "услуга")
        if m not in months:
            months.append(m)
    old = any(r["charge"].date.replace(day=1) < today.replace(day=1) for r in unpaid)
    text = " и ".join(months) if len(months) <= 2 else f"{months[0]} и ещё {len(months) - 1}"
    return text, ("bad" if old else "debt")


def children_list(db: Session, org: Organization, only_debt: bool = False, q: str | None = None) -> dict:
    today = date.today()
    students = (db.query(Student)
                .filter(Student.organization_id == org.id, Student.status.in_(("active", "frozen")),
                        Student.deleted_at.is_(None))
                .order_by(Student.name).all())
    if q:
        ql = q.strip().lower()
        students = [s for s in students if ql in (s.name or "").lower() or ql in (s.pin or "")]
    ids = [s.id for s in students]
    balances = billing.get_balances(db, ids)
    charges_by = {}
    for c in (db.query(Charge).filter(Charge.student_id.in_(ids), Charge.deleted_at.is_(None)).all() if ids else []):
        charges_by.setdefault(c.student_id, []).append(c)
    paid_by = dict(db.query(Transaction.student_id, func.coalesce(func.sum(Transaction.amount), 0))
                   .filter(Transaction.student_id.in_(ids), Transaction.type == "income", Transaction.deleted_at.is_(None))
                   .group_by(Transaction.student_id).all()) if ids else {}
    group_by = {}
    for sid, gname in (db.query(Enrollment.student_id, Group.name).join(Group, Group.id == Enrollment.group_id)
                       .filter(Enrollment.student_id.in_(ids), Enrollment.end_date.is_(None)).all() if ids else []):
        group_by[sid] = gname

    rows = []
    debt_total = old_total = 0.0
    for s in students:
        bal = balances.get(s.id, 0.0)
        alloc = _allocate(charges_by.get(s.id, []), float(paid_by.get(s.id, 0)))
        status, kind = _status(alloc, bal, today)
        if bal > 0.5:
            debt_total += bal
            old_total += sum(r["left"] for r in alloc if r["charge"].date.replace(day=1) < today.replace(day=1))
        sub = []
        if s.status == "frozen":
            sub.append("заморожен")
        if s.discount_amount and float(s.discount_amount) > 0:
            sub.append(f"скидка {float(s.discount_amount):,.0f}".replace(",", " ") + (f", {s.discount_reason}" if s.discount_reason else ""))
        rows.append({"s": s, "balance": bal, "status": status, "kind": kind, "group": group_by.get(s.id, "Без группы"),
                     "sub": ", ".join(sub)})
    if only_debt:
        rows = [r for r in rows if r["balance"] > 0.5]
    groups = []
    for r in sorted(rows, key=lambda r: (r["group"] == "Без группы", r["group"], r["s"].name)):
        if not groups or groups[-1]["name"] != r["group"]:
            groups.append({"name": r["group"], "rows": [], "total": 0})
        groups[-1]["rows"].append(r)
    for g in groups:
        g["total"] = len([r for r in rows if r["group"] == g["name"]])
    return {"groups": groups, "count": len(students), "debt_total": debt_total, "old_total": old_total,
            "tariff": billing.get_tuition_service(db, org.id), "shown": len(rows)}


def child_card(db: Session, student: Student) -> dict:
    today = date.today()
    charges = db.query(Charge).filter(Charge.student_id == student.id, Charge.deleted_at.is_(None)).all()
    payments = (db.query(Transaction)
                .filter(Transaction.student_id == student.id, Transaction.type == "income", Transaction.deleted_at.is_(None))
                .order_by(Transaction.date.desc(), Transaction.id.desc()).all())
    paid_total = sum(float(p.amount) for p in payments)
    alloc = _allocate(charges, paid_total)
    balance = billing.get_balance(db, student.id)
    status, kind = _status(alloc, balance, today)
    events = []
    for c in charges:
        label = f"Начислено за {month_name(c.date)}" if c.description == MONTHLY else (c.description or "Начислено")
        events.append({"date": c.date, "text": label, "amount": float(c.amount), "kind": "charge"})
    for p in payments:
        src = "через банк, Optima" if p.external_txn_id else ("наличными" if not p.service_id else (p.description or "оплата"))
        events.append({"date": p.date, "text": f"Оплата {src}", "amount": float(p.amount), "kind": "payment"})
    if student.discount_amount and float(student.discount_amount) > 0 and student.discount_set_at:
        events.append({"date": student.discount_set_at.date(), "text": f"Скидка {float(student.discount_amount):,.0f} сом".replace(",", " ") + (f": {student.discount_reason}" if student.discount_reason else ""), "amount": None, "kind": "note"})
    enr = (db.query(Enrollment).filter(Enrollment.student_id == student.id).order_by(Enrollment.start_date.asc()).first())
    if enr:
        events.append({"date": enr.start_date, "text": f"Зачислен(а): {enr.group.name if enr.group else ''}", "amount": None, "kind": "note"})
    events.sort(key=lambda e: e["date"], reverse=True)

    months: dict[date, dict] = {}
    for r in alloc:
        c = r["charge"]
        key = c.date.replace(day=1)
        m = months.setdefault(key, {"period": key, "charged": 0.0, "paid": 0.0, "left": 0.0})
        m["charged"] += r["amount"]; m["paid"] += r["paid"]; m["left"] += r["left"]
    month_rows = sorted(months.values(), key=lambda m: m["period"], reverse=True)
    group = (db.query(Group.name).join(Enrollment, Enrollment.group_id == Group.id)
             .filter(Enrollment.student_id == student.id, Enrollment.end_date.is_(None)).first())
    return {"balance": balance, "status": status, "kind": kind, "events": events[:40], "months": month_rows,
            "group": group[0] if group else None,
            "tariff": billing.tuition_base_price(db, student) if billing.get_tuition_service(db, student.organization_id) else None,
            "overpaid": max(0.0, -balance)}


def find_similar(db: Session, org_id: int, name: str, inn: str | None = None) -> list[Student]:
    q = db.query(Student).filter(Student.organization_id == org_id, Student.deleted_at.is_(None))
    conds = [func.lower(Student.name) == name.strip().lower()]
    if inn:
        conds.append(Student.extra["inn"].astext == inn.strip())
    return q.filter(or_(*conds)).all()


def similar_children(db: Session, org_id: int, last_name: str, first_name: str, inn: str | None = None) -> list[dict]:
    """Похожие карточки: те же фамилия и имя в объекте (любой статус) или тот же ИНН.
    Решение владельца 15.09: вторая карточка только после «это другой ребёнок»."""
    from rapidfuzz import fuzz
    out = []
    key = f"{last_name.strip()} {first_name.strip()}".lower()
    for s in db.query(Student).filter(Student.organization_id == org_id, Student.deleted_at.is_(None)).all():
        s_key = f"{(s.last_name or '')} {(s.first_name or '')}".strip().lower() or (s.name or "").lower()
        same_inn = bool(inn and s.extra and s.extra.get("inn") == inn.strip())
        score = fuzz.ratio(key, s_key)
        if same_inn or score >= 88:
            group = (db.query(Group.name).join(Enrollment, Enrollment.group_id == Group.id)
                     .filter(Enrollment.student_id == s.id, Enrollment.end_date.is_(None)).first())
            out.append({"s": s, "group": group[0] if group else None, "why": "тот же ИНН" if same_inn else "похожее имя"})
    return out


def add_child(db: Session, *, user: User, org_id: int, last_name: str, first_name: str, patronymic: str,
              group_id: int | None, parent_name: str, parent_contact: str, inn: str | None,
              start: date | None = None) -> Student:
    from app.services.students import compose_name, get_next_free_pin
    student = Student(organization_id=org_id, name=compose_name(last_name, first_name, patronymic),
                      last_name=last_name.strip(), first_name=first_name.strip(),
                      patronymic=(patronymic or "").strip() or None, pin=get_next_free_pin(db), status="active",
                      parent_name=(parent_name or "").strip() or None, parent_contact=(parent_contact or "").strip() or None,
                      extra={"inn": inn.strip()} if inn and inn.strip() else None)
    db.add(student)
    db.flush()
    if group_id:
        db.add(Enrollment(student_id=student.id, group_id=group_id, start_date=start or date.today()))
    audit(db, "student", student.id, "insert", user.id, {"from": "new/children", "org": org_id, "group": group_id})
    return student


def set_discount(db: Session, *, user: User, student: Student, amount: float, reason: str) -> None:
    base = billing.tuition_base_price(db, student)
    if not (0 <= amount <= base):
        raise ValueError(f"Скидка от 0 до {base:,.0f} сом".replace(",", " "))
    if amount > 0 and not reason.strip():
        raise ValueError("Скидка без причины не ставится")
    old = float(student.discount_amount or 0)
    if amount != old or (amount > 0 and reason.strip() != (student.discount_reason or "")):
        audit(db, "student_discount", student.id, "update", user.id,
              {"old": {"amount": old, "reason": student.discount_reason}, "new": {"amount": amount, "reason": reason.strip() or None}})
        student.discount_set_by = user.id
        student.discount_set_at = datetime.now()
    student.discount_amount = amount
    student.discount_reason = reason.strip() or None


def set_status(db: Session, *, user: User, student: Student, status: str, d: date, reason: str | None) -> None:
    """active | frozen | inactive. Выбыл закрывает группу; заморозка группу держит.
    Начисление за текущий месяц не трогается: вышел хоть 15-го — платит месяц
    (правило владельца 09.09), прошлые месяцы неявки — заморозка."""
    if status not in ("active", "frozen", "inactive"):
        raise ValueError("Неизвестный статус")
    old = student.status
    student.status = status
    current = db.query(Enrollment).filter(Enrollment.student_id == student.id, Enrollment.end_date.is_(None)).first()
    if status == "inactive" and current:
        current.end_date = d
    audit(db, "student_status", student.id, "update", user.id,
          {"old": old, "new": status, "date": d.isoformat(), "reason": reason})


def move_group(db: Session, *, user: User, student: Student, group_id: int, d: date) -> None:
    current = db.query(Enrollment).filter(Enrollment.student_id == student.id, Enrollment.end_date.is_(None)).first()
    if current and current.group_id == group_id:
        return
    if current:
        current.end_date = d
    db.add(Enrollment(student_id=student.id, group_id=group_id, start_date=d))
    audit(db, "student_group", student.id, "update", user.id,
          {"old": current.group_id if current else None, "new": group_id, "date": d.isoformat()})


def accept_cash(db: Session, *, user: User, site_org_id: int, student: Student, amount: Decimal, d: date,
                what: str | None, pocket_user_id: int | None = None) -> Transaction:
    """Наличные от родителя: событие у ребёнка + деньги в карман принявшего."""
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    pocket = pocket_user_id or user.id
    txn = Transaction(organization_id=student.organization_id, type="income", amount=amount, student_id=student.id,
                      description=what or "Оплата наличными", date=d, created_by=user.id, paid_from_user_id=pocket)
    db.add(txn)
    db.flush()
    db.add(CashFunding(organization_id=site_org_id, source_type="direct_cash", amount=amount, date=d,
                       taken_by=pocket, accountable_user_id=pocket, source_transaction_id=txn.id,
                       comment=f"{what or 'Оплата'} — {student.name}", created_by=user.id))
    db.flush()
    audit(db, "transaction", txn.id, "insert", user.id, {"kind": "cash_income", "student": student.id, "amount": float(amount)})
    return txn
