"""Закрытие дня (владелец 24.09): день площадки закрыт, когда всё сошлось — черновиков нет,
каждый держатель кармана назвал наличные и они сошлись с записями, счёт после движения
подтверждён, открытых вопросов бота нет. Пока не закрыт — бот называет людям, что висит,
по лестнице: 15:00 группа каждому по имени → 16:00 заведующей в личку → 16:45 группа
заведующей → 17:15 итог → утром учредителю. Люди уходят в 17:30 — требовать заранее.

Что НЕ входит: копейки, старые долги, склад — это другие ритмы (неделя, месяц).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import BotMessage, CashFunding, Organization, Reconciliation, User
from app.services import cash, meals, rules, today
from app.services.purchases import site_orgs


def holders(db: Session, site: Organization) -> list[User]:
    """Чьи наличные закрываем: заведующая и учётчик площадки (роли manager, staff) с ботом."""
    org_ids = [o.id for o in site_orgs(db, site.id)]
    return (db.query(User).filter(User.role.in_(("manager", "staff")), User.deleted_at.is_(None),
                                  User.organization_id.in_(org_ids), User.tg_id.isnot(None))
            .order_by(User.id).all())


def manager(db: Session, site: Organization) -> User | None:
    return next((u for u in holders(db, site) if u.role == "manager"), None)


def checker(db: Session, site: Organization) -> User | None:
    return next((u for u in holders(db, site) if u.role == "staff"), None)


def _first(name: str) -> str:
    return (name or "").split()[0] if name else ""


def _fmt(v) -> str:
    from app.services.bot_group import fmt_money, _signed
    return _signed(Decimal(v)) if Decimal(v) < 0 or Decimal(v) > 0 else fmt_money(0)


def items(db: Session, site: Organization, d: date) -> list[dict]:
    """Что мешает закрыть день: [{who: User|None, text}] — текст без имени, коротко."""
    out: list[dict] = []
    since = datetime.combine(d, datetime.min.time())
    chk = checker(db, site)
    # 1. чеки на проверке — учётчику
    waiting = today.unchecked_receipts(db, site.id)
    if waiting:
        n = len(waiting)
        out.append({"who": chk, "text": f"{n} {'чек' if n == 1 else 'чека' if n < 5 else 'чеков'} подтвердить"})
    # 2. едоки за день — учётчику
    if meals.expected_today(db, site.id, d) and meals.get(db, site.id, d) is None:
        out.append({"who": chk, "text": "сколько сегодня ели — записать"})
    # 3. наличные каждого держателя: названы и сошлись
    for u in holders(db, site):
        rec = (db.query(Reconciliation).filter(Reconciliation.kind == "pocket", Reconciliation.subject_id == u.id,
                                               Reconciliation.date == d, Reconciliation.cancelled_at.is_(None))
               .order_by(Reconciliation.id.desc()).first())
        if rec is not None:
            if abs(Decimal(rec.delta or 0)) > 1 and not (rec.reason or "").strip():
                out.append({"who": u, "text": f"наличные не сошлись ({_fmt(rec.delta)}) — написать, что произошло"})
            continue
        offer = (db.query(BotMessage).filter(BotMessage.kind == "money_offer", BotMessage.user_id == u.id,
                                             BotMessage.created_at >= since,
                                             BotMessage.payload["op"].astext == "recount",
                                             BotMessage.status.in_(("sent", "paused", "deferred", "logged")))
                 .order_by(BotMessage.id.desc()).first())
        if offer is None:
            out.append({"who": u, "text": "наличные на конец дня — написать боту одной цифрой"})
        elif offer.status in ("sent", "logged", "paused"):
            out.append({"who": u, "text": "ответить боту про наличные (да / нет)"})
        # deferred — цифра есть, ждёт проверки чеков: держит только пункт 1
    # 4. счёт: было снятие сегодня — остаток после него должен быть подтверждён
    for org in site_orgs(db, site.id):
        moved = db.query(CashFunding.id).filter(CashFunding.source_type == "withdrawal", CashFunding.date == d,
                                                CashFunding.deleted_at.is_(None),
                                                (CashFunding.account_org_id == org.id)
                                                | ((CashFunding.account_org_id.is_(None))
                                                   & (CashFunding.organization_id == org.id))).first()
        if moved is None:
            continue
        rec = db.query(Reconciliation.id).filter(Reconciliation.kind == "account", Reconciliation.organization_id == org.id,
                                                 Reconciliation.date == d, Reconciliation.cancelled_at.is_(None)).first()
        if rec is None:
            from app.services.bot import _account_holder
            out.append({"who": _account_holder(db, org), "text": f"остаток счёта {org.name} после снятия — скрин боту"})
    # 5. открытые вопросы бота о деньгах (не про наличные — те уже выше)
    for m in (db.query(BotMessage).filter(BotMessage.kind == "money_offer", BotMessage.created_at >= since,
                                          BotMessage.status.in_(("sent", "logged")),
                                          BotMessage.payload["op"].astext != "recount").all()):
        out.append({"who": db.get(User, m.user_id), "text": "ответить боту: " + (m.payload or {}).get("what", "")[:60]})
    return out


def closed(db: Session, site: Organization, d: date) -> bool:
    return not items(db, site, d)


def by_person(items_: list[dict]) -> list[str]:
    """«Махабат, 4 чека подтвердить; сколько ели — записать.» — по строке на человека."""
    groups: dict[int | None, tuple[str, list[str]]] = {}
    for it in items_:
        u = it["who"]
        key = u.id if u else None
        groups.setdefault(key, (_first(u.name) if u else "", []))[1].append(it["text"])
    lines = []
    for _, (name, texts) in groups.items():
        body = "; ".join(texts)
        lines.append(f"{name}, {body}." if name else body[0].upper() + body[1:] + ".")
    return lines


def group_text(db: Session, site: Organization, d: date, to_manager: bool = False) -> str | None:
    its = items(db, site, d)
    if not its:
        return None
    lines = by_person(its)
    if to_manager:
        m = manager(db, site)
        if m is not None:
            return f"{_first(m.name)}, до конца дня не закрыто:\n" + "\n".join(lines)
    return "До конца дня осталось:\n" + "\n".join(lines)


def manager_text(db: Session, site: Organization, d: date) -> str | None:
    """Заведующей в личку (владелец 24.09: «она же руководитель»): что не закрыто у людей."""
    its = items(db, site, d)
    if not its:
        return None
    m = manager(db, site)
    if m is None:
        return None
    return (f"{_first(m.name)}, вы заведующая — до конца дня не закрыто:\n" + "\n".join(by_person(its))
            + "\nБез этого день не сойдётся.")


def result_text(db: Session, site: Organization, d: date) -> str:
    its = items(db, site, d)
    if not its:
        return "День закрыт: чеки проведены, наличные и счёт сошлись. Спасибо!"
    return "День не закрыт:\n" + "\n".join(by_person(its))


def owner_line(db: Session, site: Organization, d: date) -> str:
    its = items(db, site, d)
    if not its:
        return f"{site.name}: день закрыт."
    return f"{site.name}: день не закрыт — " + " ".join(by_person(its))


def founder_text(db: Session, site: Organization, d: date) -> str | None:
    """Утро после незакрытого дня — учредителю один раз: что и кто отвечает."""
    its = items(db, site, d)
    if not its:
        return None
    m = manager(db, site)
    who = f", отвечает {_first(m.name)}" if m else ""
    return f"Вчера ({d.strftime('%d.%m')}) день не закрыт{who}:\n" + "\n".join(by_person(its))


def last_working_day(db: Session, site: Organization, d: date) -> date:
    prev = d - timedelta(days=1)
    while not meals.expected_today(db, site.id, prev) and prev > d - timedelta(days=7):
        prev -= timedelta(days=1)
    return prev
