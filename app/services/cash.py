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
from app.services.price_check import fmt_money
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
    # Итог — сумма карманов, не касса объекта по записям (владелец 21.09): пересчёт
    # кармана — факт, а касса объекта считается от своего пересчёта 8 сентября и
    # пересчётов карманов не видит (18.09 показала −9 392 при 7 869 на руках).
    # Разница не прячется: «найдено против записей» — то, что пересчёты нашли
    # сверх записей (плюс) или недосчитались (минус), причина — в Корректировках.
    net = podotchet.get_cash_state(db, site_org_id)["net"]
    return {"rows": rows, "total": total, "records": net, "found": total - net}


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
    # Только наличная часть: «на карту» идёт со счёта и кассу не трогает.
    # Махабат 18.09: «Светлана 31 079 — система считает всё из кассы» — считала
    # правильно, а лента показывала сумму с картой.
    salary = (db.query(Transaction.date, func.sum(Transaction.amount), func.count(Transaction.id))
              .filter(Transaction.organization_id == site_org_id, Transaction.type == "expense",
                      Transaction.employee_id.isnot(None), Transaction.deleted_at.is_(None),
                      Transaction.paid_directly.is_(False))
              .group_by(Transaction.date).order_by(Transaction.date.desc()).limit(3).all())
    for d, s, n in salary:
        items.append({"date": d, "title": "Зарплата наличными", "sub": f"{n} чел., на карту — отдельно со счёта",
                      "amount": -Decimal(s), "at": None})
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


# ── Касса целиком (макет v3, утверждён 21.09) ────────────────────────────
# Принципы (context/revision/00_business.md): остатки совпадают с реальностью,
# пересчёт — не ритуал; проверку система просит только там, где сама видит
# пробел, и называет его словами. «Не сходится» не говорим, пока не хватает
# записи. Закрытое пересчётом не всплывает. Экран для площадки с любым числом
# счетов и держателей: счёт или человек без движений просто не показывается.

ACCOUNT = "account"
ACTIVE_DAYS = 30


def _account_active(db: Session, org_id: int, since: date | None) -> bool:
    """Счёт показываем, если его сверяли или по нему были движения за 30 дней."""
    if since is not None:
        return True
    from datetime import timedelta
    edge = date.today() - timedelta(days=ACTIVE_DAYS)
    # Тестовые оплаты банка (PIN от 9000, «Тестов Тестомир») счёт живым не делают:
    # иначе счёт школы всплывал с пробелом до запуска Optima для школы (21.09).
    from app.models import Student
    from app.services.students import TEST_PIN_THRESHOLD
    income = any(not (pin and pin.isdigit() and int(pin) >= TEST_PIN_THRESHOLD) for (pin,) in
                 db.query(Student.pin).select_from(Transaction).outerjoin(Student, Student.id == Transaction.student_id)
                 .filter(Transaction.organization_id == org_id, Transaction.type == "income",
                         Transaction.deleted_at.is_(None), Transaction.date >= edge).all())
    out = db.query(CashFunding.id).filter(CashFunding.account_org_id == org_id, CashFunding.source_type == "withdrawal",
                                          CashFunding.deleted_at.is_(None), CashFunding.date >= edge).first()
    return bool(income or out)


def state(db: Session, site_org_id: int) -> dict:
    """Два остатка с признаком доверия и пробелами. Один источник для Кассы,
    «Сегодня» и Обзора — чтобы экраны не расходились в словах и цифрах."""
    pk = pockets(db, site_org_id)
    rows = [r for r in pk["rows"] if abs(r["balance"]) >= 1 or r["start"]["own"]]
    gaps = []
    for r in rows:
        name = r["user"].name
        if r["balance"] < -1:
            gaps.append({"where": "cash", "title": f"{name}: по записям минус {fmt_money(float(abs(r['balance'])))}",
                         "sub": "не хватает записи, откуда деньги: снятия или передачи",
                         "go": "Передать деньги", "url": f"/new/cash/transfer?to={r['user'].id}"})
        elif not r["start"]["own"]:
            gaps.append({"where": "cash", "title": f"{name}: наличные ни разу не подтверждали",
                         "sub": f"по записям {fmt_money(float(r['balance']))}",
                         "go": "Подтвердить сумму", "url": f"/new/cash/recount?pocket={r['user'].id}"})
    own_dates = [r["start"]["date"] for r in rows if r["start"]["own"] and r["start"]["date"]]
    cash = {"total": sum((r["balance"] for r in rows), ZERO), "rows": rows,
            "ok": not any(g["where"] == "cash" for g in gaps),
            "confirmed": max(own_dates) if own_dates else None}

    accs = []
    for a in accounts(db, site_org_id):
        if not _account_active(db, a["org"].id, a["since"]):
            continue
        name = a["org"].name
        if a["since"] is None:
            gaps.append({"where": "account", "title": f"Остаток в банке по счёту {name} ни разу не вносили",
                         "sub": f"по записям {fmt_money(float(a['expected']))}",
                         "go": "Внести остаток в банке", "url": f"/new/cash/bank?org={a['org'].id}"})
        elif a["expected"] < -1:
            gaps.append({"where": "account", "title": f"По записям на счёте {name} минус",
                         "sub": "не хватает записи о поступлении на счёт",
                         "go": "Внести остаток в банке", "url": f"/new/cash/bank?org={a['org'].id}"})
        a["ok"] = not any(g["where"] == "account" and name in g["title"] for g in gaps)
        accs.append(a)
    return {"cash": cash, "accounts": accs, "gaps": gaps}


