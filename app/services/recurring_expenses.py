from datetime import date as date_type
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import Employee, RecurringExpenseTemplate, Transaction


def month_start(on_date: date_type | None = None) -> date_type:
    return (on_date or date_type.today()).replace(day=1)


def suggested_amount(db: Session, template: RecurringExpenseTemplate) -> Decimal | None:
    """Подсказка суммы для подтверждения — не обязательная, человек всегда может
    поправить перед проведением. None = нет подсказки, поле остаётся пустым
    (коммуналка — сумма каждый месяц своя по счётчику)."""
    if template.amount_source == "employees_sum":
        total = (
            db.query(Employee.salary)
            .filter(Employee.organization_id == template.organization_id, Employee.status == "active")
            .all()
        )
        return sum((row[0] for row in total), Decimal(0)) if total else Decimal(0)
    if template.amount_source == "last_amount":
        last_tx = (
            db.query(Transaction)
            .filter(Transaction.recurring_template_id == template.id, Transaction.deleted_at.is_(None))
            .order_by(Transaction.date.desc(), Transaction.id.desc())
            .first()
        )
        return last_tx.amount if last_tx else None
    return None


def month_postings(db: Session, template: RecurringExpenseTemplate, m_start: date_type) -> list[Transaction]:
    """Все проводки по шаблону за этот месяц — намеренно не одна: аванс и
    остаток зарплаты, например, две отдельные проводки в одном месяце.
    Фильтр по `period` (за какой месяц), не по `date` (когда реально
    оплачено) — расход за июль иногда проводят в начале августа (12.07)."""
    return (
        db.query(Transaction)
        .filter(
            Transaction.recurring_template_id == template.id,
            Transaction.period == m_start,
            Transaction.deleted_at.is_(None),
        )
        .order_by(Transaction.date, Transaction.id)
        .all()
    )


def list_for_month(db: Session, organization_id: int, user_role: str, m_start: date_type) -> list[dict]:
    templates = (
        db.query(RecurringExpenseTemplate)
        .filter(
            RecurringExpenseTemplate.organization_id == organization_id,
            RecurringExpenseTemplate.active.is_(True),
        )
        .order_by(RecurringExpenseTemplate.id)
        .all()
    )
    if user_role not in ("owner", "founder"):
        templates = [t for t in templates if not t.owner_only]

    rows = []
    for t in templates:
        postings = month_postings(db, t, m_start)
        rows.append({
            "template": t,
            "suggested": suggested_amount(db, t),
            "postings": postings,
            "posted_total": sum((p.amount for p in postings), Decimal(0)),
        })
    return rows
