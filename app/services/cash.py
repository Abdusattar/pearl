"""Касса с карманами (новый вход, макет блок 4, принят 15.09).

Два счёта, одна касса площадки, карманы по людям. Карман человека =
последний пересчёт его кармана (Reconciliation kind='pocket') + движения
после него: снятия и взносы в его карман, передачи, минус расходы из его
кармана, оплаты поставщикам, изъятия учредителей. Пока карман ни разу не
пересчитывали, отсчёт идёт от пересчёта кассы объекта (старый вход): его
сумма приписана держателю кассы, у остальных ноль.

Расход без указания кармана (старый вход) считается из кармана того, кто его
завёл: другого честного ответа нет, и именно поэтому первый пересчёт карманов
— точка отсчёта, которую владелец делает руками (решение 15.09).
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.models import (CapitalWithdrawal, CashFunding, CashTransfer, Organization, Reconciliation,
                        SupplierPayment, Transaction, User)
from app.services import podotchet
from app.services.podotchet import PODOTCHET_START_DATE, _not_yet_counted, get_cash_baseline
from app.services.purchases import audit, founders, pocket_users, site_orgs

POCKET = "pocket"
POCKET_DELTA_THRESHOLD = Decimal("500")   # выше — нужна причина (макет 4б); потом в Настройки
ZERO = Decimal("0")


def pocket_start(db: Session, site_org_id: int, user_id: int) -> dict:
    """База кармана: свой пересчёт, иначе доля в пересчёте кассы объекта."""
    rec = (db.query(Reconciliation)
           .filter(Reconciliation.organization_id == site_org_id, Reconciliation.kind == POCKET,
                   Reconciliation.subject_id == user_id, Reconciliation.cancelled_at.is_(None))
           .order_by(Reconciliation.date.desc(), Reconciliation.id.desc()).first())
    if rec is not None:
        return {"amount": Decimal(rec.actual_amount), "date": rec.date, "at": rec.created_at, "own": True}
    base = get_cash_baseline(db, site_org_id)
    org = db.get(Organization, site_org_id)
    holder = org.cash_recipient_user_id if org else None
    return {"amount": base["amount"] if holder == user_id else ZERO, "date": base["date"], "at": base["at"], "own": False}


def _sum(q) -> Decimal:
    return Decimal(q.scalar() or 0)


def pocket_balance(db: Session, site_org_id: int, user_id: int) -> Decimal:
    start = pocket_start(db, site_org_id, user_id)

    def after(model):
        conds = [model.date >= PODOTCHET_START_DATE, model.deleted_at.is_(None)]
        if start["date"] is not None:
            conds.append(_not_yet_counted(model, start))
        return and_(*conds)

    funded = _sum(db.query(func.coalesce(func.sum(CashFunding.amount), 0)).filter(
        CashFunding.organization_id == site_org_id, CashFunding.accountable_user_id == user_id, after(CashFunding)))
    payer = or_(Transaction.paid_from_user_id == user_id,
                and_(Transaction.paid_from_user_id.is_(None), Transaction.created_by == user_id))
    spent = _sum(db.query(func.coalesce(func.sum(func.coalesce(Transaction.amount_paid, Transaction.amount)), 0)).filter(
        Transaction.organization_id == site_org_id, Transaction.type == "expense",
        Transaction.paid_directly.is_(False), payer, after(Transaction)))
    paid = _sum(db.query(func.coalesce(func.sum(SupplierPayment.amount), 0)).filter(
        SupplierPayment.organization_id == site_org_id, SupplierPayment.paid_directly.is_(False),
        SupplierPayment.paid_from_user_id == user_id, after(SupplierPayment)))
    taken = _sum(db.query(func.coalesce(func.sum(CapitalWithdrawal.amount), 0)).filter(
        CapitalWithdrawal.organization_id == site_org_id,
        or_(CapitalWithdrawal.from_user_id == user_id,
            and_(CapitalWithdrawal.from_user_id.is_(None), CapitalWithdrawal.created_by == user_id)),
        after(CapitalWithdrawal)))
    t_in = _sum(db.query(func.coalesce(func.sum(CashTransfer.amount), 0)).filter(
        CashTransfer.site_org_id == site_org_id, CashTransfer.to_user_id == user_id, after(CashTransfer)))
    t_out = _sum(db.query(func.coalesce(func.sum(CashTransfer.amount), 0)).filter(
        CashTransfer.site_org_id == site_org_id, CashTransfer.from_user_id == user_id, after(CashTransfer)))
    # Пополнение с источником «та же касса» (слияние Сокулука 15.09: деньги
    # Айжан из снятых 250 000) — это перевод между карманами: касса объекта
    # его уже вычитает через _lent_out, а у кармана отдавшего (taken_by) он
    # должен уйти в минус, иначе сумма карманов не сойдётся с кассой.
    internal_out = _sum(db.query(func.coalesce(func.sum(CashFunding.amount), 0)).filter(
        CashFunding.organization_id == site_org_id, CashFunding.source_organization_id == site_org_id,
        CashFunding.taken_by == user_id, CashFunding.accountable_user_id != user_id, after(CashFunding)))
    return start["amount"] + funded + t_in - spent - paid - taken - t_out - internal_out


def pocket_people(db: Session, site_org_id: int) -> list[User]:
    people = {u.id: u for u in pocket_users(db, site_org_id)}
    org = db.get(Organization, site_org_id)
    extra_ids = {r[0] for r in db.query(CashFunding.accountable_user_id)
                 .filter(CashFunding.organization_id == site_org_id, CashFunding.deleted_at.is_(None)).distinct()}
    extra_ids |= {r[0] for r in db.query(Transaction.paid_from_user_id)
                  .filter(Transaction.organization_id == site_org_id, Transaction.paid_from_user_id.isnot(None)).distinct()}
    if org and org.cash_recipient_user_id:
        extra_ids.add(org.cash_recipient_user_id)
    for uid in extra_ids - set(people):
        u = db.get(User, uid)
        if u and u.role != "founder":
            people[uid] = u
    return sorted(people.values(), key=lambda u: u.id)


def pockets(db: Session, site_org_id: int) -> dict:
    """Карманы площадки: у кого сколько, когда подтверждали, и что не разнесено."""
    rows = []
    total = ZERO
    for u in pocket_people(db, site_org_id):
        bal = pocket_balance(db, site_org_id, u.id)
        start = pocket_start(db, site_org_id, u.id)
        days = (date.today() - start["date"]).days if start["date"] else None
        rows.append({"user": u, "balance": bal, "start": start, "days": days,
                     "flag": "bad" if bal < -1 else ("warn" if (not start["own"] or (days or 0) > 7) else "")})
        total += bal
    net = podotchet.get_cash_state(db, site_org_id)["net"]
    return {"rows": rows, "total": net, "unassigned": net - total}


def accounts(db: Session, site_org_id: int) -> list[dict]:
    out = []
    for o in site_orgs(db, site_org_id):
        exp = podotchet.get_expected_balance(db, o.id, date.today())
        out.append({"org": o, "expected": exp["expected"], "since": exp["since"]})
    return out


def recent(db: Session, site_org_id: int, limit: int = 10) -> list[dict]:
    """Последние движения кассы одной лентой."""
    items = []
    for f in (db.query(CashFunding).filter(CashFunding.organization_id == site_org_id, CashFunding.deleted_at.is_(None))
              .order_by(CashFunding.date.desc(), CashFunding.id.desc()).limit(limit).all()):
        who = f.accountable.name if f.accountable else "?"
        if f.source_type == "withdrawal":
            acc = f.account_org.name if f.account_org else (f.organization.name if f.organization else "")
            title, sub = f"{who}: снятие со счёта {acc}", f.comment or ""
        elif f.source_founder_id:
            title, sub = f"{f.source_founder.name if f.source_founder else 'Учредитель'} внёс(ла) в карман {who}", f.comment or ""
        else:
            title, sub = f"Наличные в карман {who}", f.comment or ""
        items.append({"date": f.date, "title": title, "sub": sub, "amount": Decimal(f.amount), "at": f.created_at})
    for t in (db.query(CashTransfer).filter(CashTransfer.site_org_id == site_org_id, CashTransfer.deleted_at.is_(None))
              .order_by(CashTransfer.date.desc(), CashTransfer.id.desc()).limit(limit).all()):
        items.append({"date": t.date, "title": f"{t.from_user.name} передал(а) {t.to_user.name}", "sub": t.comment or "",
                      "amount": Decimal(t.amount), "at": t.created_at, "transfer": True})
    for w in (db.query(CapitalWithdrawal).filter(CapitalWithdrawal.organization_id == site_org_id, CapitalWithdrawal.deleted_at.is_(None))
              .order_by(CapitalWithdrawal.date.desc(), CapitalWithdrawal.id.desc()).limit(limit).all()):
        items.append({"date": w.date, "title": f"{w.founder.name if w.founder else 'Учредитель'} взял(а) из кармана {w.from_user.name if w.from_user else 'кассы'}",
                      "sub": w.comment or "", "amount": -Decimal(w.amount), "at": w.created_at})
    for r in (db.query(Reconciliation).filter(Reconciliation.organization_id == site_org_id, Reconciliation.kind == POCKET,
                                              Reconciliation.cancelled_at.is_(None))
              .order_by(Reconciliation.date.desc(), Reconciliation.id.desc()).limit(limit).all()):
        u = db.get(User, r.subject_id)
        d = Decimal(r.delta)
        items.append({"date": r.date, "title": f"Пересчёт кармана {u.name if u else '?'}: " + ("сошлось" if abs(d) < Decimal('0.01') else ("недостача" if d < 0 else "излишек")),
                      "sub": r.reason or "", "amount": d if abs(d) >= Decimal("0.01") else None, "at": r.created_at})
    for p in (db.query(SupplierPayment).filter(SupplierPayment.organization_id == site_org_id, SupplierPayment.deleted_at.is_(None))
              .order_by(SupplierPayment.date.desc(), SupplierPayment.id.desc()).limit(limit).all()):
        items.append({"date": p.date, "title": f"Оплата {p.supplier.name if p.supplier else ''}",
                      "sub": ("со счёта" if p.paid_directly else f"из кармана {p.paid_from_user.name if p.paid_from_user else ''}"),
                      "amount": -Decimal(p.amount), "at": p.created_at})
    salary = (db.query(Transaction.date, func.sum(Transaction.amount), func.count(Transaction.id))
              .filter(Transaction.organization_id == site_org_id, Transaction.type == "expense",
                      Transaction.employee_id.isnot(None), Transaction.deleted_at.is_(None))
              .group_by(Transaction.date).order_by(Transaction.date.desc()).limit(3).all())
    for d, s, n in salary:
        items.append({"date": d, "title": "Зарплата", "sub": f"{n} чел.", "amount": -Decimal(s), "at": None})
    items.sort(key=lambda x: (x["date"], x["at"] or datetime.min), reverse=True)
    return items[:limit]


# ── действия ─────────────────────────────────────────────────────────────

def withdraw(db: Session, *, user: User, site_org_id: int, account_org_id: int, amount: Decimal, d: date,
             comment: str | None = None, pocket_user_id: int | None = None) -> CashFunding:
    """Снятие со счёта: кто нажал, у того и деньги (макет 4б)."""
    f = CashFunding(organization_id=site_org_id, source_type="withdrawal", amount=amount, date=d,
                    taken_by=user.id, accountable_user_id=pocket_user_id or user.id, account_org_id=account_org_id,
                    comment=comment, created_by=user.id)
    db.add(f)
    db.flush()
    audit(db, "cash_funding", f.id, "insert", user.id, {"kind": "withdrawal", "account": account_org_id, "amount": float(amount)})
    return f


def transfer(db: Session, *, user: User, site_org_id: int, from_user_id: int, to_user_id: int, amount: Decimal,
             d: date, comment: str | None = None) -> CashTransfer:
    if from_user_id == to_user_id:
        raise ValueError("Передать можно только другому человеку")
    t = CashTransfer(site_org_id=site_org_id, from_user_id=from_user_id, to_user_id=to_user_id, amount=amount,
                     date=d, comment=comment, created_by=user.id)
    db.add(t)
    db.flush()
    audit(db, "cash_transfer", t.id, "insert", user.id, {"from": from_user_id, "to": to_user_id, "amount": float(amount)})
    return t


def recount(db: Session, *, user: User, site_org_id: int, pocket_user_id: int, actual: Decimal, d: date,
            reason: str | None) -> Reconciliation:
    """Пересчёт кармана: насчитанное — новая база; разница остаётся фактом."""
    expected = pocket_balance(db, site_org_id, pocket_user_id)
    delta = actual - expected
    if abs(delta) > POCKET_DELTA_THRESHOLD and not (reason or "").strip():
        raise ValueError(f"Разница {delta:+.0f} выше порога {POCKET_DELTA_THRESHOLD:.0f}: напишите, что произошло")
    rec = Reconciliation(organization_id=site_org_id, kind=POCKET, subject_id=pocket_user_id, date=d,
                         expected_amount=expected, actual_amount=actual, delta=delta,
                         reason=(reason or "").strip() or None, created_by=user.id)
    db.add(rec)
    db.flush()
    audit(db, "reconciliation", rec.id, "insert", user.id, {"kind": POCKET, "pocket": pocket_user_id,
                                                            "expected": float(expected), "actual": float(actual)})
    return rec


def founder_fund(db: Session, *, user: User, site_org_id: int, founder_id: int, pocket_user_id: int,
                 amount: Decimal, d: date, comment: str | None = None) -> CashFunding:
    f = CashFunding(organization_id=site_org_id, source_type="direct_cash", amount=amount, date=d,
                    taken_by=pocket_user_id, accountable_user_id=pocket_user_id, source_founder_id=founder_id,
                    comment=comment, created_by=user.id)
    db.add(f)
    db.flush()
    audit(db, "cash_funding", f.id, "insert", user.id, {"kind": "founder", "founder": founder_id, "amount": float(amount)})
    return f


def founder_withdraw(db: Session, *, user: User, site_org_id: int, founder_id: int, pocket_user_id: int,
                     amount: Decimal, d: date, comment: str | None = None) -> CapitalWithdrawal:
    w = CapitalWithdrawal(organization_id=site_org_id, founder_user_id=founder_id, amount=amount, date=d,
                          from_user_id=pocket_user_id, comment=comment, created_by=user.id)
    db.add(w)
    db.flush()
    audit(db, "capital_withdrawal", w.id, "insert", user.id, {"founder": founder_id, "from": pocket_user_id, "amount": float(amount)})
    return w


def founder_list(db: Session) -> list[User]:
    return founders(db)