def expected_account(db: Session, org_id: int, d: date | None = None) -> Decimal:
    return Decimal(podotchet.get_expected_balance(db, org_id, d or date.today())["expected"])


def bank_balance(db: Session, *, user: User, org_id: int, actual: Decimal, d: date, reason: str | None) -> Reconciliation:
    """Остаток в банке (присылает Мунара): новая точка отсчёта счёта.
    Заметную разницу без причины не принимаем — как в старой сверке."""
    from app.services import reconciliation
    expected = reconciliation.expected_for(db, org_id, ACCOUNT, None, d)
    if reconciliation.severity(expected, actual - expected) == "big" and not (reason or "").strip():
        raise ValueError("Разница заметная: напишите, что произошло. Если это снятие или платёж со счёта, "
                         "которые ещё не внесли, сначала внесите их")
    rec = reconciliation.create(db, organization_id=org_id, kind=ACCOUNT, actual=actual, user_id=user.id,
                                on_date=d, reason=reason or "")
    audit(db, "reconciliation", rec.id, "insert", user.id, {"kind": ACCOUNT, "org": org_id,
                                                            "expected": float(rec.expected_amount), "actual": float(actual)})
    return rec


# ── история и «Убрать» ───────────────────────────────────────────────────

CHECKS = ("recount", "bank")
MINE = ("funding", "transfer", "founder_out")   # свои такие записи человек убирает сам


def _removal_reasons(db: Session, entity: str, ids: list[int]) -> dict[int, str]:
    from app.models import AuditLog
    if not ids:
        return {}
    out = {}
    for a in (db.query(AuditLog).filter(AuditLog.entity_type == entity, AuditLog.action == "delete",
                                        AuditLog.entity_id.in_(ids)).order_by(AuditLog.id).all()):
        out[a.entity_id] = (a.new_data or {}).get("reason") or ""
    return out


def can_remove(user: User, item: dict) -> bool:
    """Свои снятия, передачи и изъятия — сам; чужое, пересчёты и остатки в
    банке — только владелец (владелец 21.09: «ошибочные пока убираем вместе»)."""
    if item.get("removed") or not item.get("kind"):
        return False
    if item.get("locked"):
        return False
    if user.role == "owner":
        return True
    return item["kind"] in MINE and item.get("created_by") == user.id


