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
    # завтрак и обед — с подписями, целиком (владелец 23.09)
    p = meals.parse(meals.EXAMPLE, date(2026, 9, 23))
    assert p["menu"] == "Завтрак: каша, чай. Обед: борщ, плов, компот" and p["school"] == 310


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


def _thursday_after(d):
    return d + timedelta(days=(3 - d.weekday()) % 7)


def test_count_on_thursday_with_bank_line_and_friday_morning(db, site, counter, monkeypatch):
    from app.services import reconciliation
    p = Product(name="Говядина тест-чт", unit="кг")
    db.add(p)
    db.flush()
    monkeypatch.setattr(rules, "key_products", lambda db: [p.id])
    monkeypatch.setattr(rules, "count_weekday", lambda db: 3)
    mgr = User(name="Управляющая ед", role="manager", organization_id=site.id)
    db.add(mgr)
    db.flush()
    thu = _thursday_after(date.today() + timedelta(days=7))
    reconciliation.create(db, organization_id=site.id, kind="account", actual=1000, user_id=mgr.id,
                          on_date=thu - timedelta(days=10))
    monkeypatch.setattr(bot.cash, "bank_due", lambda db, s, start: [{"org": site}])
    assert bot._meal_and_count_asks(db, site, datetime(thu.year, thu.month, thu.day, 14, 5)) == []   # 14 — ещё кухня
    keys = bot._meal_and_count_asks(db, site, datetime(thu.year, thu.month, thu.day, 15, 5))
    text = db.query(BotMessage).filter_by(job_key=keys[-1]).one().text
    assert "пересчёт ключевых" in text and "Управляющая ед, и остаток в банке" in text
    fri = thu + timedelta(days=1)
    keys = bot._meal_and_count_asks(db, site, datetime(fri.year, fri.month, fri.day, 8, 5))
    assert keys == [f"count_ask:{fri.isoformat()}:8"]
    assert "до поваров" in db.query(BotMessage).filter_by(job_key=keys[0]).one().text


def test_stuck_goes_to_founder_only_after_bot_asked(db, site, counter, monkeypatch):
    founder = User(name="Учредитель ед", role="founder", organization_id=site.id, tg_id=555777)
    db.add(founder)
    db.flush()
    monkeypatch.setattr(bot.cash, "bank_due", lambda db, s, start: [])
    monkeypatch.setattr(rules, "escalate_from", lambda db: date.today() - timedelta(days=1))
    d = date.today()
    at16 = datetime(d.year, d.month, d.day, 16, 5)
    assert bot._escalate(db, site, at16) == []            # бот не спрашивал — жаловаться не на что
    for x in (d - timedelta(days=1), d):
        db.add(BotMessage(kind="meal_ask", job_key=f"meal_ask:{x.isoformat()}:12", direction="out", status="logged"))
    db.flush()
    assert bot._escalate(db, site, at16) == [f"escalate:{d.isoformat()}:16"]
    msg = db.query(BotMessage).filter_by(job_key=f"escalate:{d.isoformat()}:16:{founder.id}").one()
    assert msg.chat_id == 555777 and "сколько едят" in msg.text and msg.text.startswith("Учредитель ед,")
    assert bot._escalate(db, site, at16) == []            # раз в день


def test_unknown_start_is_not_asked_for_number(db, site, counter):
    upd = {"message": {"message_id": 2, "chat": {"id": 777, "type": "private"}, "from": {"id": 777, "first_name": "Новый"},
                       "text": "/start"}}
    reply = bot.handle_update(db, upd)
    assert "передавать ничего не нужно" in reply and "номер" not in reply


def test_makhabat_real_line_staff_summed():
    line = ("Здравствуйте, школа 328 детей персонал 33  Садик 97 детей  персонал 10. "
            "Меню: овсяная каша, суп с вермишелью, печенье, компот, пюре с мясным фаршем.")
    p = meals.parse(line, date(2026, 9, 23))
    assert (p["school"], p["sadik"], p["staff"]) == (328, 97, 43)


