"""Экран «Настройки» (макет 21.09): правила, по которым система считает.
Видит только владелец. Тарифы, заморозка, разовые услуги, держатель наличных,
пороги, зарплата, склад, доступ — строками «что — сейчас — Изменить»."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import (AuditLog, Charge, Organization, ProductCategory, Service, ServicePriceHistory, Student,
                        User)
from app.services import rules
from app.services.purchases import site_orgs

MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
              "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
ROLE_TEXT = {"owner": "учредитель, настройки", "founder": "учредитель", "director": "директор",
             "manager": "управляющая", "staff": "учётчик"}
ROLE_SEES = {"owner": "всё, первым Обзор и Настройки", "founder": "всё, первым Обзор",
             "director": "свой объект: дети, оплаты, зарплата", "manager": "садик: касса, дети, расходы",
             "staff": "ввод: покупки, лист кухни, склад, дети, зарплата садика"}


def _day(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]} {d.year}"


def month_label(d: date) -> str:
    return f"{MONTHS_NOM[d.month - 1]} {d.year}"


def tuition(db: Session, org_id: int) -> Service | None:
    return (db.query(Service).filter(Service.organization_id == org_id, Service.is_tuition.is_(True),
                                     Service.deleted_at.is_(None)).first())


def charged_this_month(db: Session, org_id: int) -> bool:
    this = date.today().replace(day=1)
    return db.query(Charge.id).join(Student, Student.id == Charge.student_id).filter(
        Student.organization_id == org_id, Charge.date == this, Charge.description == "Начисление за месяц",
        Charge.deleted_at.is_(None)).first() is not None


def tariff_months(db: Session, org_id: int) -> list[date]:
    """С какого месяца можно: текущий — только если его ещё не начисляли."""
    this = date.today().replace(day=1)
    nxt = (this.replace(day=28) + timedelta(days=4)).replace(day=1)
    after = (nxt.replace(day=28) + timedelta(days=4)).replace(day=1)
    months = [nxt, after]
    return ([this] if not charged_this_month(db, org_id) else []) + months


def history(db: Session, svc: Service | None) -> list[dict]:
    if svc is None:
        return []
    names = {u.id: u.name for u in db.query(User).all()}
    rows = (db.query(ServicePriceHistory).filter(ServicePriceHistory.service_id == svc.id)
            .order_by(ServicePriceHistory.effective_date.desc(), ServicePriceHistory.id.desc()).all())
    return [{"price": r.price, "from": r.effective_date, "by": names.get(r.changed_by)} for r in rows]


def overview(db: Session, site_id: int) -> dict:
    orgs = site_orgs(db, site_id)
    pending = {p["org_id"]: p for p in rules.pending_tariffs(db)}
    tariffs, singles = [], []
    for o in orgs:
        t = tuition(db, o.id)
        legacy = None
        if o.legacy_tariff_price is not None and o.legacy_tariff_until:
            ended = o.legacy_tariff_until <= date.today()
            legacy = (f"переходные {Decimal(o.legacy_tariff_price):,.0f}".replace(",", " ")
                      + (" закончились " if ended else " до ") + _day(o.legacy_tariff_until))
        p = pending.get(o.id)
        tariffs.append({"org": o, "svc": t, "legacy": legacy,
                        "pending": {"price": p["price"], "from": date.fromisoformat(p["from"])} if p else None})
        for s in (db.query(Service).filter(Service.organization_id == o.id, Service.is_tuition.is_(False),
                                           Service.deleted_at.is_(None)).order_by(Service.name).all()):
            singles.append({"org": o, "svc": s})
    users = db.query(User).filter(User.deleted_at.is_(None)).order_by(User.id).all()
    holders = [{"org": o, "user": db.get(User, o.cash_recipient_user_id) if o.cash_recipient_user_id else None}
               for o in orgs]
    cats = db.query(ProductCategory).order_by(ProductCategory.sort_order, ProductCategory.name).all()
    rv = {k: rules.get(db, k) for k in rules.RULES}
    return {"orgs": orgs, "tariffs": tariffs, "singles": singles, "holders": holders,
            "people": [u for u in users if u.role in ROLE_TEXT],
            "frozen": [{"org": o, "percent": o.frozen_discount_percent} for o in orgs],
            "cats": cats, "minor": [c for c in cats if c.is_minor], "rules": rv,
            "weekdays_text": rules.weekdays_text(rules.kitchen_weekdays(db))}


def set_tariff(db: Session, *, user: User, org: Organization, price_raw: str, month_raw: str) -> str:
    """Тариф обучения с месяца. Текущий (ещё не начисленный) — сразу; будущий — ждёт
    в Настройках и включится в начале месяца, до начислений. Возвращает, что сделано."""
    try:
        price = Decimal(price_raw.replace(" ", "").replace(",", "."))
    except Exception:
        raise ValueError("Сумма в месяц — числом, например 10 000")
    if price <= 0 or price > 1_000_000:
        raise ValueError("Сумма в месяц — больше нуля")
    try:
        start = date.fromisoformat(f"{month_raw}-01")
    except ValueError:
        raise ValueError("Выберите, с какого месяца")
    if start not in tariff_months(db, org.id):
        raise ValueError("Этот месяц уже начислен по прежней цене — выберите следующий")
    this = date.today().replace(day=1)
    items = [p for p in rules.pending_tariffs(db) if p["org_id"] != org.id]
    if start == this:
        svc = tuition(db, org.id)
        old = float(svc.price) if svc else None
        if svc is None:
            svc = Service(organization_id=org.id, name="Обучение", price=price, is_tuition=True, is_recurring=True)
            db.add(svc)
            db.flush()
        svc.price = price
        db.add(ServicePriceHistory(service_id=svc.id, price=price, effective_date=start, changed_by=user.id))
        rules.put(db, user=user, key=rules.PENDING_TARIFFS, value=items)
        db.add(AuditLog(entity_type="tariff", entity_id=org.id, action="update", user_id=user.id,
                        old_data={"price": old}, new_data={"price": float(price), "from": start.isoformat()}))
        return f"Тариф {org.name}: {price:,.0f} с {month_label(start)}. Начисления этого месяца пойдут по нему.".replace(",", " ")
    items.append({"org_id": org.id, "price": float(price), "from": start.isoformat(), "by": user.id})
    rules.put(db, user=user, key=rules.PENDING_TARIFFS, value=items)
    return f"Тариф {org.name}: {price:,.0f} с {month_label(start)}. Включится в начале месяца, до начислений.".replace(",", " ")


def cancel_pending(db: Session, *, user: User, org_id: int) -> None:
    rules.put(db, user=user, key=rules.PENDING_TARIFFS,
              value=[p for p in rules.pending_tariffs(db) if p["org_id"] != org_id])


def set_service_price(db: Session, *, user: User, svc: Service, price_raw: str) -> None:
    try:
        price = Decimal(price_raw.replace(" ", "").replace(",", "."))
    except Exception:
        raise ValueError("Цена — числом")
    if price <= 0:
        raise ValueError("Цена — больше нуля")
    if price == Decimal(svc.price):
        return
    svc.price = price
    db.add(ServicePriceHistory(service_id=svc.id, price=price, effective_date=date.today(), changed_by=user.id))


def set_frozen(db: Session, *, user: User, org: Organization, raw: str) -> None:
    try:
        v = float(raw.replace(",", ".").replace("%", "").strip())
    except ValueError:
        raise ValueError("Заморозка — процент от тарифа, числом")
    if not 0 <= v <= 100:
        raise ValueError("Заморозка — от 0 до 100 %")
    old = org.frozen_discount_percent
    org.frozen_discount_percent = v
    db.add(AuditLog(entity_type="organization", entity_id=org.id, action="update", user_id=user.id,
                    old_data={"frozen_discount_percent": float(old) if old is not None else None},
                    new_data={"frozen_discount_percent": v}))


def set_holder(db: Session, *, user: User, org: Organization, holder_id: int | None) -> None:
    old = org.cash_recipient_user_id
    org.cash_recipient_user_id = holder_id
    db.add(AuditLog(entity_type="organization", entity_id=org.id, action="update", user_id=user.id,
                    old_data={"cash_recipient_user_id": old}, new_data={"cash_recipient_user_id": holder_id}))


def set_minor(db: Session, *, user: User, minor_ids: set[int]) -> None:
    changed = {}
    for c in db.query(ProductCategory).all():
        level = "minor" if c.id in minor_ids else "stock"
        if c.level != level:
            changed[c.name] = level
            c.level = level
    if changed:
        db.add(AuditLog(entity_type="product_category", entity_id=0, action="update", user_id=user.id,
                        new_data=changed))
