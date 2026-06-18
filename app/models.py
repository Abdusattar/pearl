from sqlalchemy import (
    Column, Integer, String, Numeric, Date, DateTime, Text, Boolean,
    ForeignKey, CheckConstraint, func
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
    role            = Column(String(20), nullable=False)  # owner|director|manager|teacher
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
    pin             = Column(String(20), unique=True, nullable=False)
    parent_contact  = Column(String(200))
    status          = Column(String(10), default="active")
    extra           = Column(JSONB)
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
    category_id     = Column(Integer, ForeignKey("expense_categories.id"))
    student_id      = Column(Integer, ForeignKey("students.id"))
    description     = Column(Text)
    date            = Column(Date, nullable=False)
    external_txn_id = Column(String(50), unique=True)  # Optima txn_id — идемпотентность
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
    id         = Column(Integer, primary_key=True)
    name       = Column(String(100), nullable=False, unique=True)
    unit       = Column(String(10), default="кг")   # кг, л, шт, г, уп
    category   = Column(String(50))                  # мясо, молочные, крупы, овощи, прочее
    created_at = Column(DateTime, server_default=func.now())

    aliases = relationship("ProductAlias", back_populates="product")


class ProductAlias(Base):
    __tablename__ = "product_aliases"
    id         = Column(Integer, primary_key=True)
    raw_text   = Column(String(200), nullable=False, unique=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    product = relationship("Product", back_populates="aliases")


class ReceiptItem(Base):
    __tablename__ = "receipt_items"
    id          = Column(Integer, primary_key=True)
    receipt_id  = Column(Integer, ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False)
    name        = Column(String(200), nullable=False)  # сырой текст из OCR
    product_id  = Column(Integer, ForeignKey("products.id"), nullable=True)
    qty         = Column(Numeric(10, 3))
    unit_price  = Column(Numeric(12, 2))
    total_price = Column(Numeric(12, 2), nullable=False)

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
    created_by      = Column(Integer, ForeignKey("users.id"))
    created_at      = Column(DateTime, server_default=func.now())
    deleted_at      = Column(DateTime)

    product      = relationship("Product")
    organization = relationship("Organization")


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
