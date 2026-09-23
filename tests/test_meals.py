"""Сколько сегодня едят (23.09): строка в чат → запись, проверка правдоподобия,
строка на «Сегодня», вопросы бота с образцом, ключевые продукты в пересчёте."""
from datetime import date, datetime, timedelta

import pytest

from app.models import BotMessage, MealCount, Organization, Product, Student, User
from app.services import bot, meals, rules, stock, today


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень ед", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-ед", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    school = Organization(name="Школа тест-ед", type="school", parent_id=root.id, site_id=sadik.id)
    db.add(school)
    db.flush()
    for i in range(20):
        db.add(Student(organization_id=school.id, name=f"Ученик ед {i}", status="active", pin=f"8{i:03d}"))
    for i in range(10):
        db.add(Student(organization_id=sadik.id, name=f"Ребёнок ед {i}", status="active", pin=f"7{i:03d}"))
    db.flush()
    return sadik


@pytest.fixture()
def counter(db, site, monkeypatch):
    u = User(name="Учётчик ед", role="staff", organization_id=site.id, tg_id=555001)
    db.add(u)
    db.flush()
    monkeypatch.setattr(bot, "site_for_bot", lambda db: site)
    monkeypatch.delenv(bot.TOKEN_ENV, raising=False)
    monkeypatch.setenv(bot.GROUP_ENV, "-100555")
    monkeypatch.setattr(rules, "kitchen_weekdays", lambda db: {0, 1, 2, 3, 4, 5, 6})
    return u


def test_parse_line_and_yesterday():
    p = meals.parse("школа 310, садик 48, персонал 12. Меню: борщ, плов", date(2026, 9, 23))
    assert (p["school"], p["sadik"], p["staff"], p["menu"], p["date"]) == (310, 48, 12, "борщ, плов", date(2026, 9, 23))
    assert meals.parse("вчера 300 школа 45 садик", date(2026, 9, 23))["date"] == date(2026, 9, 22)
    assert meals.parse("школа 18000 садик 10000") is None       # это деньги
    assert meals.parse("школа 310") is None                      # одно число — не про едоков


def test_chat_line_records_and_replies_with_doubt(db, site, counter):
    upd = {"message": {"message_id": 1, "chat": {"id": -100555, "type": "supergroup"}, "from": {"id": 555001},
                       "text": "школа 25, садик 8, персонал 5"}}
    reply = bot.handle_update(db, upd)
    row = db.query(MealCount).filter_by(site_org_id=site.id, date=date.today()).one()
    assert (row.school, row.sadik, row.staff, row.source) == (25, 8, 5, "chat")
    assert reply.startswith("Записал сегодня") and "по списку 20" in reply   # школа больше списка — вопрос
    # поправка той же строкой — та же запись, не вторая
    upd["message"]["text"] = "школа 18, садик 8, персонал 5"
    bot.handle_update(db, upd)
    assert db.query(MealCount).filter_by(site_org_id=site.id).count() == 1
    assert db.query(MealCount).filter_by(site_org_id=site.id).one().school == 18


def test_today_row_until_recorded_then_figure(db, site, counter):
    titles = [i["title"] for i in today.todo(db, site.id)]
    assert "Сегодня едят: не записано" in titles
    meals.record(db, site_org_id=site.id, d=date.today(), values={"school": 18, "sadik": 9, "staff": 5}, menu=None,
                 user=counter, source="form")
    assert "Сегодня едят: не записано" not in [i["title"] for i in today.todo(db, site.id)]
    assert today.now_figures(db, site.id)["meals"] == "школа 18, садик 9, персонал 5"


def test_doubt_uses_own_history_not_roster(db, site, counter):
    for back, n in ((3, 6), (2, 6), (1, 7)):
        meals.record(db, site_org_id=site.id, d=date.today() - timedelta(days=back), values={"sadik": n}, menu=None,
                     user=counter, source="chat")
    assert meals.doubts(db, site.id, {"sadik": 6}, date.today()) == []           # 6 из 10 — обычно для этого садика
    assert meals.doubts(db, site.id, {"sadik": 2}, date.today())                  # резко меньше обычного — вопрос


def test_asks_with_example_in_own_hour_only(db, site, counter):
    d = date.today()
    assert bot._meal_and_count_asks(db, site, datetime(d.year, d.month, d.day, 11, 30)) == []
    keys = bot._meal_and_count_asks(db, site, datetime(d.year, d.month, d.day, 12, 5))
    assert keys == [f"meal_ask:{d.isoformat()}:12"]
    msg = db.query(BotMessage).filter_by(job_key=keys[0]).one()
    assert "например" in msg.text and meals.EXAMPLE in msg.text and msg.text.startswith("Учётчик ед,")
    assert bot._meal_and_count_asks(db, site, datetime(d.year, d.month, d.day, 12, 40)) == []   # один раз


def test_key_products_open_count_first(db, site, counter, monkeypatch):
    p = Product(name="Говядина тест-ед", unit="кг")
    db.add(p)
    db.flush()
    monkeypatch.setattr(rules, "key_products", lambda db: [p.id])
    data = stock.count_rows(db, site.id, None)
    assert data["current"] == stock.KEY and [r["p"].id for r in data["rows"]] == [p.id]
