"""Бот, шаг 2 (24.09): сообщение о деньгах / скрин банка → «Верно?» в личку источнику →
«да» записывает, «нет» — нет. Модель подменена."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import BotMessage, CashFunding, CashTransfer, Organization, Reconciliation, Supplier, User
from app.services import bot as svc
from app.services import bot_group as grp
from app.services import bot_money

GROUP = -1001234567891


@pytest.fixture()
def world(db, monkeypatch, tmp_path):
    root = Organization(name="Корень тест-дн", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-дн", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    m = User(name="Махабаттест Дн", role="staff", organization_id=sadik.id, tg_id=900000000201)
    n = User(name="Мунаратест Дн", role="manager", organization_id=sadik.id, tg_id=900000000202)
    db.add_all([m, n])
    db.flush()
    monkeypatch.setattr(svc, "site_for_bot", lambda db: sadik)
    monkeypatch.delenv(svc.TOKEN_ENV, raising=False)
    monkeypatch.setenv(svc.GROUP_ENV, str(GROUP))
    monkeypatch.setattr(svc, "download_file", lambda file_id: b"photo-" + file_id.encode())
    monkeypatch.setattr(svc, "_account_holder", lambda db, org: n)
    from app.services import drafts
    monkeypatch.setattr(drafts, "MEDIA_ROOT", tmp_path)
    return {"sadik": sadik, "m": m, "n": n}


def _model(monkeypatch, answer: dict):
    monkeypatch.setattr(grp, "ask_model", lambda prompt, image=None, mime="image/jpeg", pdf=None: answer)


def _group(user, text=None, photo_id=None, mid=1):
    msg = {"message_id": mid, "chat": {"id": GROUP, "type": "supergroup"}, "from": {"id": user.tg_id}}
    if text is not None:
        msg["text" if photo_id is None else "caption"] = text
    if photo_id:
        msg["photo"] = [{"file_id": photo_id, "file_unique_id": "u-" + photo_id, "file_size": 10}]
    return {"message": msg}


def _private(user, text, mid=50):
    return {"message": {"message_id": mid, "chat": {"id": user.tg_id, "type": "private"},
                        "from": {"id": user.tg_id}, "text": text}}


def _offer(db, user):
    return db.query(BotMessage).filter_by(kind=bot_money.OFFER, user_id=user.id).order_by(BotMessage.id.desc()).first()


def test_withdrawal_in_group_asks_pocket_owner_and_yes_records(db, world, monkeypatch):
    n = world["n"]
    _model(monkeypatch, {"kind": "withdrawal", "amount": 25000, "who": None, "date": "today", "sure": True})
    svc.handle_update(db, _group(n, "сняла 25 000"))
    o = _offer(db, n)
    assert o is not None and "снятие 25 000 со счёта садика в карман Мунаратест" in o.text
    assert db.query(CashFunding).filter_by(organization_id=world["sadik"].id).count() == 0   # до «да» — ничего
    reply = svc.handle_update(db, _private(n, "да"))
    assert reply == "Записал. Спасибо!"
    f = db.query(CashFunding).filter_by(organization_id=world["sadik"].id).one()
    assert f.amount == Decimal("25000") and f.accountable_user_id == n.id and f.created_by == n.id
    assert _offer(db, n).status == "answered"


def test_no_declines_and_nothing_written(db, world, monkeypatch):
    m, n = world["m"], world["n"]
    _model(monkeypatch, {"kind": "transfer", "amount": 20000, "who": None, "to": "Махабаттест", "date": "today"})
    svc.handle_update(db, _group(n, "передала Махабат 20 000"))
    o = _offer(db, n)
    assert "передача Мунаратест → Махабаттест 20 000" in o.text
    assert "Не записал" in svc.handle_update(db, _private(n, "нет"))
    assert db.query(CashTransfer).filter_by(site_org_id=world["sadik"].id).count() == 0


def test_transfer_said_by_receiver_asks_giver(db, world, monkeypatch):
    m, n = world["m"], world["n"]
    _model(monkeypatch, {"kind": "transfer", "amount": 20000, "who": "Мунаратест", "to": None, "date": "today"})
    svc.handle_update(db, _group(m, "получила от Мунары 20 000"))
    assert _offer(db, n) is not None and _offer(db, m) is None
    svc.handle_update(db, _private(n, "да"))
    t = db.query(CashTransfer).filter_by(site_org_id=world["sadik"].id).one()
    assert (t.from_user_id, t.to_user_id) == (n.id, m.id)


def test_already_recorded_is_not_asked(db, world, monkeypatch):
    n = world["n"]
    from app.services import cash
    cash.withdraw(db, user=n, site_org_id=world["sadik"].id, account_org_id=world["sadik"].id,
                  amount=Decimal("25000"), d=date.today())
    _model(monkeypatch, {"kind": "withdrawal", "amount": 25000, "date": "today"})
    svc.handle_update(db, _group(n, "сняла 25 000"))
    assert _offer(db, n) is None


def test_bank_screenshot_asks_holder_for_end_of_yesterday(db, world, monkeypatch):
    n = world["n"]
    _model(monkeypatch, {"kind": "bank", "bank_op": "balance", "balance": 64797.68, "date": date.today().isoformat(),
                         "sure": True})
    svc.handle_update(db, _group(n, photo_id="bank1"))
    o = _offer(db, n)
    y = date.today() - timedelta(days=1)
    assert o is not None and f"на конец {y.strftime('%d.%m')}" in o.text
    assert svc.handle_update(db, _private(n, "да, Optima 18.09 в пути")) == "Записал. Спасибо!"
    rec = db.query(Reconciliation).filter_by(kind="account", organization_id=world["sadik"].id).one()
    assert rec.date == y and rec.actual_amount == Decimal("64797.68") and rec.reason == "Optima 18.09 в пути"


def test_private_text_asks_self_once(db, world, monkeypatch):
    n = world["n"]
    _model(monkeypatch, {"kind": "withdrawal", "amount": 10000, "date": "yesterday"})
    reply = svc.handle_update(db, _private(n, "вчера сняла 10 000"))
    assert reply.startswith("Мунаратест, записываю: снятие 10 000") and ", вчера." in reply
    assert db.query(BotMessage).filter_by(kind=bot_money.OFFER, user_id=n.id).count() == 1
    svc.handle_update(db, _private(n, "да", mid=51))
    assert db.query(CashFunding).filter_by(organization_id=world["sadik"].id).one().date == date.today() - timedelta(days=1)


def test_unknown_person_is_not_guessed(db, world, monkeypatch):
    m = world["m"]
    _model(monkeypatch, {"kind": "transfer", "amount": 5000, "who": "Гульнара", "to": "Бакыт", "date": "today"})
    svc.handle_update(db, _group(m, "Гульнара передала Бакыту 5 000"))
    assert db.query(BotMessage).filter_by(kind=bot_money.OFFER).count() == 0


def test_on_hand_in_private_asks_self_and_yes_records_pocket_point(db, world, monkeypatch):
    """Айжан 24.09: «наличных школы у меня 12 400» в личку → «верно?» → «да, причина» → точка кармана."""
    n = world["n"]
    reply = svc.handle_update(db, _private(n, "наличных у меня на руках 12 400"))
    assert reply.startswith("Мунаратест, записываю: наличных у Мунаратест на руках 12 400")
    assert "Записал" in svc.handle_update(db, _private(n, "да, остаток с прошлого месяца", mid=51))
    rec = db.query(Reconciliation).filter_by(kind="pocket", subject_id=n.id).one()
    assert rec.actual_amount == Decimal("12400") and rec.reason == "остаток с прошлого месяца"


def test_excel_in_private_is_saved_and_owner_told(db, world, monkeypatch, tmp_path):
    n = world["n"]
    monkeypatch.setattr(svc, "MEDIA_ROOT", tmp_path)
    upd = {"message": {"message_id": 7, "chat": {"id": n.tg_id, "type": "private"}, "from": {"id": n.tg_id},
                       "document": {"file_id": "x1", "file_unique_id": "ux1", "file_name": "дети.xlsx",
                                    "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}}}
    reply = svc.handle_update(db, upd)
    assert reply == "Файл получил, передал Абдусаттару. Спасибо!"
    saved = db.query(BotMessage).filter_by(kind="file", user_id=n.id).one()
    assert (tmp_path / saved.payload["file"]).read_bytes() == b"photo-x1"
    assert db.query(BotMessage).filter_by(kind="owner_copy").count() == 1
