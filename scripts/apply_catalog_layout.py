"""Применение раскладки каталога (переход 15.09).

    python scripts/apply_catalog_layout.py                # diff, ничего не пишет
    python scripts/apply_catalog_layout.py --apply        # записать

База берётся из DATABASE_URL (локальная) или из --db <строка>. Раскладка —
`context/revision/catalog_layout.csv`, утверждена владельцем 15.09; решения по
переименованиям и единицам — в `05_catalog_layout.md`, здесь они константами.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
LAYOUT = ROOT / "context" / "revision" / "catalog_layout.csv"

# Решения владельца 15.09: оба говядина, разные части, не сливать.
RENAMES = {338: "Говядина филе", 248: "Мясо говядина"}
# Чай считать в граммах: карточка «Чай чёрный» из кг в г (история ×1000).
UNIT_CHANGES = {193: ("г", 1000)}
# «чай» (уп) сливается в «Чай чёрный» (г): пачка = 500 г по алиасу
# «Чемпион Кения 500» — уточнить у Махабат до записи.
MERGE_FACTORS = {325: 500}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать (по умолчанию только diff)")
    ap.add_argument("--db", help="строка подключения вместо DATABASE_URL")
    args = ap.parse_args()

    if args.db:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=create_engine(args.db))
    else:
        from app.database import SessionLocal as Session

    from app.services.transition import apply_layout, read_layout

    rows = read_layout(LAYOUT)
    db = Session()
    try:
        lines = apply_layout(db, rows, renames=RENAMES, unit_changes=UNIT_CHANGES,
                             merge_factors=MERGE_FACTORS, dry_run=not args.apply)
        if args.apply:
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()
    for line in lines:
        print(line)
    skipped = sum(1 for l in lines if l.startswith("!"))
    print(f"\n{'ЗАПИСАНО' if args.apply else 'DRY RUN, не записано'}: строк {len(lines)}, пропущено {skipped}")


if __name__ == "__main__":
    main()
