"""Убрать все зарплатные проводки (Махабат вводит зарплату заново, 16.09).

Мягкое удаление: `deleted_at`, запись в AuditLog от имени владельца (user 1).
Ничего физически не стирается: старый вход и касса перестают их считать,
история остаётся. Без `--apply` только показывает список.

Запуск: python scripts/remove_salary_entries.py [--apply]
Прод — через DATABASE_URL в окружении.
"""
import sys
from datetime import datetime

from app.database import SessionLocal
from app.models import AuditLog, Employee, Transaction

apply = "--apply" in sys.argv
db = SessionLocal()
rows = (db.query(Transaction, Employee.full_name)
        .outerjoin(Employee, Employee.id == Transaction.employee_id)
        .filter(Transaction.employee_id.isnot(None), Transaction.deleted_at.is_(None))
        .order_by(Transaction.date, Transaction.id).all())
total = 0
for t, name in rows:
    total += float(t.amount)
    print(f"{t.id:5} {t.date} за {t.period} {float(t.amount):>10,.0f} {name}".replace(",", " "))
print(f"итого {len(rows)} проводок на {total:,.0f}".replace(",", " "))
if not apply:
    print("dry run — ничего не записано")
    sys.exit(0)
now = datetime.now()
for t, name in rows:
    t.deleted_at = now
    db.add(AuditLog(entity_type="transaction", entity_id=t.id, action="delete", user_id=1,
                    old_data={"amount": float(t.amount), "date": t.date.isoformat(), "employee": name},
                    new_data={"reason": "зарплата вводится заново (Махабат, 16.09)"}))
db.commit()
left = db.query(Transaction).filter(Transaction.employee_id.isnot(None), Transaction.deleted_at.is_(None)).count()
print(f"ЗАПИСАНО: убрано {len(rows)}, живых зарплатных проводок осталось {left}")
