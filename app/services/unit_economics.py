from datetime import date as date_type
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Asset, Employee

# Сроки по умолчанию — ответ финэксперта 10.07 (НК КР, амортизационные группы),
# см. wiki/blueprints/unit_economics_module.md
DEFAULT_USEFUL_LIFE_MONTHS = {
    "мебель": 60,
    "оборудование": 36,
    "игровой инвентарь": 24,
    "прочее": None,
}


def monthly_payroll(db: Session, organization_id: int) -> Decimal:
    """ФОТ — сумма окладов активных сотрудников. Считается на лету, не
    хранится (см. Employee, разбор почему generate_monthly_charges не подходит)."""
    total = (
        db.query(func.sum(Employee.salary))
        .filter(Employee.organization_id == organization_id, Employee.status == "active")
        .scalar()
    )
    return total or Decimal(0)


def _month_idx(d: date_type) -> int:
    return d.year * 12 + d.month


def _depreciated_up_to(asset: Asset, month_idx: int) -> Decimal:
    """Сколько от стоимости актива самортизировано к концу указанного месяца
    (включительно). Начисление стартует со следующего месяца после покупки —
    уточнено финэкспертом 10.07."""
    if not asset.useful_life_months:
        return Decimal(0)
    start_idx = _month_idx(asset.purchase_date) + 1  # первый месяц начисления
    months_elapsed = month_idx - start_idx + 1
    if months_elapsed <= 0:
        return Decimal(0)
    monthly_amount = asset.cost / asset.useful_life_months
    return min(asset.cost, monthly_amount * min(months_elapsed, asset.useful_life_months))


def asset_monthly_amortization(asset: Asset, on_date: date_type | None = None) -> Decimal:
    """Амортизация ЭТОГО актива за месяц, содержащий on_date. Чистая функция —
    не хранит состояние, считается заново на любую дату (см. blueprint)."""
    on_date = on_date or date_type.today()
    this_month = _month_idx(on_date)
    return _depreciated_up_to(asset, this_month) - _depreciated_up_to(asset, this_month - 1)


def monthly_amortization(db: Session, organization_id: int, on_date: date_type | None = None) -> Decimal:
    """Суммарная амортизация за месяц по всем непросроченным активам организации."""
    assets = (
        db.query(Asset)
        .filter(Asset.organization_id == organization_id, Asset.deleted_at.is_(None))
        .all()
    )
    return sum((asset_monthly_amortization(a, on_date) for a in assets), Decimal(0))
