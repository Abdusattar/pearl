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


# ── расход без чека (17.09) ──────────────────────────────────────────────

def _nocheck(client, **kw):
    data = {"amount": "4 800", "kind": "light", "what": "Свет за август", "supplier_name": "Северэлектро тест-лр",
            "for_org": "shared", "payment": "cash", "date": date.today().isoformat()}
    data.update({k: str(v) for k, v in kw.items()})
    return client.post("/new/nocheck", data=data, follow_redirects=False)


def test_nocheck_from_pocket_goes_to_feed_and_category(client, db, site, staff):
    from app.models import ExpenseCategory, Purchase, Transaction
    db.add(ExpenseCategory(name="Электричество"))
    db.flush()
    cash0 = podotchet.get_cash_state(db, site.id)["net"]
    r = _nocheck(client, payer_id=staff.id)
    assert r.status_code == 303, r.text[:300]
    p = _purchase(db, r)
    tx = db.query(Transaction).filter_by(purchase_id=p.id).one()
    assert tx.category_id == db.query(ExpenseCategory).filter_by(name="Электричество").first().id
    assert tx.paid_from_user_id == staff.id and p.receipt_id is None and p.note == "Свет за август"
    assert podotchet.get_cash_state(db, site.id)["net"] == cash0 - Decimal(4800)
    first, last, _ = svc.month_bounds(date.today().strftime("%Y-%m"))
    days, _ = svc.month_rows(db, site.id, first, last)
    row = next(x for d in days for x in d["rows"] if x["url"] == f"/new/buy/{p.id}")
    assert row["title"] == "Северэлектро тест-лр" and row["sub"] == "Свет за август" and row["status"] == "из кассы"
    card = client.get(f"/new/buy/{p.id}?saved=1")
    assert card.status_code == 200 and "Расход без чека: Свет за август" in card.text


def test_nocheck_debt_then_pay(client, db, site, staff):
    r = _nocheck(client, payment="debt", kind="delivery", amount="300", supplier_name="Такси тест-лр", what="доставка мяса")
    p = _purchase(db, r)
    assert get_supplier_balance(db, p.supplier_id) == Decimal(300)
    client.post("/new/pay", data={"supplier_id": p.supplier_id, "amount": "300", "source": "cash",
                                  "payer_id": str(staff.id), "pay_date": date.today().isoformat()})
    assert get_supplier_balance(db, p.supplier_id) == 0


def test_nocheck_repair_needs_object_and_repeat_asks(client, db, site, staff):
    from app.models import Purchase
    r = _nocheck(client, kind="repair", amount="12000", what="краска", supplier_name="Строймаркет тест-лр")
    assert r.status_code == 200 and "Ремонт для кого" in r.text
    assert _nocheck(client, kind="repair", amount="12000", for_org=site.id, supplier_name="Строймаркет тест-лр").status_code == 303
    r = _nocheck(client, kind="repair", amount="12000", for_org=site.id, supplier_name="Строймаркет тест-лр")
    assert r.status_code == 200 and "Такое уже записано" in r.text
    assert db.query(Purchase).filter(Purchase.site_org_id == site.id, Purchase.deleted_at.is_(None)).count() == 1
    assert _nocheck(client, kind="repair", amount="12000", for_org=site.id, supplier_name="Строймаркет тест-лр",
                    repeat_ok="1").status_code == 303


def test_nocheck_remove_rolls_back_cash(client, db, site, staff):
    cash0 = podotchet.get_cash_state(db, site.id)["net"]
    p = _purchase(db, _nocheck(client, payer_id=staff.id, amount="700", kind="other"))
    assert client.post(f"/new/buy/{p.id}/remove", follow_redirects=False).status_code == 303
    assert podotchet.get_cash_state(db, site.id)["net"] == cash0


