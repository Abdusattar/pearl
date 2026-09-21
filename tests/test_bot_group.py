"""Бот в группе, день 1 (21.09): понимает и отвечает, ничего не записывает. Модель подменена."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import (BotMessage, CashFunding, Organization, Purchase, Supplier, SupplierPayment, Transaction,
                        User)
from app.services import bot as svc
from app.services import bot_group as grp

GROUP = -1001234567890


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень бг", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-бг", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    return sadik


@pytest.fixture()
def people(db, site, monkeypatch):
    m = User(name="Махабаттест Бг", role="staff", organization_id=site.id, tg_id=900000000101)
    n = User(name="Мунаратест Бг", role="manager", organization_id=site.id, tg_id=900000000102)
    db.add_all([m, n])
    db.flush()
    monkeypatch.setattr(svc, "site_for_bot", lambda db: site)
    monkeypatch.delenv(svc.TOKEN_ENV, raising=False)
    monkeypatch.setenv(svc.GROUP_ENV, str(GROUP))
    monkeypatch.setattr(svc, "download_file", lambda file_id: b"photo-" + file_id.encode())
    return m, n


@pytest.fixture()
def supplier(db):
    s = Supplier(name="Халиматест Овощи", phone="0000")
    db.add(s)
    db.flush()
    return s


def _model(monkeypatch, answer: dict):
    calls = []

    def fake(prompt, image=None, mime="image/jpeg"):
        calls.append(prompt)
        return answer
    monkeypatch.setattr(grp, "ask_model", fake)
    return calls


def _upd(user, text=None, photo_id=None, message_id=1, chat=GROUP):
    msg = {"message_id": message_id, "chat": {"id": chat, "type": "supergroup"}, "from": {"id": user.tg_id}}
    if text is not None:
        msg["text" if photo_id is None else "caption"] = text
    if photo_id:
        msg["photo"] = [{"file_id": photo_id, "file_unique_id": "u-" + photo_id, "file_size": 10}]
    return {"message": msg}


def _purchase(db, site, supplier, user, total, d):
    p = Purchase(site_org_id=site.id, supplier_id=supplier.id, date=d, total=total, payment="cash",
                 paid_amount=total, created_by=user.id)
    db.add(p)
    db.flush()
    db.add(Transaction(organization_id=site.id, type="expense", amount=total, date=d, supplier_id=supplier.id,
                       purchase_id=p.id, created_by=user.id))
    db.flush()
    return p


def _writes(db, site):
    return (db.query(Transaction).filter(Transaction.organization_id == site.id).count(),
            db.query(CashFunding).filter(CashFunding.organization_id == site.id).count(),
            db.query(SupplierPayment).count())


def test_receipt_in_system(db, site, people, supplier, monkeypatch):
    m, _ = people
    _purchase(db, site, supplier, m, Decimal("705"), date.today())
    _model(monkeypatch, {"kind": "purchase", "supplier": "Халиматест Овощи", "date": date.today().isoformat(), "amount": 706})
    before = _writes(db, site)
    reply = svc.handle_update(db, _upd(m, photo_id="a"))
    assert "Закуп: Халиматест Овощи" in reply and "в системе есть" in reply
    assert _writes(db, site) == before          # день 1: ничего не записано
    log = db.query(BotMessage).filter_by(kind="group_photo").one()
    assert log.payload["reply"] == reply and log.payload["message_id"] == 1


def test_receipt_missing_and_same_photo_again(db, site, people, supplier, monkeypatch):
    m, _ = people
    _model(monkeypatch, {"kind": "purchase", "supplier": "Халиматест Овощи", "amount": 2625,
                         "date": (date.today() - timedelta(days=10)).isoformat()})
    reply = svc.handle_update(db, _upd(m, photo_id="b"))
    assert "в системе нет" in reply and "от " in reply   # старая дата названа словами
    again = svc.handle_update(db, _upd(m, photo_id="b", message_id=2))
    assert "уже присылали" in again


def test_service_is_not_a_purchase(db, site, people, monkeypatch):
    m, _ = people
    _model(monkeypatch, {"kind": "service", "supplier": "Электрик", "amount": 1500})
    assert svc.handle_update(db, _upd(m, photo_id="c")).startswith("Услуга, не на склад")


def test_supplier_payment_is_not_a_new_purchase(db, site, people, supplier, monkeypatch):
    m, _ = people
    db.add(SupplierPayment(supplier_id=supplier.id, amount=8380, date=date.today(), organization_id=site.id,
                           paid_from_user_id=m.id, created_by=m.id))
    db.flush()
    _model(monkeypatch, {"kind": "supplier_payment", "supplier": "Халиматест Овощи", "amount": 8380})
    reply = svc.handle_update(db, _upd(m, photo_id="d"))
    assert "не новый закуп" in reply and "в системе есть" in reply


def test_list_without_prices_asks_kitchen_or_count(db, site, people, monkeypatch):
    m, _ = people
    _model(monkeypatch, {"kind": "kitchen", "sure": False})
    assert "кухня" in svc.handle_update(db, _upd(m, photo_id="e"))
    # подпись решает сама
    _model(monkeypatch, {"kind": "kitchen", "sure": False, "date": date.today().isoformat()})
    assert "пересчёт" in svc.handle_update(db, _upd(m, text="остаток", photo_id="f", message_id=3)).lower()


def test_salary_list_never_shows_sums_in_group(db, site, people, monkeypatch):
    _, n = people
    _model(monkeypatch, {"kind": "salary", "amount": 250000})
    reply = svc.handle_update(db, _upd(n, photo_id="g"))
    assert "личку" in reply and "250" not in reply


def test_chatter_does_not_call_model(db, site, people, monkeypatch):
    m, _ = people
    calls = _model(monkeypatch, {"kind": "none"})
    assert svc.handle_update(db, _upd(m, text="доброе утро, девочки")) is None
    assert calls == []


def test_withdrawal_text_finds_earlier_one(db, site, people, monkeypatch):
    _, n = people
    db.add(CashFunding(organization_id=site.id, source_type="withdrawal", amount=250000,
                       date=date.today() - timedelta(days=2), taken_by=n.id, accountable_user_id=n.id))
    db.flush()
    _model(monkeypatch, {"kind": "withdrawal", "amount": 250000, "date": "today"})
    reply = svc.handle_update(db, _upd(n, text="сняла 250 000"))
    assert "снятие 250 000" in reply and "карман: Мунаратест" in reply and "уже записано" in reply


def test_transfer_fills_author_as_the_other_side(db, site, people, monkeypatch):
    m, n = people
    _model(monkeypatch, {"kind": "transfer", "amount": 20000, "who": None, "to": "Махабаттест"})
    reply = svc.handle_update(db, _upd(n, text="передала Махабат 20 000"))
    assert "Мунаратест → Махабаттест 20 000" in reply


def test_other_groups_are_ignored(db, site, people, monkeypatch):
    m, _ = people
    calls = _model(monkeypatch, {"kind": "purchase", "amount": 100})
    assert svc.handle_update(db, _upd(m, photo_id="h", chat=-100999)) is None
    assert calls == []


def test_model_failure_is_silent_and_logged(db, site, people, monkeypatch):
    m, _ = people

    def boom(*a, **k):
        raise RuntimeError("openrouter down")
    monkeypatch.setattr(grp, "ask_model", boom)
    assert svc.handle_update(db, _upd(m, photo_id="i")) is None
    assert db.query(BotMessage).filter_by(kind="group_error").count() == 1


def test_far_date_is_dropped(monkeypatch):
    _model(monkeypatch, {"kind": "purchase", "date": "2026-01-01", "amount": 7167})
    info = grp.read_photo(b"x", date(2026, 9, 21))
    assert info["date"] is None
