"""Меню больше не списывает со склада (09.09).

Тех.карта собиралась ИИ-разбором документов, повара кладут по-своему —
расчётное списание не сходилось с реальным расходом и нарастило 46 минусов.
Тест держит выключатель: пока флаг снят, ни один заход на Склад/Расходы не
должен породить списание.
"""
from datetime import date

from app.models import WriteOff
from app.services import writeoff_calc


def test_menu_writeoff_is_disabled():
    assert writeoff_calc.MENU_WRITEOFF_ENABLED is False, (
        "Меню снова списывает со склада — если это осознанно, поправь и тест"
    )


def test_auto_apply_writes_nothing_while_disabled(db):
    before = db.query(WriteOff).count()
    # org_id/user_id намеренно любые: до флага дело не доходит вообще
    writeoff_calc.auto_apply_if_pending(db, 4, date(2026, 8, 20), 1)
    assert db.query(WriteOff).count() == before