def test_nocheck_edit_replaces_and_keeps_kind(client, db, site, staff):
    from app.models import ExpenseCategory, Purchase, Transaction
    if not db.query(ExpenseCategory).filter_by(name="Охрана").first():
        db.add(ExpenseCategory(name="Охрана"))
        db.flush()
    cash0 = podotchet.get_cash_state(db, site.id)["net"]
    old = _purchase(db, _nocheck(client, kind="guard", amount="15000", what="охрана за август", payer_id=staff.id))
    card = client.get(f"/new/buy/{old.id}/edit", follow_redirects=False)
    assert card.status_code == 302 and card.headers["location"] == f"/new/nocheck?edit={old.id}"
    form = client.get(f"/new/nocheck?edit={old.id}")
    assert "Поправить расход" in form.text and 'value="guard" checked' in form.text
    new = _purchase(db, _nocheck(client, kind="guard", amount="12000", what="охрана за август", payer_id=staff.id,
                                 replaces_id=old.id))
    db.refresh(old)
    assert old.deleted_at is not None and new.replaces_id == old.id
    assert podotchet.get_cash_state(db, site.id)["net"] == cash0 - Decimal(12000)


def test_feed_debt_row_follows_later_payment(client, db, site, staff, halima, carrot):
    p = _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}], payment="debt"))
    def row():
        first, last, _ = svc.month_bounds(date.today().strftime("%Y-%m"))
        days, _ = svc.month_rows(db, site.id, first, last)
        return next(x for d in days for x in d["rows"] if x["url"] == f"/new/buy/{p.id}")
    assert row()["status"] == "в долг"
    client.post("/new/pay", data={"supplier_id": halima.id, "amount": "100", "source": "cash", "payer_id": str(staff.id),
                                  "pay_date": date.today().isoformat()})
    assert row()["status"] == "200 в долг"
    client.post("/new/pay", data={"supplier_id": halima.id, "amount": "200", "source": "cash", "payer_id": str(staff.id),
                                  "pay_date": date.today().isoformat(), "repeat_ok": "1"})
    assert row()["status"] == "долг оплачен" and row()["status_kind"] == ""


# ── блок «Расходы» целиком (21.09) ───────────────────────────────────────

def test_edit_before_pocket_recount_does_not_charge_again(client, db, site, staff):
    """Фильтры 18.09: поправка записи, внесённой до пересчёта кармана, не должна
    списывать всю новую сумму ещё раз — пересчёт уже видел реальные деньги."""
    from datetime import datetime, timedelta
    from app.models import Reconciliation, Transaction
    from app.services import cash
    old = _purchase(db, _nocheck(client, kind="other", amount="5220", what="фильтры", payer_id=staff.id))
    t_old = datetime.now() - timedelta(hours=2)
    for t in db.query(Transaction).filter_by(purchase_id=old.id).all():
        t.created_at = t_old
    rec = cash.recount(db, user=staff, site_org_id=site.id, pocket_user_id=staff.id, actual=Decimal(5235),
                       d=date.today(), reason="пересчёт")
    rec.created_at = datetime.now() - timedelta(hours=1)
    db.flush()
    assert cash.pocket_balance(db, site.id, staff.id) == Decimal(5235)
    new = _purchase(db, _nocheck(client, kind="other", amount="13400", what="фильтры, 3 шт.", payer_id=staff.id,
                                 replaces_id=old.id))
    assert new.replaces_id == old.id
    assert cash.pocket_balance(db, site.id, staff.id) == Decimal(5235)


def test_legacy_record_opens_in_new_card_and_edits_as_nocheck(client, db, site, staff):
    from app.models import Transaction
    sup = Supplier(name="Такси старое тест-лр", phone="0000")
    db.add(sup)
    db.flush()
    t = Transaction(organization_id=site.id, type="expense", amount=150, date=date.today(), supplier_id=sup.id,
                    description="такси", created_by=staff.id, paid_from_user_id=staff.id)
    db.add(t)
    db.flush()
    first, last, _ = svc.month_bounds(date.today().strftime("%Y-%m"))
    days, _ = svc.month_rows(db, site.id, first, last)
    row = next(x for d in days for x in d["rows"] if x["title"] == sup.name)
    assert row["url"] == f"/new/record/t/{t.id}"
    card = client.get(row["url"])
    assert card.status_code == 200 and "Расход без чека" in card.text and "/expenses/" not in _main(card.text)
    e = client.get(f"/new/record/t/{t.id}/edit", follow_redirects=False)
    assert e.status_code == 302 and e.headers["location"] == f"/new/nocheck?legacy=t:{t.id}"
    form = client.get(e.headers["location"])
    assert "Поправить расход" in form.text and f'value="t:{t.id}"' in form.text
    new = _purchase(db, _nocheck(client, kind="delivery", amount="200", what="такси", supplier_name=sup.name,
                                 payer_id=staff.id, replaces_legacy=f"t:{t.id}"))
    db.refresh(t)
    assert t.deleted_at is not None and new.total == Decimal(200)
    assert client.get(f"/new/record/t/{t.id}").status_code == 404