def history(db: Session, site_org_id: int, only_checks: bool = False, limit: int = 60) -> list[dict]:
    """Движения кассы и счетов одной лентой, убранные — зачёркнутыми."""
    names = {u.id: u.name for u in db.query(User).all()}
    org_ids = [o.id for o in site_orgs(db, site_org_id)] + [site_org_id]
    items: list[dict] = []

    if not only_checks:
        fs = (db.query(CashFunding).filter(CashFunding.organization_id == site_org_id)
              .order_by(CashFunding.date.desc(), CashFunding.id.desc()).limit(limit).all())
        from app.models import Purchase
        linked = {r[0] for r in db.query(Purchase.funding_id).filter(Purchase.funding_id.in_([f.id for f in fs])).all()}
        why = _removal_reasons(db, "cash_funding", [f.id for f in fs if f.deleted_at])
        for f in fs:
            who = names.get(f.accountable_user_id, "?")
            if f.source_type == "withdrawal":
                acc = f.account_org.name if f.account_org else (f.organization.name if f.organization else "")
                title = f"{who}: снятие со счёта, {acc}"
            elif f.source_founder_id:
                title = f"{names.get(f.source_founder_id, 'Учредитель')} внёс(ла) в карман {who}"
            else:
                title = f"Наличные в карман {who}"
            items.append({"kind": "funding", "id": f.id, "date": f.date, "at": f.created_at, "title": title,
                          "sub": f.comment or "", "amount": Decimal(f.amount), "created_by": f.created_by,
                          "by": names.get(f.created_by), "removed": f.deleted_at is not None, "why": why.get(f.id, ""),
                          "locked": f.id in linked or f.source_transaction_id is not None})
        ts = (db.query(CashTransfer).filter(CashTransfer.site_org_id == site_org_id)
              .order_by(CashTransfer.date.desc(), CashTransfer.id.desc()).limit(limit).all())
        why = _removal_reasons(db, "cash_transfer", [t.id for t in ts if t.deleted_at])
        for t in ts:
            items.append({"kind": "transfer", "id": t.id, "date": t.date, "at": t.created_at,
                          "title": f"{names.get(t.from_user_id, '?')} передал(а) {names.get(t.to_user_id, '?')}",
                          "sub": t.comment or "", "amount": Decimal(t.amount), "created_by": t.created_by,
                          "by": names.get(t.created_by), "removed": t.deleted_at is not None, "why": why.get(t.id, "")})
        ws = (db.query(CapitalWithdrawal).filter(CapitalWithdrawal.organization_id == site_org_id)
              .order_by(CapitalWithdrawal.date.desc(), CapitalWithdrawal.id.desc()).limit(limit).all())
        why = _removal_reasons(db, "capital_withdrawal", [w.id for w in ws if w.deleted_at])
        for w in ws:
            items.append({"kind": "founder_out", "id": w.id, "date": w.date, "at": w.created_at,
                          "title": f"{names.get(w.founder_user_id, 'Учредитель')} взял(а) из кармана {names.get(w.from_user_id, 'кассы')}",
                          "sub": w.comment or "", "amount": -Decimal(w.amount), "created_by": w.created_by,
                          "by": names.get(w.created_by), "removed": w.deleted_at is not None, "why": why.get(w.id, "")})
        for p in (db.query(SupplierPayment).filter(SupplierPayment.organization_id == site_org_id,
                                                   SupplierPayment.deleted_at.is_(None))
                  .order_by(SupplierPayment.date.desc(), SupplierPayment.id.desc()).limit(limit).all()):
            src = "со счёта" if p.paid_directly else f"из кармана {names.get(p.paid_from_user_id, '')}"
            items.append({"kind": None, "id": p.id, "date": p.date, "at": p.created_at,
                          "title": f"Оплата {p.supplier.name if p.supplier else ''}", "sub": src,
                          "amount": -Decimal(p.amount), "by": names.get(p.created_by)})
        salary = (db.query(Transaction.date, func.sum(Transaction.amount), func.count(Transaction.id))
                  .filter(Transaction.organization_id == site_org_id, Transaction.type == "expense",
                          Transaction.employee_id.isnot(None), Transaction.deleted_at.is_(None),
                          Transaction.paid_directly.is_(False))
                  .group_by(Transaction.date).order_by(Transaction.date.desc()).limit(5).all())
        for d, s, n in salary:
            items.append({"kind": None, "id": None, "date": d, "at": None, "title": "Зарплата наличными",
                          "sub": f"{n} чел., убрать выдачу — в Зарплате", "amount": -Decimal(s)})

    recs = (db.query(Reconciliation).filter(Reconciliation.organization_id.in_(org_ids),
                                            Reconciliation.kind.in_((POCKET, ACCOUNT)))
            .order_by(Reconciliation.date.desc(), Reconciliation.id.desc()).limit(limit).all())
    for r in recs:
        d = Decimal(r.delta)
        same = abs(d) < Decimal("0.01")
        if r.kind == POCKET:
            title = f"{names.get(r.subject_id, '?')}, пересчёт наличных: " + (
                "сошлось" if same else ("больше, чем по записям" if d > 0 else "меньше, чем по записям"))
            sub = f"по записям {fmt_money(float(r.expected_amount))}, насчитали {fmt_money(float(r.actual_amount))}"
            kind = "recount"
        else:
            org = db.get(Organization, r.organization_id)
            title = f"Остаток в банке, счёт {org.name if org else ''}: " + (
                "сошлось" if same else ("больше, чем по записям" if d > 0 else "меньше, чем по записям"))
            sub = f"по записям {fmt_money(float(r.expected_amount))}, в банке {fmt_money(float(r.actual_amount))}"
            kind = "bank"
        if r.reason:
            sub += f". «{r.reason}»"
        items.append({"kind": kind, "id": r.id, "date": r.date, "at": r.created_at, "title": title, "sub": sub,
                      "amount": None if same else d, "created_by": r.created_by, "by": names.get(r.created_by),
                      "removed": r.cancelled_at is not None, "why": r.cancel_reason or ""})

    items.sort(key=lambda x: (x["date"], x["at"] or datetime.min), reverse=True)
    return items[:limit]


def remove(db: Session, *, user: User, site_org_id: int, kind: str, item_id: int, reason: str) -> None:
    """Убрать ошибочную запись: мягко, строка остаётся зачёркнутой с причиной."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Напишите, почему убираете")
    item = next((i for i in history(db, site_org_id, limit=500)
                 if i["kind"] == kind and i["id"] == item_id), None)
    if item is None or item["removed"]:
        raise ValueError("Запись не найдена или уже убрана")
    if not can_remove(user, item):
        raise ValueError("Эту запись убираем вместе с владельцем" if not item.get("locked")
                         else "Эта запись убирается вместе с покупкой или услугой, откуда она пришла")
    now = datetime.now()
    if kind in CHECKS:
        from app.services import reconciliation
        reconciliation.cancel(db, item_id, user.id, reason)
        audit(db, "reconciliation", item_id, "delete", user.id, {"reason": reason})
        return
    model, entity = {"funding": (CashFunding, "cash_funding"), "transfer": (CashTransfer, "cash_transfer"),
                     "founder_out": (CapitalWithdrawal, "capital_withdrawal")}[kind]
    row = db.get(model, item_id)
    row.deleted_at = now
    audit(db, entity, item_id, "delete", user.id, {"reason": reason, "amount": float(row.amount),
                                                    "date": row.date.isoformat()})
