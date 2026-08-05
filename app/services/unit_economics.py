from datetime import date as date_type, timedelta
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Asset, Charge, Employee, ExpenseCategory, Student, Transaction, WarehouseReceipt, WriteOff

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


def month_end(m_start: date_type) -> date_type:
    if m_start.month == 12:
        next_start = date_type(m_start.year + 1, 1, 1)
    else:
        next_start = date_type(m_start.year, m_start.month + 1, 1)
    return next_start - timedelta(days=1)


# Кухня общая на Школу (id=2) и Садик Сокулук (id=4) — один повар, один
# закуп, готовят на оба объекта сразу (подтверждено пользователем 05.08).
# Приход товара может быть проведён под любым из двух id, списание сейчас
# всегда идёт под Садик Сокулук (у Школы пока нет своих групп/явки — пилот
# ограничен Сокулуком). Считать стоимость питания по одному из двух id
# отдельно — недооценивать её на сумму прихода, проведённого под другим.
# Садик Кожомкул — отдельная кухня, в эту группу не входит.
SHARED_KITCHEN_ORG_IDS = {2, 4}


def kitchen_group(organization_id: int) -> set[int]:
    if organization_id in SHARED_KITCHEN_ORG_IDS:
        return SHARED_KITCHEN_ORG_IDS
    return {organization_id}


def _avg_price_map(db: Session, org_ids: set[int]) -> dict[int, Decimal]:
    """Средняя цена прихода по каждому товару (вся история, без FIFO/партий)
    — та же методика валюации, что у остатков склада (warehouse._get_balances)."""
    rows = (
        db.query(
            WarehouseReceipt.product_id,
            func.sum(WarehouseReceipt.quantity),
            func.sum(WarehouseReceipt.total_cost),
        )
        .filter(WarehouseReceipt.organization_id.in_(org_ids), WarehouseReceipt.deleted_at.is_(None))
        .group_by(WarehouseReceipt.product_id)
        .all()
    )
    result = {}
    for product_id, qty, cost in rows:
        qty = Decimal(qty)
        result[product_id] = (Decimal(cost) / qty) if qty > 0 else Decimal(0)
    return result


def monthly_food_cost(db: Session, organization_id: int, m_start: date_type) -> Decimal:
    """Стоимость списанного питания за месяц — сумма(WriteOff.quantity ×
    средняя цена товара). Не Transaction — списание не покупка, у него нет
    своей цены (см. writeoff_calc), цена берётся из прихода на склад.
    Для Школы/Садика Сокулук считается по общей кухне (см. kitchen_group) —
    иначе цифра зависела бы от того, под каким из двух id провели приход."""
    org_ids = kitchen_group(organization_id)
    avg_prices = _avg_price_map(db, org_ids)
    rows = (
        db.query(WriteOff.product_id, func.sum(WriteOff.quantity))
        .filter(
            WriteOff.organization_id.in_(org_ids),
            WriteOff.date >= m_start,
            WriteOff.date <= month_end(m_start),
            WriteOff.deleted_at.is_(None),
        )
        .group_by(WriteOff.product_id)
        .all()
    )
    total = Decimal(0)
    for product_id, qty in rows:
        total += Decimal(qty) * avg_prices.get(product_id, Decimal(0))
    return total


def category_subtree_ids(db: Session, root_name: str) -> list[int]:
    """id корневой категории + прямых детей (дерево ExpenseCategory сейчас
    двухуровневое — см. wiki/architecture/expense_categories)."""
    root = (
        db.query(ExpenseCategory.id)
        .filter(ExpenseCategory.name == root_name, ExpenseCategory.organization_id.is_(None))
        .first()
    )
    if not root:
        return []
    children = db.query(ExpenseCategory.id).filter(ExpenseCategory.parent_id == root[0]).all()
    return [root[0]] + [c[0] for c in children]


def monthly_expense_total(db: Session, organization_id: int, category_ids: list[int], m_start: date_type) -> Decimal:
    """Сумма проведённых расходов (Transaction) за месяц по набору категорий
    — считается по `date` (когда реально провели), не по `period`: `period`
    заполняется только для recurring_template_id, обычные проводки его не
    используют (см. Transaction.period)."""
    if not category_ids:
        return Decimal(0)
    total = (
        db.query(func.sum(Transaction.amount))
        .filter(
            Transaction.organization_id == organization_id,
            Transaction.type == "expense",
            Transaction.category_id.in_(category_ids),
            Transaction.deleted_at.is_(None),
            func.extract("year", Transaction.date) == m_start.year,
            func.extract("month", Transaction.date) == m_start.month,
        )
        .scalar()
    )
    return total or Decimal(0)


def monthly_billing_summary(db: Session, organization_id: int, m_start: date_type) -> dict:
    """Начислено vs оплачено за месяц — не «100% родителей заплатили»,
    а факт что механизм начисления/фиксации оплаты работает и даёт цифры."""
    m_end = month_end(m_start)
    charged = (
        db.query(func.sum(Charge.amount))
        .join(Student, Student.id == Charge.student_id)
        .filter(Student.organization_id == organization_id, Charge.date >= m_start, Charge.date <= m_end)
        .scalar()
    ) or Decimal(0)
    paid = (
        db.query(func.sum(Transaction.amount))
        .join(Student, Student.id == Transaction.student_id)
        .filter(
            Student.organization_id == organization_id,
            Transaction.type == "income",
            Transaction.deleted_at.is_(None),
            Transaction.date >= m_start,
            Transaction.date <= m_end,
        )
        .scalar()
    ) or Decimal(0)
    return {"charged": charged, "paid": paid}
