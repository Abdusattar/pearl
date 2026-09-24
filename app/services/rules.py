"""Правила бизнеса из Настроек (блок «Настройки», макет 21.09).

Раньше были константами в коде (cash.POCKET_DELTA_THRESHOLD, today.DEBT_OLD_DAYS,
salary.PAY_DAY и ставки, kitchen.WORKING_WEEKDAYS). Теперь владелец меняет их сам;
значение по умолчанию — здесь, и оно же действует, пока в Настройках ничего не
меняли. Софт пойдёт другим садикам: правила — настройки, а не код.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import AppSetting, AuditLog, User

RULES = {
    "pocket_delta_threshold": {"default": 500, "title": "Пересчёт наличных с разницей",
                               "unit": "сом", "min": 0, "max": 100000},
    "debt_old_days": {"default": 30, "title": "Долг поставщику «пора платить»", "unit": "дней", "min": 1, "max": 365},
    "pay_day": {"default": 10, "title": "День выдачи зарплаты", "unit": "число", "min": 1, "max": 28},
    "socfond_rate": {"default": 10, "title": "Соцфонд с работника", "unit": "%", "min": 0, "max": 50},
    "income_tax_rate": {"default": 10, "title": "Подоходный", "unit": "%", "min": 0, "max": 50},
    "tax_deduction": {"default": 650, "title": "Стандартный вычет", "unit": "сом", "min": 0, "max": 100000},
    "kitchen_weekdays": {"default": [0, 1, 2, 3, 4], "title": "Кухня работает"},
    # Склад по нормам (23.09): ключевые продукты считаем раз в неделю, лист кухни —
    # по желанию. Пока лист обязателен, система напоминает о каждом невнесённом дне.
    "key_products": {"default": [], "title": "Ключевые продукты"},
    # Четверг (владелец 23.09): после кухни, до пятничной закладки; пятница утром — запасной день.
    "count_weekday": {"default": 3, "title": "День пересчёта ключевых"},
    "kitchen_sheet_required": {"default": False, "title": "Лист кухни обязателен"},
    # Едоки и меню (владелец 24.09): вопрос в 11:20, напоминание в 13:30, если пусто
    "meal_ask_time": {"default": "11:20", "title": "Во сколько бот спрашивает, сколько едят"},
    "meal_remind_time": {"default": "13:30", "title": "Напоминание про едоков, если не записано"},
    # Первая неделя новой схемы (владелец 23.09): застрявшее — только владельцу, учредителям — с этой даты
    "escalate_from": {"default": "2026-10-01", "title": "С какого дня застрявшее видят учредители"},
    # Привыкание (владелец 23.09): каждое утро лично — «на руках X, верно?» и остаток счёта за вчера;
    # ошибку ловим в тот же день, а не ищем задним числом. Потом — только по расхождению.
    "daily_checks_until": {"default": "2026-10-07", "title": "До какого дня бот каждое утро сверяет кассу и счёт"},
}
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
PENDING_TARIFFS = "pending_tariffs"   # [{"org_id", "price", "from": "YYYY-MM-01", "by"}]


def get(db: Session, key: str):
    row = db.get(AppSetting, key)
    if row is not None:
        return row.value
    return RULES[key]["default"] if key in RULES else None


def put(db: Session, *, user: User, key: str, value) -> None:
    row = db.get(AppSetting, key)
    old = row.value if row is not None else (RULES[key]["default"] if key in RULES else None)
    if old == value:
        return
    if row is None:
        db.add(AppSetting(key=key, value=value, updated_by=user.id))
    else:
        row.value, row.updated_by, row.updated_at = value, user.id, datetime.now()
    db.add(AuditLog(entity_type="app_setting", entity_id=0, action="update", user_id=user.id,
                    old_data={"key": key, "value": old}, new_data={"key": key, "value": value}))
    db.flush()


def set_number(db: Session, *, user: User, key: str, raw: str) -> None:
    spec = RULES[key]
    try:
        v = Decimal(raw.replace(" ", "").replace(",", "."))
    except Exception:
        raise ValueError(f"{spec['title']}: нужно число")
    if not (spec["min"] <= v <= spec["max"]):
        raise ValueError(f"{spec['title']}: от {spec['min']} до {spec['max']}")
    put(db, user=user, key=key, value=int(v) if v == int(v) else float(v))


# ── удобные чтения ────────────────────────────────────────────────────────

def pocket_delta_threshold(db: Session) -> Decimal:
    return Decimal(str(get(db, "pocket_delta_threshold")))


def debt_old_days(db: Session) -> int:
    return int(get(db, "debt_old_days"))


def pay_day(db: Session) -> int:
    return int(get(db, "pay_day"))


def tax_rates(db: Session) -> dict:
    return {"soc": Decimal(str(get(db, "socfond_rate"))) / 100,
            "tax": Decimal(str(get(db, "income_tax_rate"))) / 100,
            "deduction": Decimal(str(get(db, "tax_deduction")))}


def kitchen_weekdays(db: Session) -> set[int]:
    return set(int(x) for x in get(db, "kitchen_weekdays"))


def key_products(db: Session) -> list[int]:
    return [int(x) for x in (get(db, "key_products") or [])]


def count_weekday(db: Session) -> int:
    return int(get(db, "count_weekday"))


def daily_checks_until(db: Session):
    from datetime import date as _date
    return _date.fromisoformat(str(get(db, "daily_checks_until")))


def escalate_from(db: Session):
    from datetime import date as _date
    return _date.fromisoformat(str(get(db, "escalate_from")))


def kitchen_sheet_required(db: Session) -> bool:
    return bool(get(db, "kitchen_sheet_required"))


def weekdays_text(days) -> str:
    days = sorted(days)
    if days == list(range(days[0], days[-1] + 1)) and len(days) > 2:
        return f"{WEEKDAYS[days[0]]} – {WEEKDAYS[days[-1]]}"
    return ", ".join(WEEKDAYS_SHORT[d] for d in days)


# ── отложенные тарифы ─────────────────────────────────────────────────────

def pending_tariffs(db: Session) -> list[dict]:
    return list(get(db, PENDING_TARIFFS) or [])


def apply_pending_tariffs(db: Session, period: date) -> int:
    """Тариф «с октября» включается в начале октября, до начислений месяца.
    Вызывается из billing.generate_monthly_charges под тем же замком."""
    from app.models import Organization, Service, ServicePriceHistory
    items = pending_tariffs(db)
    due = [p for p in items if date.fromisoformat(p["from"]) <= period]
    if not due:
        return 0
    for p in due:
        org = db.get(Organization, p["org_id"])
        if org is None:
            continue
        svc = (db.query(Service).filter(Service.organization_id == org.id, Service.is_tuition.is_(True),
                                        Service.deleted_at.is_(None)).first())
        if svc is None:
            svc = Service(organization_id=org.id, name="Обучение", price=p["price"], is_tuition=True, is_recurring=True)
            db.add(svc)
            db.flush()
        svc.price = p["price"]
        db.add(ServicePriceHistory(service_id=svc.id, price=p["price"], effective_date=date.fromisoformat(p["from"]),
                                   changed_by=p.get("by")))
    row = db.get(AppSetting, PENDING_TARIFFS)
    row.value = [p for p in items if p not in due]
    db.flush()
    return len(due)
