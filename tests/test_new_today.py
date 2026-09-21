"""Экран «Сегодня» нового входа (16.09, макет блок 1)."""
from datetime import date, timedelta

import pytest

from app.models import KitchenSheet, Organization, Product, Supplier, Transaction, User, WarehouseReceipt
from app.routers import new_buy as buy_router
from app.routers import new_today as today_router
from app.services import today as svc
from app.services.transition import ensure_categories


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень сг", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-сг", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    return sadik


@pytest.fixture()
def staff(db, site, monkeypatch):
    u = User(name="Учётчик тест-сг", role="staff", organization_id=site.id)
    db.add(u)
    db.flush()
    monkeypatch.setattr(today_router, "get_current_user", lambda request, db: u)
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: site)
    return u


def _last_working_days(n):
    days, d = [], date.today() - timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days


def test_missing_sheets_are_signals(client, db, site, staff):
    for d in _last_working_days(14):
        pass
    page = client.get("/new/today")
    assert page.status_code == 200
    assert "Лист кухни" in page.text and "не внесён" in page.text and "Внести" in page.text
    items = svc.todo(db, site.id)
    assert any("Лист кухни" in it["title"] for it in items)
    assert all(it["url"].startswith("/new/kitchen?date=") for it in items if "Лист кухни" in it["title"])


def test_all_done_when_sheets_entered_and_no_old_debt(client, db, site, staff):
    for d in list(svc.kitchen.missing_days(db, site.id)):
        db.add(KitchenSheet(site_org_id=site.id, date=d, created_by=staff.id))
    db.flush()
    items = [it for it in svc.todo(db, site.id) if "Лист кухни" in it["title"]]
    assert items == []


def test_old_debt_is_a_signal(db, site, staff):
    s = Supplier(name="Халима тест-сг", phone="0700000202")
    db.add(s)
    db.flush()
    db.add(Transaction(organization_id=site.id, type="expense", amount=58300, amount_paid=0,
                       supplier_id=s.id, date=date.today() - timedelta(days=40)))
    db.flush()
    items = svc.todo(db, site.id)
    hit = [it for it in items if it["title"].startswith("Халима тест-сг")]
    assert hit and "58 300" in hit[0]["sub"] and hit[0]["url"] == f"/new/pay?supplier={s.id}"


def test_figures_count_only_stock_level_products(db, site, staff):
    cats = ensure_categories(db)
    main = Product(name="Гречка тест-сг", unit="кг", is_standard=True, category_id=cats["крупы и макароны"].id)
    minor = Product(name="Хлеб тест-сг", unit="шт", is_standard=True, category_id=cats["хлеб и выпечка"].id)
    db.add_all([main, minor])
    db.flush()
    db.add(WarehouseReceipt(date=date.today(), product_id=main.id, quantity=10, price_per_unit=80, total_cost=800, organization_id=site.id))
    db.add(WarehouseReceipt(date=date.today(), product_id=minor.id, quantity=10, price_per_unit=25, total_cost=250, organization_id=site.id))
    db.flush()
    f = svc.now_figures(db, site.id)
    assert f["stock"] == 800   # долги поставщикам общие для базы, здесь не проверяем