def test_unknown_in_group_linked_by_unique_first_name(db, site, counter):
    u = User(name="Гульнара", role="staff", organization_id=site.id)
    db.add(u)
    db.flush()
    upd = {"message": {"message_id": 3, "chat": {"id": -100555, "type": "supergroup"},
                       "from": {"id": 555999, "first_name": "Гульнара", "last_name": "Керимкуловна"},
                       "text": "школа 18, садик 8, персонал 5"}}
    bot.handle_update(db, upd)
    assert u.tg_id == 555999
    assert db.query(MealCount).filter_by(site_org_id=site.id, date=date.today()).one().created_by == u.id


def test_first_week_stuck_only_to_owner(db, site, counter, monkeypatch):
    founder = User(name="Учредитель пн", role="founder", organization_id=site.id, tg_id=555778)
    db.add(founder)
    db.flush()
    monkeypatch.setattr(bot.cash, "bank_due", lambda db, s, start: [])
    monkeypatch.setattr(rules, "escalate_from", lambda db: date.today() + timedelta(days=3))
    d = date.today()
    for x in (d - timedelta(days=1), d):
        db.add(BotMessage(kind="meal_ask", job_key=f"meal_ask:{x.isoformat()}:12", direction="out", status="logged"))
    db.flush()
    assert bot._escalate(db, site, datetime(d.year, d.month, d.day, 16, 5))
    assert db.query(BotMessage).filter_by(user_id=founder.id, kind="escalate").count() == 0
    owner_copy = db.query(BotMessage).filter_by(job_key=f"escalate:{d.isoformat()}:16").one()
    assert "Привыкание" in owner_copy.text and "что мешает" in owner_copy.text


def test_incomplete_line_asks_for_rest_then_accepts_one_part(db, site, counter):
    upd = {"message": {"message_id": 5, "chat": {"id": -100555, "type": "supergroup"}, "from": {"id": 555001},
                       "text": "школа 18, персонал 5. Обед: плов"}}
    reply = bot.handle_update(db, upd)
    assert "Не хватает: садик" in reply and "садик 48" in reply
    upd["message"]["text"] = "садик 8"
    reply = bot.handle_update(db, upd)
    row = db.query(MealCount).filter_by(site_org_id=site.id, date=date.today()).one()
    assert (row.school, row.sadik, row.staff) == (18, 8, 5) and reply.endswith("Спасибо!")
    # когда день полный — одиночное «садик 9» в чате больше не ловим
    upd["message"]["text"] = "садик 9"
    assert bot.handle_update(db, upd) is None or row.sadik == 8


def test_count_day_after_fresh_count_asks_only_bank(db, site, counter, monkeypatch):
    """24.09: точку ноль записали в среду — в четверг пересчёт не просим, остаётся банк."""
    from app.models import StockCount, StockCountLine
    p = Product(name="Говядина тест-ср", unit="кг")
    db.add(p)
    db.flush()
    monkeypatch.setattr(rules, "key_products", lambda db: [p.id])
    monkeypatch.setattr(rules, "count_weekday", lambda db: 3)
    thu = _thursday_after(date.today() + timedelta(days=7))
    sc = StockCount(organization_id=site.id, count_date=thu - timedelta(days=1), status="applied",
                    started_by=counter.id)
    db.add(sc)
    db.flush()
    db.add(StockCountLine(count_id=sc.id, product_id=p.id, actual_qty=5, mode="number"))
    db.flush()
    assert bot.key_count_done(db, site.id, thu)
    assert not bot.key_count_done(db, site.id, thu + timedelta(days=7))   # через неделю — уже нужен
    monkeypatch.setattr(bot.cash, "bank_due", lambda db, s, start: [{"org": site}])
    keys = bot._meal_and_count_asks(db, site, datetime(thu.year, thu.month, thu.day, 15, 5))
    text = db.query(BotMessage).filter_by(job_key=keys[-1]).one().text
    assert "пересчёт" not in text and "остаток в банке" in text and ", и остаток" not in text
