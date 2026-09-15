"""Слияние склада и кассы Сокулука: Школа (org 2) → площадка Садик Сокулук (org 4).

    python scripts/merge_sokuluk.py --db <прод>                 # diff, ничего не пишет
    python scripts/merge_sokuluk.py --db <прод> --apply --user-id 1

Схема `context/revision/06_merge_sokuluk.md`, утверждена владельцем 15.09.
Запускает владелец сам; --user-id — его id, им подписываются
компенсирующие списания и отмена брошенного пересчёта.
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SOURCE = 2          # Школа
TARGET = 4          # Садик Сокулук — площадка
CUTOFF = date(2026, 9, 9)   # пересчёт склада, считавший общую кухню целиком
REASON = "слияние складов: остаток Школы учтён пересчётом 09.09"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="записать (по умолчанию только diff)")
    ap.add_argument("--db", help="строка подключения вместо DATABASE_URL")
    ap.add_argument("--user-id", type=int, help="кем подписать списания и отмену пересчёта (нужен для --apply)")
    args = ap.parse_args()
    if args.apply and not args.user_id:
        ap.error("--apply требует --user-id")

    if args.db:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        Session = sessionmaker(bind=create_engine(args.db))
    else:
        from app.database import SessionLocal as Session

    from app.services.transition import merge_site

    db = Session()
    try:
        lines = merge_site(db, SOURCE, TARGET, CUTOFF, REASON, args.user_id, dry_run=not args.apply)
        if args.apply:
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()
    for line in lines:
        print(line)
    print(f"\n{'ЗАПИСАНО' if args.apply else 'DRY RUN, не записано'}: строк {len(lines)}")


if __name__ == "__main__":
    main()