def test_legacy_record_remove(client, db, site, staff):
    from app.models import Transaction
    t = Transaction(organization_id=site.id, type="expense", amount=990, date=date.today(), description="прочее",
                    created_by=staff.id)
    db.add(t)
    db.flush()
    r = client.post(f"/new/record/t/{t.id}/remove", follow_redirects=False)
    assert r.status_code == 303
    db.refresh(t)
    assert t.deleted_at is not None


def test_photo_receipt_opens_buy_draft_and_can_be_skipped(client, db, site, staff):
    from app.models import AuditLog, Receipt
    rc = Receipt(organization_id=site.id, file_path="receipts/2026-09/test-lr.jpg", ocr_status="pending",
                 created_by=staff.id)
    db.add(rc)
    db.flush()
    from app.services import today as today_svc
    item = next(i for i in today_svc.todo(db, site.id) if i["url"] == f"/new/buy?receipt={rc.id}")
    assert item["title"] == "Чек с фото не внесён" and staff.name in item["sub"]
    page = client.get(f"/new/buy?receipt={rc.id}")
    assert page.status_code == 200 and "выберите, у кого купили" in page.text and f"receipt={rc.id}" in page.text
    assert "Не вносить" in page.text
    r = client.post(f"/new/receipt/{rc.id}/skip", data={"reason": ""}, follow_redirects=False)
    assert "skip_error" in r.headers["location"] and rc.ocr_status == "pending"
    r = client.post(f"/new/receipt/{rc.id}/skip", data={"reason": "уже внесён"}, follow_redirects=False)
    assert r.headers["location"].startswith("/new/today")
    db.refresh(rc)
    assert rc.ocr_status == "rejected"
    assert db.query(AuditLog).filter_by(entity_type="receipt", entity_id=rc.id).first().new_data["reason"] == "уже внесён"
    assert client.get(f"/new/buy?receipt={rc.id}", follow_redirects=False).status_code == 302


def test_suppliers_page_history_and_debt_once(client, db, site, staff, halima, carrot):
    p = _purchase(db, _post(client, halima, [{"name": carrot.name, "pid": carrot.id, "qty": "10", "price": "30"}],
                            payment="debt"))
    page = client.get(f"/new/suppliers/{halima.id}")
    assert page.status_code == 200 and halima.name in page.text and f"/new/buy/{p.id}" in page.text
    assert "/suppliers/" not in page.text.replace("/new/suppliers/", "")
    r = client.post(f"/new/suppliers/{halima.id}/debt", data={"amount": "5000", "reason": ""})
    assert "Напишите, откуда цифра" in r.text
    r = client.post(f"/new/suppliers/{halima.id}/debt", data={"amount": "5000", "reason": "сверили с Халимой"},
                    follow_redirects=False)
    assert r.status_code == 303 and get_supplier_balance(db, halima.id) == Decimal(5000)
    r = client.post(f"/new/suppliers/{halima.id}/debt", data={"amount": "6000", "reason": "ещё раз"})
    assert "уже уточняли" in r.text


def test_expenses_page_has_no_old_links(client, db, site, staff):
    page = client.get("/new/expenses")
    assert page.status_code == 200 and "Зарплата и налоги" in page.text and "/new/suppliers" in page.text
    for old in ('href="/suppliers', 'href="/expenses/', "/podotchet"):
        assert old not in _main(page.text)   # «Старый вход» в меню остаётся до блока «Склад»


def _main(html: str) -> str:
    return html.split('<main', 1)[-1]
