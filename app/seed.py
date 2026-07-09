"""Seed initial data. Idempotent — safe to run multiple times."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal
from app.models import ExpenseCategory, Organization, User


def seed():
    db = SessionLocal()
    try:
        # Organizations
        if not db.query(Organization).first():
            root   = Organization(id=1, name="Жемчужина", parent_id=None, type="root")
            school = Organization(id=2, name="Школа", parent_id=1, type="school")
            kinder = Organization(id=3, name="Садики", parent_id=1, type="kindergarten")
            sok    = Organization(id=4, name="Садик Сокулук", parent_id=3, type="kindergarten")
            koj    = Organization(id=5, name="Садик Кожомкул", parent_id=3, type="kindergarten")
            db.add_all([root, school, kinder, sok, koj])
            db.commit()
            print("✅ Организации созданы")
        else:
            print("— Организации уже есть")

        # Global expense categories
        if not db.query(ExpenseCategory).first():
            cats = [
                ExpenseCategory(id=1,  name="Питание",        parent_id=None, organization_id=None),
                ExpenseCategory(id=2,  name="Продукты",       parent_id=1,    organization_id=None),
                ExpenseCategory(id=3,  name="Готовая еда",    parent_id=1,    organization_id=None),
                ExpenseCategory(id=4,  name="Хозяйство",      parent_id=None, organization_id=None),
                ExpenseCategory(id=5,  name="Мыломойка",      parent_id=4,    organization_id=None),
                ExpenseCategory(id=6,  name="Инвентарь",      parent_id=4,    organization_id=None),
                ExpenseCategory(id=7,  name="Канцелярия",     parent_id=None, organization_id=None),
                ExpenseCategory(id=8,  name="Зарплаты",       parent_id=None, organization_id=None),
                ExpenseCategory(id=9,  name="Коммунальные",   parent_id=None, organization_id=None),
                ExpenseCategory(id=10, name="Транспорт",      parent_id=None, organization_id=None),
                ExpenseCategory(id=11, name="Прочее",         parent_id=None, organization_id=None),
            ]
            db.add_all(cats)
            db.commit()
            print("✅ Категории расходов созданы")
        else:
            print("— Категории уже есть")

        # Test users
        if not db.query(User).first():
            users = [
                User(id=1, name="Абдусаттар", role="owner",    organization_id=1, tg_id=None),
                User(id=2, name="Айжан",      role="director", organization_id=2, tg_id=None),
                User(id=3, name="Мунара",     role="manager",  organization_id=3, tg_id=None),
            ]
            db.add_all(users)
            db.commit()
            print("✅ Пользователи созданы (Абдусаттар / Айжан / Мунара)")
        else:
            print("— Пользователи уже есть")

        print("\n✅ Seed завершён. Запускай: uvicorn app.main:app --reload --port 8001")

    finally:
        db.close()


if __name__ == "__main__":
    seed()
