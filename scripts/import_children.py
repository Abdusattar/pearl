"""
Импорт детей садика из data/ИНН.xlsx.
Назначает 3-значные PIN: 001, 002, ... (отсортировано по имени).
Запуск: python scripts/import_children.py
Идемпотентен — повторный запуск пропустит уже существующих (по ИНН).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import openpyxl
from app.database import SessionLocal
from app.models import Student

EXCEL_PATH = Path("data/ИНН.xlsx")
SHEET_NAME = "Детсад Сокулук"
ORG_ID = 4  # Садик Сокулук


def main():
    wb = openpyxl.load_workbook(EXCEL_PATH, read_only=True)
    ws = wb[SHEET_NAME]

    children = []
    for row in ws.iter_rows(values_only=True):
        name   = row[0]
        inn    = str(row[1]) if row[1] else ""
        amount = row[2]
        status = row[3]  # None = активный, "выбыл" = выбыл

        if not name or len(inn) != 14:
            continue
        if status == "выбыл":
            continue
        children.append({"name": name, "inn": inn, "monthly_fee": amount})

    wb.close()

    # Стабильный порядок → PIN всегда предсказуем
    children.sort(key=lambda r: r["name"])

    # Назначаем PIN до любых проверок — порядок не зависит от того, что уже в БД
    for i, child in enumerate(children, 1):
        child["pin"] = f"{i:03d}"

    db = SessionLocal()
    try:
        existing_inns = {
            s.extra["inn"]
            for s in db.query(Student).all()
            if s.extra and "inn" in s.extra
        }

        created = 0
        skipped = 0

        for child in children:
            if child["inn"] in existing_inns:
                print(f"  = {child['pin']} пропуск (уже есть): {child['name']}")
                skipped += 1
                continue

            student = Student(
                organization_id=ORG_ID,
                name=child["name"],
                pin=child["pin"],
                status="active",
                extra={"inn": child["inn"], "monthly_fee": child["monthly_fee"]},
            )
            db.add(student)
            print(f"  + {child['pin']}: {child['name']}")
            created += 1

        db.commit()
        print(f"\nГотово: создано {created}, пропущено {skipped} из {len(children)} активных.")
    except Exception as e:
        db.rollback()
        print(f"Ошибка: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
