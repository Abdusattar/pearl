"""Лист кухни нового входа (16.09, план context/revision/08_kitchen_sheet.md)."""
from datetime import date, timedelta

import pytest

from app.models import KitchenSheet, Organization, Product, User, WarehouseReceipt, WriteOff
from app.routers import new_buy as buy_router
from app.routers import new_kitchen as kitchen_router
from app.services import kitchen as svc
from app.services.transition import ensure_categories


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень лк", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-лк", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    return sadik


@pytest.fixture()
def staff(db, site, monkeypatch):
    u = User(name="Учётчик тест-лк", role="staff", organization_id=site.id)
    db.add(u)
    db.flush()
    monkeypatch.setattr(kitchen_router, "get_current_user", lambda request, db: u)
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: site)
    return u


@pytest.fixture()
def cats(db):
    return ensure_categories(db)


@pytest.fixture()
def butter(db, cats, site):
    p = Product(name="Масло тест-лк", unit="кг", is_standard=True, category_id=cats["бакалея и масла"].id)
    db.add(p)
    db.flush()
    db.add(WarehouseReceipt(date=date.today() - timedelta(days=5), product_id=p.id, quantity=3,
                            price_per_unit=500, total_cost=1500, organization_id=site.id))
    db.flush()
    return p


@pytest.fixture()
def dill(db, cats):
    p = Product(name="Укроп тест-лк", unit="пучок", is_standard=True, category_id=cats["зелень"].id)
    db.add(p)
    db.flush()
    return p


def _post(client, d, rows, children="", **kw):
    data = {"date": d.isoformat(), "children": children,
            "item_product_id": [], "item_name": [], "item_qty": [], "item_unit": []}
    for r in rows:
        data["item_product_id"].append(str(r.get("pid", "")))
        data["item_name"].append(r.get("name", ""))
        data["item_qty"].append(r.get("qty", ""))
        data["item_unit"].append(r.get("unit", ""))
    data.update(kw)
    return client.post("/new/kitchen", data=data, follow_redirects=False)


def _lines(db, sheet):
    return db.query(WriteOff).filter(WriteOff.sheet_id == sheet.id, WriteOff.deleted_at.is_(None)).all()


# ── разбор чисел и единиц ────────────────────────────────────────────────

def test_parse_qty_sum_and_comma():
    assert svc.parse_qty("3,2") == 3.2
    assert svc.parse_qty("0,5 + 3,5") == 4.0
    with pytest.raises(ValueError):
        svc.parse_qty("500г")
    assert svc.parse_qty("") is None


def test_grams_convert_to_card_unit(db, butter):
    assert svc.to_card_unit(butter, 200, "г") == 0.2
    assert svc.to_card_unit(butter, 1.5, "кг") == 1.5
    with pytest.raises(ValueError):
        svc.to_card_unit(butter, 1, "шт")


# ── запись листа ─────────────────────────────────────────────────────────

def test_sheet_writes_lines_in_card_unit(client, db, site, staff, butter):
    d = date.today()
    r = _post(client, d, [{"pid": butter.id, "name": butter.name, "qty": "200", "unit": "г"}], children="118")
    assert r.status_code == 303, r.text[:300]
    sheet = svc.sheet_for(db, site.id, d)
    lines = _lines(db, sheet)
    assert len(lines) == 1 and float(lines[0].quantity) == 0.2
    assert lines[0].reason == "лист кухни" and lines[0].children_count == 118 and lines[0].meal_type is None
    assert sheet.children_count == 118 and not sheet.shortfalls


def test_minor_written_without_stock_check(client, db, site, staff, dill):
    d = date.today()
    r = _post(client, d, [{"pid": dill.id, "name": dill.name, "qty": "2"}])
    assert r.status_code == 303
    sheet = svc.sheet_for(db, site.id, d)
    lines = _lines(db, sheet)
    assert len(lines) == 1 and float(lines[0].quantity) == 2 and not sheet.shortfalls


