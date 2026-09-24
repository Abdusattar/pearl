"""Закупки за день в 17:00 и утренние сверки в личку на время привыкания (23.09)."""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models import BotMessage, Organization, Reconciliation, Transaction, User
from app.services import bot, cash, rules


@pytest.fixture()
def world(db, monkeypatch):
    root = Organization(name="Корень тест-ут", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-ут", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    m = User(name="Учётчик тест-ут", role="staff", organization_id=sadik.id, tg_id=557001)
    mu = User(name="Управляющая тест-ут", role="manager", organization_id=sadik.id, tg_id=557002)
    db.add_all([m, mu])
    db.flush()
    monkeypatch.setattr(bot, "site_for_bot", lambda db: sadik)
    monkeypatch.delenv(bot.TOKEN_ENV, raising=False)
    monkeypatch.setenv(bot.GROUP_ENV, "-100557")
    monkeypatch.setattr(rules, "kitchen_weekdays", lambda db: {0, 1, 2, 3, 4, 5, 6})
    monkeypatch.setattr(rules, "daily_checks_until", lambda db: date.today() + timedelta(days=30))
    from app.services import today as _today
    monkeypatch.setattr(_today, "supplier_debts", lambda db, s: [])   # долги — отдельным тестом
    from app.services import meals as _meals
    monkeypatch.setattr(_meals, "missing_today", lambda db, s: False)   # едоки записаны — отдельным тестом
    return {"sadik": sadik, "m": m, "mu": mu}


def _at(h, d=None):
    d = d or date.today()
    return datetime(d.year, d.month, d.day, h, 5)


def _monday():
    d = date.today()
    return d + timedelta(days=(7 - d.weekday()) % 7 or 7)


def test_purchases_ask_only_when_nothing_bought(db, world):
    keys = bot._purchases_ask(db, world["sadik"], _at(17))
    assert keys and "без закупок" in db.query(BotMessage).filter_by(job_key=keys[0]).one().text
    assert bot._purchases_ask(db, world["sadik"], _at(17)) == []            # раз в день


def test_purchases_ask_silent_after_no_buy_answer(db, world):
    bot.handle_update(db, {"message": {"message_id": 9, "chat": {"id": -100557, "type": "supergroup"},
                                       "from": {"id": 557001}, "text": "сегодня без закупок"}})
    assert bot._purchases_ask(db, world["sadik"], _at(17)) == []


def test_purchases_ask_silent_when_bought(db, world):
    db.add(Transaction(organization_id=world["sadik"].id, type="expense", amount=500, date=date.today()))
    db.flush()
    assert bot._purchases_ask(db, world["sadik"], _at(17)) == []


def test_morning_pocket_ask_and_yes_records_recount(db, world, monkeypatch):
    monkeypatch.setattr(cash, "pocket_people", lambda db, s: [world["m"]])
    monkeypatch.setattr(cash, "state", lambda db, s: {"accounts": []})
    assert bot._morning_checks(db, world["sadik"], _at(9)) == [] or date.today().weekday() == 0   # не понедельник — молчим (24.09)
    mon = _monday()
    keys = bot._morning_checks(db, world["sadik"], _at(9, mon))
    assert keys == [f"pocket_ask:{mon.isoformat()}:{world['m'].id}"]
    reply = bot.handle_update(db, {"message": {"message_id": 1, "chat": {"id": 557001, "type": "private"},
                                               "from": {"id": 557001}, "text": "да"}})
    assert "Записано" in reply
    assert db.query(Reconciliation).filter_by(kind="pocket", subject_id=world["m"].id).count() == 1


def test_bank_answer_records_yesterday(db, world, monkeypatch):
    monkeypatch.setattr(cash, "pocket_people", lambda db, s: [])
    monkeypatch.setattr(cash, "state", lambda db, s: {"accounts": [{"org": world["sadik"]}]})
    keys = bot._morning_checks(db, world["sadik"], _at(9))
    if date.today().weekday() >= 5:
        return
    assert keys and keys[0].startswith("bank_ask:")
    exp = cash.expected_account(db, world["sadik"].id, date.today() - timedelta(days=1))
    reply = bot.handle_update(db, {"message": {"message_id": 2, "chat": {"id": 557002, "type": "private"},
                                               "from": {"id": 557002}, "text": f"{int(exp)}"}})
    assert reply.startswith("Записал остаток")
    rec = db.query(Reconciliation).filter_by(kind="account", organization_id=world["sadik"].id).one()
    assert rec.date == date.today() - timedelta(days=1)


def test_morning_no_without_number_asks_figure_then_yes_with_reason(db, world, monkeypatch):
    """Махабат 24.09: «нет хлеб взяли в долг» — не дежурная фраза, а просьба одной цифрой;
    «да, хлеб в долг» — подтверждение с причиной."""
    monkeypatch.setattr(cash, "pocket_people", lambda db, s: [world["m"]])
    monkeypatch.setattr(cash, "state", lambda db, s: {"accounts": []})
    mon = _monday()
    bot._morning_checks(db, world["sadik"], _at(9, mon))
    def say(t, mid):
        return bot.handle_update(db, {"message": {"message_id": mid, "chat": {"id": 557001, "type": "private"},
                                                  "from": {"id": 557001}, "text": t}})
    assert "цифрой" in say("нет хлеб взяли в долг", 3)
    assert db.query(Reconciliation).filter_by(kind="pocket", subject_id=world["m"].id).count() == 0
    assert "Записано" in say("да, хлеб в долг", 4)
    rec = db.query(Reconciliation).filter_by(kind="pocket", subject_id=world["m"].id).one()
    assert rec.reason == "хлеб в долг"


def test_morning_no_with_number_records(db, world, monkeypatch):
    monkeypatch.setattr(cash, "pocket_people", lambda db, s: [world["m"]])
    monkeypatch.setattr(cash, "state", lambda db, s: {"accounts": []})
    mon = _monday()
    bot._morning_checks(db, world["sadik"], _at(9, mon))
    reply = bot.handle_update(db, {"message": {"message_id": 5, "chat": {"id": 557001, "type": "private"},
                                               "from": {"id": 557001}, "text": "нет, 0 хлеб в долг"}})
    assert "Записано" in reply


def test_debts_asked_on_monday_and_when_crossing_month(db, world, monkeypatch):
    """24.09: долги — Махабат по понедельникам в 17:00 и в день, когда долг стал старше месяца."""
    from app.services import today as _today
    d = date.today()
    monkeypatch.setattr(_today, "supplier_debts",
                        lambda db, s: [{"id": 77, "name": "Мясо тест-ут", "debt": Decimal("62595"), "since": d - timedelta(days=45)}])
    monkeypatch.setattr(bot, "_bought_today", lambda db, s, day: True)   # закупки были — про закуп не спрашиваем
    keys = bot._purchases_ask(db, world["sadik"], _at(17))
    text = db.query(BotMessage).filter_by(job_key=keys[0]).one().text
    assert "долги поставщикам: Мясо тест-ут 62 595" in text and "оплатила" in text
    # на следующий день (не понедельник) — уже не повторяем: порог отмечен
    nd = d + timedelta(days=1 if d.weekday() != 6 else 2)
    keys2 = bot._purchases_ask(db, world["sadik"], datetime(nd.year, nd.month, nd.day, 17, 5))
    if nd.weekday() != 0:
        assert keys2 == []
