from sqlalchemy import text
from sqlalchemy.orm import Session


def acquire_submission_lock(db: Session, scope: str, fingerprint: str) -> None:
    """Сериализует конкурентные/повторные сабмиты с одинаковым (scope, fingerprint)
    в пределах текущей транзакции — тот же приём pg_advisory_xact_lock, что уже
    закрывает гонки в generate_monthly_charges (billing.py) и menu_day_save
    (menu.py), 04.08/23.07. Лок снимается сам при COMMIT/ROLLBACK — вызывать
    первой строкой обработчика, до любого чтения по этому ключу, иначе гонка
    успеет проскочить между чтением и локом."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": f"{scope}:{fingerprint}"})
