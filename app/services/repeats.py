"""Вопрос «такое уже записано» для денежных записей (17.09).

Двойной тап закрывает номер формы (`once.py`). Здесь другой случай: человек
через неделю вносит то же самое второй раз, забыв, что уже вносил (снятие
250 000 от 09.09 записано 10.09 и ещё раз 16.09). Совпадение — та же сумма,
та же дата события и тот же «кто/кому»; тогда форма спрашивает, а не пишет.
Правило то же, что у «Купили» и у детей: без явного «это другое» не заводится.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import CapitalWithdrawal, CashFunding, CashTransfer, SupplierPayment, Transaction, User
from app.services.price_check import fmt_money


def _found(db: Session, row, what: str) -> dict | None:
    if row is None:
        return None
    by = db.get(User, row.created_by) if row.created_by else None
    return {"what": f"{what} {fmt_money(float(row.amount))}", "date": row.date,
            "by": by.name if by else None, "at": row.created_at.date() if row.created_at else None}


def _last(q, model):
    return q.filter(model.deleted_at.is_(None)).order_by(model.id.desc()).first()


def withdrawal(db: Session, site_org_id: int, amount: Decimal, d: date) -> dict | None:
    q = db.query(CashFunding).filter(CashFunding.organization_id == site_org_id,
                                     CashFunding.source_type == "withdrawal",
                                     CashFunding.amount == amount, CashFunding.date == d)
    return _found(db, _last(q, CashFunding), "снятие")


def transfer(db: Session, site_org_id: int, from_user_id: int, to_user_id: int, amount: Decimal, d: date) -> dict | None:
    q = db.query(CashTransfer).filter(CashTransfer.site_org_id == site_org_id, CashTransfer.from_user_id == from_user_id,
                                      CashTransfer.to_user_id == to_user_id, CashTransfer.amount == amount,
                                      CashTransfer.date == d)
    return _found(db, _last(q, CashTransfer), "передача")


def founder_fund(db: Session, site_org_id: int, founder_id: int, amount: Decimal, d: date) -> dict | None:
    q = db.query(CashFunding).filter(CashFunding.organization_id == site_org_id,
                                     CashFunding.source_founder_id == founder_id,
                                     CashFunding.amount == amount, CashFunding.date == d)
    return _found(db, _last(q, CashFunding), "взнос")


def founder_withdraw(db: Session, site_org_id: int, founder_id: int, amount: Decimal, d: date) -> dict | None:
    q = db.query(CapitalWithdrawal).filter(CapitalWithdrawal.organization_id == site_org_id,
                                           CapitalWithdrawal.founder_user_id == founder_id,
                                           CapitalWithdrawal.amount == amount, CapitalWithdrawal.date == d)
    return _found(db, _last(q, CapitalWithdrawal), "изъятие")


def supplier_payment(db: Session, supplier_id: int, amount: Decimal, d: date) -> dict | None:
    q = db.query(SupplierPayment).filter(SupplierPayment.supplier_id == supplier_id,
                                         SupplierPayment.amount == amount, SupplierPayment.date == d)
    return _found(db, _last(q, SupplierPayment), "оплата")


def child_cash(db: Session, student_id: int, amount: Decimal, d: date) -> dict | None:
    q = db.query(Transaction).filter(Transaction.type == "income", Transaction.student_id == student_id,
                                     Transaction.amount == amount, Transaction.date == d)
    return _found(db, _last(q, Transaction), "оплата")


def salary(db: Session, employee_id: int, period: date, amount: Decimal, d: date) -> dict | None:
    q = db.query(Transaction).filter(Transaction.type == "expense", Transaction.employee_id == employee_id,
                                     Transaction.period == period, Transaction.amount == amount, Transaction.date == d)
    return _found(db, _last(q, Transaction), "выдача")