def test_over_stock_writes_available_and_records_shortfall(client, db, site, staff, butter):
    d = date.today()
    r = _post(client, d, [{"pid": butter.id, "name": butter.name, "qty": "5"}])
    assert r.status_code == 303
    sheet = svc.sheet_for(db, site.id, d)
    lines = _lines(db, sheet)
    assert len(lines) == 1 and float(lines[0].quantity) == 3
    assert sheet.shortfalls == [{"product_id": butter.id, "name": butter.name, "unit": "кг", "taken": 5, "had": 3}]
    assert svc.stock_map(db, site.id)[butter.id] == 0
    page = client.get(f"/new/kitchen?saved={d.isoformat()}&date={d.isoformat()}")
    assert "Расхождения" in page.text and "взяли 5 кг" in page.text


def test_same_product_twice_counted_together(client, db, site, staff, butter):
    d = date.today()
    r = _post(client, d, [{"pid": butter.id, "name": butter.name, "qty": "2"},
                          {"pid": butter.id, "name": butter.name, "qty": "2"}])
    assert r.status_code == 303
    sheet = svc.sheet_for(db, site.id, d)
    assert sum(float(w.quantity) for w in _lines(db, sheet)) == 3
    assert sheet.shortfalls[0]["taken"] == 4 and sheet.shortfalls[0]["had"] == 3


def test_resave_replaces_lines(client, db, site, staff, butter, dill):
    d = date.today()
    _post(client, d, [{"pid": butter.id, "name": butter.name, "qty": "1"}])
    sheet = svc.sheet_for(db, site.id, d)
    first = _lines(db, sheet)[0]
    r = _post(client, d, [{"pid": butter.id, "name": butter.name, "qty": "2"}, {"pid": dill.id, "name": dill.name, "qty": "1"}])
    assert r.status_code == 303
    db.expire_all()
    assert db.get(WriteOff, first.id).deleted_at is not None
    lines = _lines(db, sheet)
    assert sorted(float(w.quantity) for w in lines) == [1, 2]
    assert db.query(KitchenSheet).filter_by(site_org_id=site.id, date=d, deleted_at=None).count() == 1
    assert svc.stock_map(db, site.id)[butter.id] == 1


def test_row_errors_do_not_write(client, db, site, staff, butter):
    d = date.today()
    r = _post(client, d, [{"pid": "", "name": "нечто", "qty": "1"}])
    assert r.status_code == 200 and "Выберите продукт из списка" in r.text
    r = _post(client, d, [{"pid": butter.id, "name": butter.name, "qty": "500г"}])
    assert r.status_code == 200 and "сумма чисел" in r.text
    assert svc.sheet_for(db, site.id, d) is None


# ── дни ──────────────────────────────────────────────────────────────────

def test_missing_days_are_working_days_after_last_sheet(db, site, staff):
    monday = date.today() - timedelta(days=date.today().weekday()) - timedelta(days=7)
    db.add(KitchenSheet(site_org_id=site.id, date=monday, created_by=staff.id))
    db.flush()
    days = svc.missing_days(db, site.id, until=monday + timedelta(days=6))  # до воскресенья
    assert days == [monday + timedelta(days=i) for i in range(1, 5)]  # вт–пт, без сб/вс
    assert svc.next_missing_day(db, site.id, monday) == monday + timedelta(days=1)


def test_after_save_redirect_to_next_missing_day(client, db, site, staff, butter):
    today = date.today()
    # берём последний рабочий день, у которого следующий рабочий день <= сегодня
    d = today - timedelta(days=7)
    while d.weekday() not in svc.WORKING_WEEKDAYS:
        d -= timedelta(days=1)
    r = _post(client, d, [{"pid": butter.id, "name": butter.name, "qty": "1"}])
    assert r.status_code == 303
    loc = r.headers["location"]
    nxt = svc.next_missing_day(db, site.id, d)
    assert f"date={nxt.isoformat()}" in loc and f"saved={d.isoformat()}" in loc


def test_like_last_prefills(client, db, site, staff, butter):
    d = date.today() - timedelta(days=1)
    _post(client, d, [{"pid": butter.id, "name": butter.name, "qty": "0,7"}])
    page = client.get(f"/new/kitchen?date={date.today().isoformat()}&like=1")
    assert page.status_code == 200
    assert "Строки с листа за" in page.text and 'value="0,7"' in page.text and butter.name in page.text
