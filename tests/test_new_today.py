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


def test_missing_sheets_are_one_row_and_button_opens_first_day(client, db, site, staff):
    page = client.get("/new/today")
    assert page.status_code == 200
    items = [it for it in svc.todo(db, site.id) if it["src"] == "kitchen"]
    first = svc.kitchen.missing_days(db, site.id)[0]
    assert len(items) == 1 and items[0]["title"].startswith("Листы кухни не внесены")
    assert items[0]["url"] == f"/new/kitchen?date={first.isoformat()}"
    assert f'href="/new/kitchen?date={first.isoformat()}"' in page.text and "не внесён" in page.text


def test_all_done_when_sheets_entered_and_no_old_debt(client, db, site, staff):
    for d in list(svc.kitchen.missing_days(db, site.id)):
        db.add(KitchenSheet(site_org_id=site.id, date=d, created_by=staff.id))
    db.flush()
    assert [it for it in svc.todo(db, site.id) if it["src"] == "kitchen"] == []
    assert svc.kitchen_action(db, site.id)["sub"] == "за сегодня"


def test_old_debt_is_a_signal(db, site, staff):
    s = Supplier(name="Халима тест-сг", phone="0700000202")
    db.add(s)
    db.flush()
    db.add(Transaction(organization_id=site.id, type="expense", amount=58300, amount_paid=0,
                       supplier_id=s.id, date=date.today() - timedelta(days=40)))
    db.flush()
    items = svc.todo(db, site.id)
    hit = [it for it in items if it["title"].startswith("Халима тест-сг")]
    assert hit and "58 300" in hit[0]["title"] and hit[0]["url"] == f"/new/pay?supplier={s.id}"


def test_photo_receipts_one_row_and_list(client, db, site, staff):
    from app.models import Receipt
    for i in range(3):
        db.add(Receipt(organization_id=site.id, file_path=f"receipts/test-sg-{i}.jpg", ocr_status="pending", created_by=staff.id))
    db.flush()
    rows = [it for it in svc.todo(db, site.id) if it["src"] == "receipts"]
    assert len(rows) == 1 and rows[0]["title"] == "3 чека с фото не внесены" and rows[0]["url"] == "/new/receipts"
    page = client.get("/new/receipts")
    assert page.status_code == 200 and page.text.count("/new/buy?receipt=") == 3


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
