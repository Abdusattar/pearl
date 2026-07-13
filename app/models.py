from sqlalchemy import (
    Column, Integer, String, Numeric, Date, DateTime, Text, Boolean,
    ForeignKey, CheckConstraint, UniqueConstraint, func
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship, backref
from app.database import Base


class Organization(Base):
    __tablename__ = "organizations"
    id        = Column(Integer, primary_key=True)
    name      = Column(String(100), nullable=False)
    parent_id = Column(Integer, ForeignKey("organizations.id"))
    type      = Column(String(20), nullable=False)  # root|school|kindergarten
    created_at = Column(DateTime, server_default=func.now())

    children  = relationship("Organization", foreign_keys=[parent_id],
                             backref=backref("parent", remote_side="Organization.id"))


class User(Base):
    __tablename__ = "users"
    id              = Column(Integer, primary_key=True)
    tg_id           = Column(Integer, unique=True)
    name            = Column(String(100), nullable=False)
    role            = Column(String(20), nullable=False)  # owner|director|manager|staff
    organization_id = Column(Integer, ForeignKey("organizations.id"))
    password_hash   = Column(String(200))
    created_at      = Column(DateTime, server_default=func.now())
    deleted_at      = Column(DateTime)


class Group(Base):
    __tablename__ = "groups"
    id              = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    name            = Column(String(50), nullable=False)
    type            = Column(String(20), nullable=False)  # class|kindergarten_group
    updated_at      = Column(DateTime, server_default=func.now(), onupdate=func.now())
    deleted_at      = Column(DateTime)


class Student(Base):
    __tablename__ = "students"
    id              = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    name            = Column(String(100), nullable=False)
    last_name       = Column(String(50))
    first_name      = Column(String(50))
    patronymic      = Column(String(50))
    pin             = Column(String(20), unique=True, nullable=False)
    parent_name     = Column(String(100))
    parent_contact  = Column(String(200))
    status          = Column(String(10), default="active")
    extra           = Column(JSONB)
    # Скидка на тариф за учёбу — в сомах от базового тарифа
    # (DEFAULT_MONTHLY_FEE в services/billing.py), не в процентах — так
    # реально считает Мунара/Абдусаттар (13.07, было испробовано с
    # discount_percent 12.07, ни разу не заполнилось на практике). С
    # обязательной причиной и аудитом (кто/когда) — старую безусловную
    # ручную корректировку баланса убрали 04.07 именно из-за отсутствия
    # этого. Не путать со "скидкой за пропуски" (100 сом/день, решено
    # 10.07, не реализовано) — это два разных механизма.
    discount_amount = Column(Numeric(10, 2), nullable=False, default=0, server_default='0')
    discount_reason  = Column(Text)
    discount_set_by  = Column(Integer, ForeignKey("users.id"))
    discount_set_at  = Column(DateTime)
    created_at      = Column(DateTime, server_default=func.now())
    updated_at      = Column(DateTime, server_default=func.now(), onupdate=func.now())
    deleted_at      = Column(DateTime)

    enrollments = relationship("Enrollment", back_populates="student")


class Enrollment(Base):
    __tablename__ = "enrollments"
    id         = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    group_id   = Column(Integer, ForeignKey("groups.id"), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date   = Column(Date)
    created_at = Column(DateTime, server_default=func.now())

    student = relationship("Student", back_populates="enrollments")
    group   = relationship("Group")


class Attendance(Base):
    """Утренняя отметка посещаемости — по одной записи на ребёнка на день.
    present=False означает "отсутствует". Используется, чтобы повар готовил
    на факт явки, а не на списочный состав группы (см. wiki/architecture/warehouse_module.md)."""
    __tablename__ = "attendance"
    __table_args__ = (
        UniqueConstraint('student_id', 'date', name='uq_attendance_student_date'),
    )
    id              = Column(Integer, primary_key=True)
    student_id      = Column(Integer, ForeignKey("students.id"), nullable=False)
    date            = Column(Date, nullable=False)
    present         = Column(Boolean, nullable=False, default=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    created_by      = Column(Integer, ForeignKey("users.id"))
    created_at      = Column(DateTime, server_default=func.now())
    updated_at      = Column(DateTime, server_default=func.now(), onupdate=func.now())

    student = relationship("Student")


class ExpenseCategory(Base):
    __tablename__ = "expense_categories"
    id                 = Column(Integer, primary_key=True)
    organization_id    = Column(Integer, ForeignKey("organizations.id"))  # NULL = глобальная
    name               = Column(String(100), nullable=False)
    parent_id          = Column(Integer, ForeignKey("expense_categories.id"))
    warehouse_eligible = Column(Boolean, nullable=False, default=False, server_default='false')
    created_at         = Column(DateTime, server_default=func.now())

    children = relationship("ExpenseCategory", foreign_keys=[parent_id],
                            backref=backref("parent", remote_side="ExpenseCategory.id"))


class Receipt(Base):
    __tablename__ = "receipts"
    id               = Column(Integer, primary_key=True)
    organization_id  = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    file_path        = Column(String(500), nullable=False)
    file_hash        = Column(String(64), unique=True)  # sha256 — защита от дублей (бот+веб)
    ocr_raw          = Column(Text)
    ocr_status       = Column(String(20), default="pending")
    amount_detected  = Column(Numeric(12, 2))
    amount_confirmed = Column(Numeric(12, 2))
    confirmed_by     = Column(Integer, ForeignKey("users.id"))
    confirmed_at     = Column(DateTime)
    created_by       = Column(Integer, ForeignKey("users.id"))
    created_at       = Column(DateTime, server_default=func.now())


class Supplier(Base):
    __tablename__ = "suppliers"
    id         = Column(Integer, primary_key=True)
    name       = Column(String(200), nullable=False)
    phone      = Column(String(20), unique=True, nullable=True)
    inn        = Column(String(20), nullable=True)  # для реестров бухгалтеру (ст. 177 НК КР)
    created_at = Column(DateTime, server_default=func.now())


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(
            "type = 'expense' OR student_id IS NOT NULL",
            name="income_needs_student"
        ),
    )
    id              = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    type            = Column(String(10), nullable=False)  # income|expense
    amount          = Column(Numeric(12, 2), nullable=False)
    amount_paid     = Column(Numeric(12, 2))  # NULL = оплачено полностью (amount); меньше amount = есть долг поставщику
    due_date        = Column(Date)  # плановая дата погашения долга, необязательно
    category_id     = Column(Integer, ForeignKey("expense_categories.id"))
    supplier_id     = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    student_id      = Column(Integer, ForeignKey("students.id"))
    description     = Column(Text)
    date            = Column(Date, nullable=False)
    external_txn_id = Column(String(50), unique=True)  # Optima txn_id — идемпотентность
    recurring_template_id = Column(Integer, ForeignKey("recurring_expense_templates.id"), nullable=True)
    # За какой месяц расход (первое число месяца) — НЕ дата фактической оплаты
    # (`date`). Нужно отдельно: расход за июль иногда проводят в начале августа
    # (решено 12.07). Заполняется только для recurring_template_id — обычные
    # транзакции NULL, месяц для них не отслеживается отдельно от date.
    period = Column(Date, nullable=True)
    created_by      = Column(Integer, ForeignKey("users.id"))
    created_at      = Column(DateTime, server_default=func.now())
    updated_at      = Column(DateTime, server_default=func.now(), onupdate=func.now())
    deleted_at      = Column(DateTime)


class ReceiptTransaction(Base):
    __tablename__ = "receipt_transactions"
    receipt_id     = Column(Integer, ForeignKey("receipts.id"), primary_key=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), primary_key=True)
    amount         = Column(Numeric(12, 2), nullable=False)


class Product(Base):
    __tablename__ = "products"
    id                  = Column(Integer, primary_key=True)
    name                = Column(String(100), nullable=False, unique=True)
    unit                = Column(String(10), default="кг")   # кг, л, шт, г, уп
    category            = Column(String(50))                  # мясо, молочные, крупы, овощи, прочее — для склада/AI-подсказок
    expense_category_id = Column(Integer, ForeignKey("expense_categories.id"))  # статья расходов — для авторазбивки по категориям
    is_standard         = Column(Boolean, default=False, nullable=False)
    created_at          = Column(DateTime, server_default=func.now())

    aliases = relationship("ProductAlias", back_populates="product")


class ProductAlias(Base):
    __tablename__ = "product_aliases"
    id         = Column(Integer, primary_key=True)
    raw_text   = Column(String(200), nullable=False, unique=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    product = relationship("Product", back_populates="aliases")


class Dish(Base):
    """Каталог блюд — глобальный, не org-scoped (по аналогии с Product).
    Создаётся через get_or_create_dish() с fuzzy-защитой от опечаток —
    см. app/services/dishes.py и wiki/blueprints/menu_module.md."""
    __tablename__ = "dishes"
    id         = Column(Integer, primary_key=True)
    name       = Column(String(150), nullable=False, unique=True)
    created_at = Column(DateTime, server_default=func.now())


class MenuEntry(Base):
    """Меню на дату/приём пищи. Несколько строк на один (date, meal_type)
    допустимо — обед может состоять из супа+второго+компота отдельными
    записями (решено 10.07)."""
    __tablename__ = "menu_entries"
    id              = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    date            = Column(Date, nullable=False)
    meal_type       = Column(String(20), nullable=False)
    dish_id         = Column(Integer, ForeignKey("dishes.id"), nullable=False)
    created_by      = Column(Integer, ForeignKey("users.id"))
    created_at      = Column(DateTime, server_default=func.now())

    dish         = relationship("Dish")
    organization = relationship("Organization")


class ReceiptItem(Base):
    __tablename__ = "receipt_items"
    id          = Column(Integer, primary_key=True)
    receipt_id  = Column(Integer, ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False)
    name        = Column(String(200), nullable=False)  # сырой текст из OCR
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=True)
    qty         = Column(Numeric(10, 3))
    unit_price  = Column(Numeric(12, 2))
    total_price = Column(Numeric(12, 2), nullable=True)  # null = OCR не нашёл цену, нужно дозаполнить

    product = relationship("Product")


class WarehouseReceipt(Base):
    __tablename__ = "warehouse_receipts"
    id              = Column(Integer, primary_key=True)
    date            = Column(Date, nullable=False)
    product_id      = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity        = Column(Numeric(10, 3), nullable=False)
    price_per_unit  = Column(Numeric(10, 2), nullable=False)
    total_cost      = Column(Numeric(12, 2), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    supplier_name   = Column(String(200))
    transaction_id  = Column(Integer, ForeignKey("transactions.id"), nullable=True)
    created_by      = Column(Integer, ForeignKey("users.id"))
    created_at      = Column(DateTime, server_default=func.now())
    deleted_at      = Column(DateTime)

    product      = relationship("Product")
    organization = relationship("Organization")


class WriteOff(Base):
    __tablename__ = "write_offs"
    id              = Column(Integer, primary_key=True)
    date            = Column(Date, nullable=False)
    product_id      = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity        = Column(Numeric(10, 3), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    children_count  = Column(Integer)
    reason          = Column(String(100), default="питание детей")
    meal_type       = Column(String(20))  # завтрак/обед/полдник/ужин — заполняется через /warehouse/writeoff/meal
    dish_id         = Column(Integer, ForeignKey("dishes.id"), nullable=True)  # nullable — списание должно проходить и без выбранного блюда
    created_by      = Column(Integer, ForeignKey("users.id"))
    created_at      = Column(DateTime, server_default=func.now())
    deleted_at      = Column(DateTime)

    product      = relationship("Product")
    organization = relationship("Organization")
    dish         = relationship("Dish")


class Asset(Base):
    """Реестр капитальных покупок (мебель, оборудование, игровые комплексы) — лёгкая версия,
    без расчёта амортизации (это решает бухгалтер по своему методу, когда появится 1С).
    Просто фиксирует сам факт: что купили, когда, за сколько — база для будущей амортизации."""
    __tablename__ = "assets"
    id              = Column(Integer, primary_key=True)
    name            = Column(String(200), nullable=False)
    description     = Column(Text)
    category        = Column(String(50))  # мебель, оборудование, игровой инвентарь, прочее
    purchase_date   = Column(Date, nullable=False)
    cost            = Column(Numeric(12, 2), nullable=False)
    useful_life_months = Column(Integer, nullable=True)  # NULL = не амортизируется (10.07)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    created_by      = Column(Integer, ForeignKey("users.id"))
    created_at      = Column(DateTime, server_default=func.now())
    deleted_at      = Column(DateTime)

    organization = relationship("Organization")


class Employee(Base):
    """Реестр сотрудников. ФОТ на `/employees/` по-прежнему считается на лету
    (SUM оклада активных) как оценка текущего месяца — но с 12.07 может
    также подтверждаться проводкой через RecurringExpenseTemplate
    (source='employees_sum'), которая становится источником истины для уже
    закрытых месяцев. См. wiki/blueprints/unit_economics_module.md."""
    __tablename__ = "employees"
    id              = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    full_name       = Column(String(150), nullable=False)
    role            = Column(String(100))
    salary          = Column(Numeric(12, 2), nullable=False)
    status          = Column(String(20), nullable=False, default="active")  # active|terminated
    created_by      = Column(Integer, ForeignKey("users.id"))
    created_at      = Column(DateTime, server_default=func.now())

    organization = relationship("Organization")


class RecurringExpenseTemplate(Base):
    """Справочник ежемесячных расходов (ФОТ/Охрана/Коммуналка и т.п.) —
    полуавтомат: система подсказывает сумму, человек подтверждает кнопкой,
    подтверждение создаёт обычный Transaction (не отдельная сущность —
    список статусов строится на лету по Transaction.recurring_template_id).
    Решено 12.07 (см. logs/terminal/2026-07-12.md): НЕ строгая идемпотентность
    "одна проводка на месяц" — аванс+остаток зарплаты это две проводки в
    одном месяце по одному шаблону, осознанно."""
    __tablename__ = "recurring_expense_templates"
    id              = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    name            = Column(String(150), nullable=False)
    category_id     = Column(Integer, ForeignKey("expense_categories.id"), nullable=False)
    # 'employees_sum' — подсказка = живой SUM(Employee.salary активных);
    # 'last_amount' — подсказка = сумма последней проводки по этому шаблону;
    # 'manual' — без подсказки (коммуналка меняется каждый месяц по счётчику)
    amount_source   = Column(String(20), nullable=False, default="manual")
    owner_only      = Column(Boolean, nullable=False, default=False)  # видят/подтверждают только owner/founder
    active          = Column(Boolean, nullable=False, default=True)
    created_by      = Column(Integer, ForeignKey("users.id"))
    created_at      = Column(DateTime, server_default=func.now())

    organization = relationship("Organization")
    category     = relationship("ExpenseCategory")


class Service(Base):
    """Услуга с ценой за месяц. Обычные (проезд, секция и т.п.) — опциональные,
    подключаются по ребёнку через StudentService (чекбокс на карточке).
    is_tuition=True — базовый тариф за учёбу (13.07): один такой на объект,
    применяется автоматически всем активным детям, не через чекбокс, цена
    редактируется здесь же (на /services/), чтобы поднять тариф не деплоем
    кода, а обычной правкой. Скидка ребёнку — Student.discount_amount, в сомах
    от этой цены."""
    __tablename__ = "services"
    id              = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    name            = Column(String(100), nullable=False)
    price           = Column(Numeric(10, 2), nullable=False)
    is_tuition      = Column(Boolean, nullable=False, default=False, server_default='false')
    created_at      = Column(DateTime, server_default=func.now())
    deleted_at      = Column(DateTime)


class StudentService(Base):
    """Подключение услуги ребёнку — с историей (start_date/end_date), как Enrollment."""
    __tablename__ = "student_services"
    id         = Column(Integer, primary_key=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=False)
    service_id = Column(Integer, ForeignKey("services.id"), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date   = Column(Date)
    created_at = Column(DateTime, server_default=func.now())

    service = relationship("Service")


class Charge(Base):
    """Начисление ребёнку (ежемесячная учёба+услуги, либо ручная корректировка).
    Баланс = сумма Charge - сумма Transaction(type=income) по этому student_id."""
    __tablename__ = "charges"
    id          = Column(Integer, primary_key=True)
    student_id  = Column(Integer, ForeignKey("students.id"), nullable=False)
    amount      = Column(Numeric(12, 2), nullable=False)
    description = Column(Text)
    date        = Column(Date, nullable=False)
    created_at  = Column(DateTime, server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id          = Column(Integer, primary_key=True)
    entity_type = Column(String(50), nullable=False)
    entity_id   = Column(Integer, nullable=False)
    action      = Column(String(20), nullable=False)  # insert|update|delete
    user_id     = Column(Integer, ForeignKey("users.id"))
    old_data    = Column(JSONB)
    new_data    = Column(JSONB)
    created_at  = Column(DateTime, server_default=func.now())


class OptimaLog(Base):
    """Каждый входящий запрос от Optima (check/pay), включая отклонённые —
    неверный PIN, ребёнок выбыл, дубликат и т.п. Отдельно от Transaction,
    потому что многие попытки вообще не доходят до создания транзакции.
    Видно только системному интегратору (13.07) — раньше приходилось
    смотреть логи Railway через CLI на каждый вопрос банка."""
    __tablename__ = "optima_log"
    id          = Column(Integer, primary_key=True)
    command     = Column(String(20))
    account     = Column(String(20))
    txn_id      = Column(String(50))
    sum         = Column(String(20))
    result_code = Column(Integer, nullable=False)
    comment     = Column(Text)
    client_ip   = Column(String(50))
    created_at  = Column(DateTime, server_default=func.now())
