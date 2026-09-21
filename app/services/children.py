"""Дети (новый вход, макет блок 5, принят 15.09): список по группам с долгом,
карточка с лентой событий и таблицей по месяцам, приём наличных в карман.

Баланс считается из начислений и оплат (billing), оплата ложится на самый
старый долг (решение владельца 15.09): поэтому у ребёнка видно не только
сумму, но и «за какие месяцы». Школа без тарифа показывает «тариф не
заведён», а не выдуманный долг.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
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


def _is_test(s: Student) -> bool:
    from app.services.students import TEST_PIN_THRESHOLD
    return bool(s.pin and s.pin.isdigit() and int(s.pin) >= TEST_PIN_THRESHOLD)


def _stem(w: str) -> str:
    """«Айбекова» (мама) находит «Айбеков Нурсултан»: срезаем женское окончание."""
    return w[:-1] if len(w) > 4 and w[-1] in "ау" else w


def _match(words: list[str], s: Student, group: str | None) -> bool:
    """Каждое набранное слово — начало какого-то слова в имени, родителе или группе
    («Айбек» не цепляет «Таалайбек»); цифры — PIN целиком, нули впереди не важны."""
    hay = " ".join(x for x in (s.name, s.parent_name, group) if x).lower().replace("«", " ").replace("»", " ").split()
    pin = (s.pin or "").lstrip("0")
    return all(any(h.startswith(w) for h in hay) or (w.isdigit() and pin == w.lstrip("0")) for w in words)


def _money(x: float) -> str:
    from app.services.price_check import fmt_money
    return fmt_money(float(x))


def _day(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def _src(p: Transaction, names: dict[int, str]) -> str:
    if p.external_txn_id:
        return "через банк"
    who = names.get(p.paid_from_user_id)
    return f"наличными, карман: {who}" if who else "наличными"


def _payments(db: Session, ids: list[int]) -> dict[int, list[Transaction]]:
    out: dict[int, list[Transaction]] = {}
    if not ids:
        return out
    for p in (db.query(Transaction).filter(Transaction.student_id.in_(ids), Transaction.type == "income",
                                           Transaction.deleted_at.is_(None))
              .order_by(Transaction.date, Transaction.id).all()):
        out.setdefault(p.student_id, []).append(p)
    return out


def _debt_months(alloc: list[dict]) -> str:
    months = []
    for r in alloc:
        if r["left"] > 0.5:
            c = r["charge"]
            m = month_name(c.date) if c.description == MONTHLY else (c.description or "услуга")
            if m not in months:
                months.append(m)
    return " и ".join(months) if len(months) <= 2 else f"{months[0]} и ещё {len(months) - 1}"


def answer(balance: float, alloc: list[dict], last: Transaction | None, tariff: bool) -> dict:
    """Ответ родителю одной фразой: дошло ли и что осталось. say — крупно, more — мелко."""
    got = f"Дошло {_day(last.date)}, {_money(float(last.amount))}." if last else None
    recent = bool(last and (date.today() - last.date).days <= 7)
    if not tariff:
        return {"kind": "none", "say": f"{got} Тариф не заведён" if got else "Тариф не заведён, оплат не было", "more": ""}
    if balance > 0.5:
        debt = f"должны {_money(balance)} за {_debt_months(alloc)}"
        if recent:
            return {"kind": "debt", "say": f"{got} Ещё {debt}", "more": ""}
        more = f"последняя оплата {_day(last.date)}, {_money(float(last.amount))}" if last else "оплат пока не было"
        return {"kind": "debt", "say": debt[0].upper() + debt[1:], "more": more}
    if balance < -0.5:
        over = f"Переплата {_money(-balance)}, уйдёт на следующий месяц"
        return {"kind": "over", "say": f"{got} {over}" if got else over, "more": ""}
    return {"kind": "ok", "say": f"{got} Долга нет" if got else "Долга нет", "more": ""}


def _groups_of(db: Session, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    return dict(db.query(Enrollment.student_id, Group.name).join(Group, Group.id == Enrollment.group_id)
                .filter(Enrollment.student_id.in_(ids), Enrollment.end_date.is_(None)).all())


def _charges(db: Session, ids: list[int]) -> dict[int, list[Charge]]:
    out: dict[int, list[Charge]] = {}
    for c in (db.query(Charge).filter(Charge.student_id.in_(ids), Charge.deleted_at.is_(None)).all() if ids else []):
        out.setdefault(c.student_id, []).append(c)
    return out


def children_list(db: Session, org: Organization, only_debt: bool = False, q: str | None = None) -> dict:
    """Список по группам. Поиск: фамилия/имя ребёнка, фамилия родителя, группа,
    PIN (молча — Махабат его не помнит, 21.09). До трёх найденных — сразу ответ
    «дошло / должны» без захода в карточку; набрали группу — список группы.
    Итоги сверху всегда по всему объекту. Тестовые PIN банка не показываем."""
    today = date.today()
    students = [s for s in (db.query(Student)
                            .filter(Student.organization_id == org.id, Student.status.in_(("active", "frozen")),
                                    Student.deleted_at.is_(None))
                            .order_by(Student.name).all()) if not _is_test(s)]
    ids = [s.id for s in students]
    group_by = _groups_of(db, ids)
    balances = billing.get_balances(db, ids)
    charges_by = _charges(db, ids)
    pays = _payments(db, ids)
    tariff = billing.get_tuition_service(db, org.id)

    rows = []
    debt_total = old_total = 0.0
    for s in students:
        bal = balances.get(s.id, 0.0)
        alloc = _allocate(charges_by.get(s.id, []), sum(float(p.amount) for p in pays.get(s.id, [])))
        status, kind = _status(alloc, bal, today)
        if bal > 0.5:
            debt_total += bal
            old_total += sum(r["left"] for r in alloc if r["charge"].date.replace(day=1) < today.replace(day=1))
        sub = []
        if s.status == "frozen":
            sub.append("заморожен")
        if s.discount_amount and float(s.discount_amount) > 0:
            sub.append(f"скидка {_money(float(s.discount_amount))}" + (f", {s.discount_reason}" if s.discount_reason else ""))
        last = pays[s.id][-1] if pays.get(s.id) else None
        rows.append({"s": s, "balance": bal, "status": status, "kind": kind, "group": group_by.get(s.id, "Без группы"),
                     "sub": ", ".join(sub), "answer": answer(bal, alloc, last, bool(tariff))})
    group_size: dict[str, int] = {}
    for r in rows:
        group_size[r["group"]] = group_size.get(r["group"], 0) + 1

    words = [_stem(w) for w in (q or "").lower().split()]
    is_group = bool(words) and any(g.lower().startswith(words[0]) for g in group_size if g != "Без группы")
    if words:
        rows = [r for r in rows if _match(words, r["s"], group_by.get(r["s"].id))]
    if only_debt:
        rows = [r for r in rows if r["balance"] > 0.5]
    hits = rows if words and not is_group and 0 < len(rows) <= 3 else []
    groups = []
    for r in sorted(rows, key=lambda r: (r["group"] == "Без группы", r["group"], r["s"].name)):
        if not groups or groups[-1]["name"] != r["group"]:
            groups.append({"name": r["group"], "rows": [], "total": group_size.get(r["group"], 0)})
        groups[-1]["rows"].append(r)
    return {"groups": groups, "hits": hits, "count": len(students), "debt_total": debt_total, "old_total": old_total,
            "tariff": tariff, "shown": len(rows)}


def _allocate_payments(charges: list[Charge], payments: list[Transaction]) -> list[dict]:
    """Каждая оплата по частям ложится на самые старые начисления — чтобы в
    карточке было видно, какая оплата закрыла какой месяц."""
    queue = [[p, float(p.amount)] for p in payments]
    out = []
    for c in sorted(charges, key=lambda c: (c.date, c.id)):
        need = float(c.amount)
        pieces = []
        while need > 0.005 and queue:
            head = queue[0]
            take = min(need, head[1])
            pieces.append((head[0], take))
            need -= take
            head[1] -= take
            if head[1] <= 0.005:
                queue.pop(0)
        out.append({"charge": c, "amount": float(c.amount), "pieces": pieces, "left": round(max(need, 0.0), 2)})
    return out


def child_card(db: Session, student: Student) -> dict:
    """Карточка: одна фраза «что сейчас» и одна лента по месяцам — начислено,
    что пришло (когда и как), что осталось (макет блока «Дети», 21.09)."""
    today = date.today()
    charges = db.query(Charge).filter(Charge.student_id == student.id, Charge.deleted_at.is_(None)).all()
    payments = _payments(db, [student.id]).get(student.id, [])
    alloc = _allocate(charges, sum(float(p.amount) for p in payments))
    balance = billing.get_balance(db, student.id)
    status, kind = _status(alloc, balance, today)
    pocket_ids = {p.paid_from_user_id for p in payments if p.paid_from_user_id}
    names = dict(db.query(User.id, User.name).filter(User.id.in_(pocket_ids)).all()) if pocket_ids else {}

    months: dict[date, dict] = {}
    for r in _allocate_payments(charges, payments):
        c = r["charge"]
        m = months.setdefault(c.date.replace(day=1), {"charged": 0.0, "left": 0.0, "extra": [], "pieces": []})
        m["charged"] += r["amount"]
        m["left"] += r["left"]
        if c.description != MONTHLY:
            m["extra"].append(f"{c.description or 'услуга'} {_money(r['amount'])}")
        m["pieces"] += r["pieces"]
    month_rows = []
    this = today.replace(day=1)
    for key in sorted(months, reverse=True):
        m = months[key]
        line = f"начислено {_money(m['charged'])}"
        if m["extra"]:
            line += f" (в том числе {', '.join(m['extra'])})"
        by_pay: dict[int, list] = {}
        for p, a in m["pieces"]:
            by_pay.setdefault(p.id, [p, 0.0])[1] += a
        parts = list(by_pay.values())
        paid = sum(a for _, a in parts)
        if len(parts) == 1:
            p = parts[0][0]
            line += f", пришло {_money(paid)}: {p.date.strftime('%d.%m')} {_src(p, names)}"
        elif parts:
            line += f", пришло {_money(paid)}: " + ", ".join(
                f"{_money(a)} {p.date.strftime('%d.%m')} {_src(p, names)}" for p, a in parts)
        else:
            line += ", оплат пока не было" if key == this else ", оплат не было"
        name = MONTHS_NOM[key.month - 1].capitalize() + ("" if key.year == today.year else f" {key.year}")
        month_rows.append({"period": key, "name": name, "left": m["left"], "text": line, "old": key < this})
    group = (db.query(Group.name).join(Enrollment, Enrollment.group_id == Group.id)
             .filter(Enrollment.student_id == student.id, Enrollment.end_date.is_(None)).first())
    enr = db.query(Enrollment).filter(Enrollment.student_id == student.id).order_by(Enrollment.start_date.asc()).first()
    has_tariff = billing.get_tuition_service(db, student.organization_id) is not None
    return {"balance": balance, "status": status, "kind": kind, "months": month_rows,
            "answer": answer(balance, alloc, payments[-1] if payments else None, has_tariff),
            "debt_months": _debt_months(alloc),
            "group": group[0] if group else None, "since": enr.start_date if enr else None,
            "tariff": billing.tuition_base_price(db, student) if has_tariff else None,
            "overpaid": max(0.0, -balance)}


def payments_month(db: Session, orgs: list[Organization], month: date) -> dict:
    """Что пришло от родителей за месяц: итог по объектам и лента по дням.
    Под каждой оплатой — что с ребёнком сейчас. Тестовые PIN банка не показываем."""
    start = month.replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    org_ids = [o.id for o in orgs]
    txs = [(t, s) for t, s in (db.query(Transaction, Student).join(Student, Student.id == Transaction.student_id)
                              .filter(Transaction.organization_id.in_(org_ids), Transaction.type == "income",
                                      Transaction.deleted_at.is_(None), Transaction.date >= start, Transaction.date < end)
                              .order_by(Transaction.date.desc(), Transaction.id.desc()).all()) if not _is_test(s)]
    ids = list({s.id for _, s in txs})
    balances = billing.get_balances(db, ids)
    charges_by = _charges(db, ids)
    pays = _payments(db, ids)
    group_by = _groups_of(db, ids)
    tariff = {o.id: billing.get_tuition_service(db, o.id) is not None for o in orgs}
    by_org = {o.id: 0.0 for o in orgs}
    days: list[dict] = []
    for t, s in txs:
        by_org[t.organization_id] = by_org.get(t.organization_id, 0.0) + float(t.amount)
        bal = balances.get(s.id, 0.0)
        alloc = _allocate(charges_by.get(s.id, []), sum(float(p.amount) for p in pays.get(s.id, [])))
        if not tariff.get(s.organization_id):
            after, kind = "", "none"
        elif bal > 0.5:
            after, kind = f"ещё должны {_money(bal)} за {_debt_months(alloc)}", "debt"
        elif bal < -0.5:
            after, kind = f"переплата {_money(-bal)}, уйдёт на следующий месяц", "over"
        else:
            after, kind = "долга нет", "ok"
        if not t.external_txn_id:
            after = f"{after}, наличными" if after else "наличными"
        if not days or days[-1]["date"] != t.date:
            days.append({"date": t.date, "rows": []})
        days[-1]["rows"].append({"t": t, "s": s, "group": group_by.get(s.id), "after": after, "kind": kind})
    return {"total": sum(by_org.values()), "count": len(txs), "days": days,
            "by_org": [(o, by_org[o.id]) for o in orgs if by_org.get(o.id)]}


def set_info(db: Session, *, user: User, student: Student, last_name: str, first_name: str, patronymic: str,
             parent_name: str, parent_contact: str) -> None:
    from app.services.students import compose_name
    if not last_name.strip() or not first_name.strip():
        raise ValueError("Фамилия и имя обязательны")
    old = {"name": student.name, "parent_name": student.parent_name, "parent_contact": student.parent_contact}
    student.last_name, student.first_name = last_name.strip(), first_name.strip()
    student.patronymic = patronymic.strip() or None
    student.name = compose_name(last_name, first_name, patronymic)
    student.parent_name = parent_name.strip() or None
    student.parent_contact = parent_contact.strip() or None
    new = {"name": student.name, "parent_name": student.parent_name, "parent_contact": student.parent_contact}
    if new != old:
        audit(db, "student_info", student.id, "update", user.id, {"old": old, "new": new})


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
