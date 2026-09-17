"""Лента расходов и оплата поставщику нового входа (16.09, макет 2а, 2в/2д)."""
from datetime import date
from decimal import Decimal

import pytest

from app.models import Organization, Product, Supplier, SupplierPayment, User
from app.routers import new_buy as buy_router
from app.routers import new_expenses as exp_router
from app.services import ledger as svc
from app.services import podotchet
from app.services.supplier_ledger import get_supplier_balance
from app.services.transition import ensure_categories
from tests.test_new_buy import _post, _purchase  # та же форма «Купили»


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень лр", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-лр", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    return sadik


@pytest.fixture()
def staff(db, site, monkeypatch):
    u = User(name="Учётчик тест-лр", role="staff", organization_id=site.id)
    db.add(u)
    db.flush()
    monkeypatch.setattr(exp_router, "get_current_user", lambda request, db: u)
    monkeypatch.setattr(buy_router, "get_current_user", lambda request, db: u)
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: site)
    return u


@pytest.fixture()
def halima(db):
    s = Supplier(name="Халима тест-лр", phone="0700000303")
    db.add(s)
    db.flush()
    return s


@pytest.fixture()
def carrot(db):
    cats = ensure_categories(db)
    p = Product(name="Морковь тест-лр", unit="кг", is_standard=True, category_id=cats["овощи и фрукты"].id)
    db.add(p)
    db.flush()
    return p


def test_feed_shows_purchase_as_one_row_with_debt(client, db, site, staff, halima, carrot):
    p = _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}],
                            payment="debt"))
    first, last, key = svc.month_bounds(date.today().strftime("%Y-%m"))
    days, totals = svc.month_rows(db, site.id, first, last)
    rows = [r for d in days for r in d["rows"]]
    mine = [r for r in rows if r["url"] == f"/new/buy/{p.id}"]
    assert len(mine) == 1
    assert mine[0]["title"] == halima.name and mine[0]["status"] == "в долг" and mine[0]["amount"] == Decimal("300")
    assert "1 позиция" in mine[0]["sub"] and carrot.name in mine[0]["sub"]
    page = client.get("/new/expenses")
    assert page.status_code == 200 and halima.name in page.text


def test_pay_reduces_debt_and_cash(client, db, site, staff, halima, carrot):
    _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}], payment="debt"))
    debt0 = get_supplier_balance(db, halima.id)
    cash0 = podotchet.get_cash_state(db, site.id)["net"]
    r = client.post("/new/pay", data={"supplier_id": halima.id, "amount": "200", "source": "cash",
                                      "payer_id": str(staff.id), "pay_date": date.today().isoformat()},
                    follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    assert get_supplier_balance(db, halima.id) == debt0 - Decimal(200)
    assert podotchet.get_cash_state(db, site.id)["net"] == cash0 - Decimal(200)
    p = db.query(SupplierPayment).filter_by(supplier_id=halima.id).one()
    assert p.organization_id == site.id and p.paid_from_user_id == staff.id and p.paid_directly is False


def test_pay_from_account_does_not_touch_cash(client, db, site, staff, halima, carrot):
    _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}], payment="debt"))
    cash0 = podotchet.get_cash_state(db, site.id)["net"]
    r = client.post("/new/pay", data={"supplier_id": halima.id, "amount": "300", "source": "account",
                                      "account_org_id": str(site.id), "pay_date": date.today().isoformat()},
                    follow_redirects=False)
    assert r.status_code == 303
    assert podotchet.get_cash_state(db, site.id)["net"] == cash0
    assert get_supplier_balance(db, halima.id) == 0
    acc = podotchet.get_expected_balance(db, site.id, date.today())
    assert acc["direct"] >= Decimal(300)


def test_pay_more_than_debt_refused(client, db, site, staff, halima, carrot):
    _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}], payment="debt"))
    r = client.post("/new/pay", data={"supplier_id": halima.id, "amount": "5000", "source": "cash",
                                      "payer_id": str(staff.id), "pay_date": date.today().isoformat()},
                    follow_redirects=False)
    assert r.status_code == 200 and "больше заплатить нельзя" in r.text
    assert db.query(SupplierPayment).filter_by(supplier_id=halima.id).count() == 0


def test_payment_row_in_feed_is_negative_and_not_in_total(client, db, site, staff, halima, carrot):
    _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}], payment="debt"))
    client.post("/new/pay", data={"supplier_id": halima.id, "amount": "100", "source": "cash",
                                  "payer_id": str(staff.id), "pay_date": date.today().isoformat()})
    first, last, _ = svc.month_bounds(None)
    days, totals = svc.month_rows(db, site.id, first, last)
    rows = [r for d in days for r in d["rows"]]
    pay = [r for r in rows if r["payment"] and halima.name in r["title"]]
    assert pay and pay[0]["amount"] == Decimal("-100")
    assert totals["total"] >= Decimal("300")


def test_same_payment_again_asks_and_token_repeat_writes_once(client, db, site, staff, halima, carrot):
    _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}], payment="debt"))
    data = {"supplier_id": halima.id, "amount": "100", "source": "cash", "payer_id": str(staff.id),
            "pay_date": date.today().isoformat(), "form_token": "tok-pay-00000000000000001"}
    assert client.post("/new/pay", data=data, follow_redirects=False).status_code == 303
    assert client.post("/new/pay", data=data, follow_redirects=False).status_code == 303   # тот же номер
    assert db.query(SupplierPayment).filter_by(supplier_id=halima.id).count() == 1
    r = client.post("/new/pay", data={**data, "form_token": "tok-pay-00000000000000002"}, follow_redirects=False)
    assert r.status_code == 200 and "Такое уже записано" in r.text
    assert db.query(SupplierPayment).filter_by(supplier_id=halima.id).count() == 1
    r = client.post("/new/pay", data={**data, "form_token": "tok-pay-00000000000000003", "repeat_ok": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert db.query(SupplierPayment).filter_by(supplier_id=halima.id).count() == 2
