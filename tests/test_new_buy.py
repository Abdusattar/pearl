"""Экран «Купили» нового входа (16.09, план context/revision/07_buy_screen.md).

Что проверяем: список с прошлого привоза; единица из карточки и отказ чужой;
фасовка «2 лотка» → штуки; вопрос о цене; похожий товар вопросом, новый
товар с категорией; вопрос о повторе и «да, другая»; пять способов оплаты →
проводки, карман, счёт, взнос учредителя; ремонт без «для кого»; долг
поставщику; убрать покупку откатывает склад и долг.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import (CashFunding, Organization, Product, ProductCategory, Purchase,
                        Supplier, Transaction, User, WarehouseReceipt)
from app.routers import new_buy as new_router
from app.services import purchases as svc
from app.services.supplier_ledger import get_supplier_balance
from app.services.transition import ensure_categories


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-нб", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    school = Organization(name="Школа тест-нб", type="school", parent_id=root.id, site_id=sadik.id)
    db.add(school)
    db.flush()
    return sadik, school


@pytest.fixture()
def staff(db, site, monkeypatch):
    sadik, _ = site
    u = User(name="Учётчик тест-нб", role="staff", organization_id=sadik.id)
    db.add(u)
    db.flush()
    monkeypatch.setattr(new_router, "get_current_user", lambda request, db: u)
    # resolve_org берёт только доступные объекты; тестовый объект в пилот не входит
    monkeypatch.setattr(new_router, "resolve_org", lambda org_id, user, db: sadik)
    return u


@pytest.fixture()
def founder(db, site):
    u = User(name="Учредитель тест-нб", role="founder", organization_id=site[0].id)
    db.add(u)
    db.flush()
    return u


@pytest.fixture()
def cats(db):
    return ensure_categories(db)


@pytest.fixture()
def egg(db, cats):
    p = Product(name="Яйцо тест-нб", unit="шт", is_standard=True,
                category_id=cats["молочное и яйца"].id, pack_name="лоток", pack_qty=30)
    db.add(p)
    db.flush()
    return p


@pytest.fixture()
def carrot(db, cats):
    p = Product(name="Морковь тест-нб", unit="кг", is_standard=True, category_id=cats["овощи и фрукты"].id)
    db.add(p)
    db.flush()
    return p


@pytest.fixture()
def paint(db, cats):
    p = Product(name="Краска тест-нб", unit="шт", is_standard=True, category_id=cats["ремонт"].id)
    db.add(p)
    db.flush()
    return p


@pytest.fixture()
def halima(db):
    s = Supplier(name="Халима тест-нб", phone="0700000101")
    db.add(s)
    db.flush()
    return s


def _receipt(db, product, org, price, qty=10, days_ago=3, tx=None):
    r = WarehouseReceipt(date=date.today() - timedelta(days=days_ago), product_id=product.id, quantity=qty,
                         price_per_unit=price, total_cost=qty * price, organization_id=org.id,
                         transaction_id=tx.id if tx else None)
    db.add(r)
    db.flush()
    return r


def _post(client, supplier, rows, **kw):
    data = {"supplier_id": str(supplier.id), "date": date.today().isoformat(),
            "payment": kw.pop("payment", "cash"), "for_org": kw.pop("for_org", "shared"),
            "item_name": [], "item_product_id": [], "item_qty": [], "item_unit": [],
            "item_unit_price": [], "item_price_ok": [], "item_new": [], "item_category_id": []}
    for r in rows:
        data["item_name"].append(r.get("name", ""))
        data["item_product_id"].append(str(r.get("pid", "")))
        data["item_qty"].append(str(r.get("qty", "")))
        data["item_unit"].append(r.get("unit", ""))
        data["item_unit_price"].append(str(r.get("price", "")))
        data["item_price_ok"].append(r.get("ok", ""))
        data["item_new"].append(r.get("new", ""))
        data["item_category_id"].append(str(r.get("cat", "")))
    data.update({k: str(v) for k, v in kw.items()})
    return client.post("/new/buy", data=data, follow_redirects=False)


def _purchase(db, resp) -> Purchase:
    assert resp.status_code == 303, resp.text[:500]
    pid = int(resp.headers["location"].split("/new/buy/")[1].split("?")[0])
    return db.get(Purchase, pid)


# ── подстановка и единицы ────────────────────────────────────────────────

def test_prefill_from_last_purchase(client, db, site, staff, halima, carrot, egg):
    r = _purchase(db, _post(client, halima, [
        {"name": carrot.name, "pid": carrot.id, "qty": "48", "price": "30"},
        {"name": egg.name, "pid": egg.id, "qty": "120", "price": "12"},
    ]))
    last_date, rows = svc.prefill_from_last(db, halima.id)
    assert last_date == date.today()
    assert [(x["name"], x["qty"], x["unit_price"], x["unit"]) for x in rows] == [
        (carrot.name, "48", "30", "кг"), (egg.name, "120", "12", "шт")]
    page = client.get(f"/new/buy?supplier={halima.id}")
    assert "Список с прошлого привоза" in page.text and 'value="48"' in page.text


def test_unit_from_card_and_foreign_unit_rejected(client, db, site, staff, halima, carrot):
    before = db.query(Transaction).count()
    r = _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "5", "price": "40", "unit": "шт"}])
    assert r.status_code == 200 and "считается в кг" in r.text
    assert db.query(Transaction).count() == before
    assert db.get(Product, carrot.id).unit == "кг"


def test_pack_converts_to_card_unit(client, db, site, staff, halima, egg):
    p = _purchase(db, _post(client, halima, [{"name": egg.name, "pid": egg.id, "qty": "2", "price": "420", "unit": "лоток"}]))
    wr = db.query(WarehouseReceipt).filter_by(transaction_id=p.transactions[0].id).one()
    assert float(wr.quantity) == 60 and float(wr.price_per_unit) == 14 and float(wr.total_cost) == 840
    assert float(p.total) == 840


# ── вопросы ─────────────────────────────────────────────────────────────

def test_price_question_then_write(client, db, site, staff, halima, egg):
    for price in (12, 12, 13):
        _receipt(db, egg, site[0], price)
    before = db.query(Transaction).count()
    r = _post(client, halima, [{"name": egg.name, "pid": egg.id, "qty": "3", "price": "420"}])
    assert r.status_code == 200 and "обычно 12 за шт" in r.text and "Да, так" in r.text
    assert db.query(Transaction).count() == before
    p = _purchase(db, _post(client, halima, [{"name": egg.name, "pid": egg.id, "qty": "3", "price": "420", "ok": "1"}]))
    assert float(p.total) == 1260


def test_similar_product_is_a_question_not_a_new_card(client, db, site, staff, halima, cats):
    # имя, которого нет в реальном каталоге: на копии прода «морков» нашёл бы настоящую Морковь
    odd = Product(name="Зюзюблик тест-нб", unit="кг", is_standard=True, category_id=cats["овощи и фрукты"].id)
    db.add(odd)
    db.flush()
    before = db.query(Product).count()
    r = _post(client, halima, [{"name": "зюзюбл", "qty": "5", "price": "30"}])
    assert r.status_code == 200 and f"Это {odd.name}?" in r.text
    assert db.query(Product).count() == before
    p = _purchase(db, _post(client, halima, [{"name": odd.name, "pid": odd.id, "qty": "5", "price": "30"}]))
    assert db.query(Product).count() == before and float(p.total) == 150


def test_new_product_needs_category_and_unit(client, db, site, staff, halima, cats):
    r = _post(client, halima, [{"name": "Дроблёнка тест-нб", "qty": "2", "price": "60"}])
    assert r.status_code == 200 and "Новый товар" in r.text
    r = _post(client, halima, [{"name": "Дроблёнка тест-нб", "qty": "2", "price": "60", "new": "1"}])
    assert r.status_code == 200 and "категорию и единицу" in r.text
    p = _purchase(db, _post(client, halima, [{"name": "Дроблёнка тест-нб", "qty": "2", "price": "60", "new": "1",
                                                "unit": "кг", "cat": cats["крупы и макароны"].id}]))
    prod = db.query(Product).filter_by(name="Дроблёнка тест-нб").one()
    assert prod.unit == "кг" and prod.category_id == cats["крупы и макароны"].id and prod.is_standard
    assert prod.expense_category_id is not None
    assert db.query(WarehouseReceipt).filter_by(product_id=prod.id).count() == 1


def test_duplicate_question_then_confirmed(client, db, site, staff, halima, carrot):
    rows = [{"name": carrot.name, "pid": carrot.id, "qty": "48", "price": "30"}]
    _purchase(db, _post(client, halima, rows))
    before = db.query(Purchase).count()
    r = _post(client, halima, rows)
    assert r.status_code == 200 and "Это другая покупка?" in r.text
    assert db.query(Purchase).count() == before
    p = _purchase(db, _post(client, halima, rows, dup_ok="1"))
    assert p.dup_confirmed and db.query(Purchase).count() == before + 1


def test_repair_only_asks_for_org(client, db, site, staff, halima, paint):
    rows = [{"name": paint.name, "pid": paint.id, "qty": "2", "price": "500"}]
    r = _post(client, halima, rows)
    assert r.status_code == 200 and "Для кого" in r.text and "ремонт" in r.text
    p = _purchase(db, _post(client, halima, rows, for_org=str(site[1].id)))
    assert p.for_org_id == site[1].id
    assert p.transactions[0].organization_id == site[0].id  # деньги и склад — на площадке


# ── оплата ──────────────────────────────────────────────────────────────

def test_debt_goes_to_supplier_ledger(client, db, site, staff, halima, carrot):
    debt0 = get_supplier_balance(db, halima.id)
    p = _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}], payment="debt"))
    assert get_supplier_balance(db, halima.id) == debt0 + Decimal(300)
    tx = p.transactions[0]
    assert float(tx.amount_paid) == 0 and tx.paid_from_user_id is None and tx.paid_directly is False


def test_cash_from_pocket(client, db, site, staff, halima, carrot):
    other = User(name="Другой карман тест-нб", role="manager", organization_id=site[0].id)
    db.add(other)
    db.flush()
    p = _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}],
                            payment="cash", payer_id=other.id))
    tx = p.transactions[0]
    assert tx.amount_paid is None and tx.paid_from_user_id == other.id and p.paid_from_user_id == other.id


def test_account_requires_org(client, db, site, staff, halima, carrot):
    rows = [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}]
    r = _post(client, halima, rows, payment="account")
    assert r.status_code == 200 and "Со счёта садика или школы" in r.text
    p = _purchase(db, _post(client, halima, rows, payment="account", account_org_id=site[1].id))
    tx = p.transactions[0]
    assert tx.paid_directly is True and tx.account_org_id == site[1].id and tx.paid_from_user_id is None


def test_part_paid_rest_debt(client, db, site, staff, halima, carrot):
    debt0 = get_supplier_balance(db, halima.id)
    p = _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}],
                            payment="part", paid_amount="100"))
    assert get_supplier_balance(db, halima.id) == debt0 + Decimal(200)
    assert float(p.paid_amount) == 100 and p.transactions[0].paid_from_user_id == p.created_by


def test_founder_pays_creates_funding(client, db, site, staff, founder, halima, carrot):
    p = _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}],
                            payment="founder", founder_id=founder.id))
    f = db.get(CashFunding, p.funding_id)
    assert f.source_founder_id == founder.id and f.accountable_user_id == founder.id and float(f.amount) == 300
    assert p.transactions[0].paid_from_user_id == founder.id


# ── убрать ──────────────────────────────────────────────────────────────

def test_remove_rolls_back_stock_and_debt(client, db, site, staff, halima, carrot):
    debt0 = get_supplier_balance(db, halima.id)
    p = _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}], payment="debt"))
    tx_ids = [t.id for t in p.transactions]
    r = client.post(f"/new/buy/{p.id}/remove", follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(Purchase, p.id).deleted_at is not None
    assert all(db.get(Transaction, i).deleted_at is not None for i in tx_ids)
    assert db.query(WarehouseReceipt).filter(WarehouseReceipt.transaction_id.in_(tx_ids),
                                             WarehouseReceipt.deleted_at.is_(None)).count() == 0
    assert get_supplier_balance(db, halima.id) == debt0
    card = client.get(f"/new/buy/{p.id}")
    assert "Покупка убрана" in card.text
