"""Склад нового входа (блок 4, 21.09)."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import (AuditLog, KitchenSheet, Organization, Product, ProductAlias, StockCount, User,
                        WarehouseReceipt, WriteOff)
from app.routers import new_buy as buy_router
from app.routers import new_stock as stock_router
from app.services import kitchen, stock as svc
from app.services.transition import ensure_categories


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень ск", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-ск", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    return sadik


@pytest.fixture()
def staff(db, site, monkeypatch):
    u = User(name="Учётчик тест-ск", role="staff", organization_id=site.id)
    db.add(u)
    db.flush()
    monkeypatch.setattr(stock_router, "get_current_user", lambda request, db: u)
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: site)
    return u


@pytest.fixture()
def cats(db):
    return ensure_categories(db)


def _product(db, site, name, cat, unit="кг", qty=10, price=50, days_ago=3):
    p = Product(name=name, unit=unit, is_standard=True, category_id=cat.id if cat else None)
    db.add(p)
    db.flush()
    if qty:
        db.add(WarehouseReceipt(date=date.today() - timedelta(days=days_ago), product_id=p.id, quantity=qty,
                                price_per_unit=price, total_cost=qty * price, organization_id=site.id,
                                supplier_name="Рынок тест-ск"))
        db.flush()
    return p


def _cover_days(db, site, staff):
    """Все рабочие дни окна внесены — чтобы пробел «листы не внесены» не мешал проверке."""
    for d in kitchen.missing_days(db, site.id):
        db.add(KitchenSheet(site_org_id=site.id, date=d, created_by=staff.id))
    db.flush()


def test_main_shows_counted_only_and_minor_hidden(client, db, site, staff, cats):
    _cover_days(db, site, staff)
    potato = _product(db, site, "Картофель тест-ск", cats["овощи и фрукты"], qty=100)
    _product(db, site, "Укроп тест-ск", cats["зелень"], unit="пучок", qty=20)
    st = svc.state(db, site.id)
    names = [r["p"].name for g in st["groups"] for r in g["rows"]]
    assert "Картофель тест-ск" in names and "Укроп тест-ск" not in names
    assert st["ok"] and st["gaps"] == []
    page = client.get("/new/stock")
    main = page.text[page.text.index("<main"):]
    assert page.status_code == 200 and "Картофель тест-ск" in main and "сходится" in main
    assert f'href="/new/stock/{potato.id}"' in main and "/warehouse" not in main


def test_missing_kitchen_days_is_a_gap_but_old_writeoff_day_is_not(db, site, staff, cats):
    p = _product(db, site, "Лук тест-ск", cats["овощи и фрукты"], qty=50)
    days = kitchen.missing_days(db, site.id)
    assert days
    db.add(WriteOff(date=days[0], product_id=p.id, quantity=1, organization_id=site.id, reason="питание детей"))
    db.flush()
    assert days[0] not in kitchen.missing_days(db, site.id)          # списано старым входом — внесено
    st = svc.state(db, site.id)
    assert not st["ok"] and st["gaps"][0]["title"].startswith("Листы кухни не внесены")
    assert st["gaps"][0]["url"].startswith("/new/kitchen?date=")


def test_count_writeoff_does_not_cover_kitchen_day(db, site, staff, cats):
    p = _product(db, site, "Морковь тест-ск", cats["овощи и фрукты"], qty=50)
    d = kitchen.missing_days(db, site.id)[0]
    db.add(WriteOff(date=d, product_id=p.id, quantity=1, organization_id=site.id, reason="пересчёт склада"))
    db.flush()
    assert d in kitchen.missing_days(db, site.id)


def test_no_category_and_minus_are_gaps(db, site, staff, cats):
    _cover_days(db, site, staff)
    _product(db, site, "Бинт тест-ск", None, unit="шт", qty=4)
    minus = _product(db, site, "Гречка тест-ск", cats["крупы и макароны"], qty=2)
    db.add(WriteOff(date=date.today(), product_id=minus.id, quantity=5, organization_id=site.id, reason="лист кухни"))
    db.flush()
    titles = [g["title"] for g in svc.state(db, site.id)["gaps"]]
    assert any(t.startswith("Гречка тест-ск: по записям минус 3") for t in titles)
    assert any("без категории" in t and "бинт тест-ск" in t for t in titles)


def test_product_card_moves_and_edit(client, db, site, staff, cats):
    p = _product(db, site, "Яйцо тест-ск", cats["молочное и яйца"], unit="шт", qty=360, price=14)
    db.add(WriteOff(date=date.today(), product_id=p.id, quantity=30, organization_id=site.id, reason="лист кухни"))
    db.flush()
    card = svc.product_card(db, site.id, p)
    assert card["balance"] == 330 and card["locked"]
    assert [m["title"] for m in card["moves"]] == ["Лист кухни", "Привоз, Рынок тест-ск"]
    r = client.post(f"/new/stock/{p.id}/edit", data={"category_id": cats["молочное и яйца"].id, "unit": "кг",
                                                    "pack_name": "лоток", "pack_qty": "30", "aliases": "яйца куриные"},
                    follow_redirects=False)
    assert "err=" in r.headers["location"]                              # единица закрыта
    r = client.post(f"/new/stock/{p.id}/edit", data={"category_id": cats["молочное и яйца"].id, "unit": "шт",
                                                    "pack_name": "лоток", "pack_qty": "30", "aliases": "яйца куриные"},
                    follow_redirects=False)
    assert "saved=1" in r.headers["location"]
    db.refresh(p)
    assert p.pack_name == "лоток" and p.pack_qty == 30 and svc.pack_text(p) == "лоток = 30 шт"
    assert db.query(ProductAlias).filter_by(product_id=p.id, raw_text="яйца куриные").count() == 1
    assert db.query(AuditLog).filter_by(entity_type="product", entity_id=p.id).count() == 1
    page = client.get(f"/new/stock/{p.id}")
    assert page.status_code == 200 and "330 шт" in page.text and "лоток = 30 шт" in page.text


def test_quick_count_changes_only_filled_rows(client, db, site, staff, cats):
    a = _product(db, site, "Капуста тест-ск", cats["овощи и фрукты"], qty=40)
    b = _product(db, site, "Тыква тест-ск", cats["овощи и фрукты"], qty=15)
    page = client.get(f"/new/stock/count?cat={cats['овощи и фрукты'].id}")
    assert page.status_code == 200 and "Капуста тест-ск" in page.text
    r = client.post("/new/stock/count", data={"cat": cats["овощи и фрукты"].id, "product_id": [a.id, b.id],
                                              "actual": ["32,5", ""], "form_token": "tok-stock-00000000000001"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/new/stock?saved=count"
    bal = svc.get_balance_map(db, {site.id})
    assert float(bal[a.id]["balance"]) == 32.5 and float(bal[b.id]["balance"]) == 15
    w = db.query(WriteOff).filter_by(product_id=a.id, reason="пересчёт склада").one()
    assert float(w.quantity) == 7.5
    cnt = db.query(StockCount).filter_by(organization_id=site.id, status="applied").one()
    assert len(cnt.lines) == 1 if hasattr(cnt, "lines") else True
    # тот же токен — второй раз не пишет
    client.post("/new/stock/count", data={"cat": cats["овощи и фрукты"].id, "product_id": [a.id, b.id],
                                          "actual": ["32,5", ""], "form_token": "tok-stock-00000000000001"},
                follow_redirects=False)
    assert db.query(StockCount).filter_by(organization_id=site.id, status="applied").count() == 1
    card = svc.product_card(db, site.id, a)
    assert card["moves"][0]["title"] == "Пересчёт, на полке меньше"


def test_count_with_nothing_filled_is_an_error(client, db, site, staff, cats):
    a = _product(db, site, "Свёкла тест-ск", cats["овощи и фрукты"], qty=10)
    r = client.post("/new/stock/count", data={"cat": cats["овощи и фрукты"].id, "product_id": [a.id], "actual": [""]})
    assert r.status_code == 200 and "Не посчитано ни одной строки" in r.text
    r = client.post("/new/stock/count", data={"cat": cats["овощи и фрукты"].id, "product_id": [a.id], "actual": ["abc"]})
    assert r.status_code == 200 and "Не число" in r.text
    assert db.query(StockCount).filter_by(organization_id=site.id).count() == 0


def test_days_text_joins_runs():
    mon = date(2026, 9, 14)
    days = [mon, mon + timedelta(days=1), mon + timedelta(days=2), mon + timedelta(days=4), mon + timedelta(days=7)]
    assert svc._days_text(days) == "14–16 сентября, 18–21 сентября"


def test_days_before_last_count_are_closed(db, site, staff, cats):
    days = kitchen.missing_days(db, site.id)
    db.add(StockCount(organization_id=site.id, count_date=days[1], status="applied", started_by=staff.id))
    db.flush()
    left = kitchen.missing_days(db, site.id)
    assert days[0] not in left and days[1] not in left and all(d > days[1] for d in left)


def test_executor_sees_no_period_totals_and_no_overview(client, db, site, staff, monkeypatch):
    from app.routers import new_expenses, new_overview, new_today
    for m in (new_expenses, new_overview, new_today):
        monkeypatch.setattr(m, "get_current_user", lambda request, db: staff)
    r = client.get("/new/overview", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/new/today"
    page = client.get("/new/today").text
    assert 'href="/new/overview"' not in page and "Продуктов на складе" not in page and "Должны поставщикам" not in page
    assert "Склад пока открывается в старом входе" not in page
    exp = client.get("/new/expenses").text
    assert '<p class="sub">За ' not in exp and "Должны поставщикам <b>" not in exp


def test_founder_sees_overview_and_totals(client, db, site, monkeypatch):
    from app.routers import new_overview, new_today
    boss = User(name="Учредитель тест-ск", role="founder", organization_id=site.id)
    db.add(boss)
    db.flush()
    for m in (new_overview, new_today):
        monkeypatch.setattr(m, "get_current_user", lambda request, db: boss)
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: site)
    assert client.get("/new/overview").status_code == 200
    page = client.get("/new/today").text
    assert 'href="/new/overview"' in page and "Продуктов на складе" in page


@pytest.fixture(autouse=True)
def _sheet_required_mode(monkeypatch):
    """Эти тесты — про режим «лист кухни обязателен» (до 23.09 он был единственным)."""
    from app.services import rules as _rules
    monkeypatch.setattr(_rules, "kitchen_sheet_required", lambda db: True)
