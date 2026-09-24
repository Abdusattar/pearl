"""Закрытие дня (владелец 24.09): что мешает закрыть, лестница 15:00 → 16:00 → 16:45 → 17:15,
строка владельцу, утро учредителю."""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models import BotMessage, Organization, User
from app.services import bot as svc
from app.services import bot_group as grp
from app.services import cash, day_close, meals

GROUP = -1001234567892


@pytest.fixture()
def world(db, monkeypatch, tmp_path):
    root = Organization(name="Корень тест-дк", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-дк", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    m = User(name="Махабаттест Дк", role="staff", organization_id=sadik.id, tg_id=900000000301)
    n = User(name="Мунаратест Дк", role="manager", organization_id=sadik.id, tg_id=900000000302)
    db.add_all([m, n])
    db.flush()
    monkeypatch.setattr(svc, "site_for_bot", lambda db: sadik)
    monkeypatch.delenv(svc.TOKEN_ENV, raising=False)
    monkeypatch.setenv(svc.GROUP_ENV, str(GROUP))
    monkeypatch.setattr(svc, "download_file", lambda file_id: b"photo-" + file_id.encode())
    monkeypatch.setattr(svc, "_account_holder", lambda db, org: n)
    monkeypatch.setattr(meals, "expected_today", lambda db, site_id, d=None: True)
    monkeypatch.setattr(meals, "get", lambda db, site_id, d: {"ok": True})
    from app.services import drafts
    monkeypatch.setattr(drafts, "MEDIA_ROOT", tmp_path)
    return {"sadik": sadik, "m": m, "n": n}


def _group(user, text=None, photo_id=None, mid=1):
    msg = {"message_id": mid, "chat": {"id": GROUP, "type": "supergroup"}, "from": {"id": user.tg_id}}
    if text is not None:
        msg["text"] = text
    if photo_id:
        msg["photo"] = [{"file_id": photo_id, "file_unique_id": "u-" + photo_id, "file_size": 10}]
    return {"message": msg}


def _private(user, text, mid=50):
    return {"message": {"message_id": mid, "chat": {"id": user.tg_id, "type": "private"},
                        "from": {"id": user.tg_id}, "text": text}}


def _at(h, mi=0):
    return datetime.combine(date.today(), datetime.min.time()).replace(hour=h, minute=mi)


def test_open_day_lists_items_by_person_then_closes(db, world, monkeypatch):
    s, m, n = world["sadik"], world["m"], world["n"]
    d = date.today()
    # оба ещё ничего не назвали: два пункта про наличные
    lines = day_close.by_person(day_close.items(db, s, d))
    assert lines == ["Махабаттест, наличные на конец дня — написать боту одной цифрой.",
                     "Мунаратест, наличные на конец дня — написать боту одной цифрой."]
    # чек в черновиках — учётчику
    monkeypatch.setattr(grp, "ask_model", lambda prompt, image=None, mime="image/jpeg", pdf=None:
                        {"kind": "purchase", "amount": 12770, "supplier": None, "sure": True})
    svc.handle_update(db, _group(n, photo_id="rc1"))
    lines = day_close.by_person(day_close.items(db, s, d))
    assert lines[0] == "Махабаттест, 1 чек подтвердить; наличные на конец дня — написать боту одной цифрой."
    # 15:00 — в группу каждому по имени
    keys = svc._day_close(db, s, _at(15, 0))
    msg = db.query(BotMessage).filter_by(kind="day_close").one()
    assert msg.chat_id == GROUP and msg.text.startswith("Сделано: едоки записаны. Спасибо!\nОсталось:\nМахабаттест, 1 чек подтвердить")
    # 16:00 — заведующей в личку
    svc._day_close(db, s, _at(16, 0))
    mgr = db.query(BotMessage).filter_by(kind="day_close", user_id=n.id).one()
    assert mgr.text.startswith("Мунаратест, вы заведующая — до конца дня не закрыто:")
    # всё сделали: чек снят, наличные названы и сошлись
    from app.models import Receipt
    for r in db.query(Receipt).all():
        r.ocr_status = "rejected"
    cash.recount(db, user=m, site_org_id=s.id, pocket_user_id=m.id, actual=Decimal("0"), d=d, reason=None)
    cash.recount(db, user=n, site_org_id=s.id, pocket_user_id=n.id, actual=Decimal("0"), d=d, reason=None)
    assert day_close.closed(db, s, d)
    svc._day_close(db, s, _at(16, 45))
    assert db.query(BotMessage).filter_by(job_key=f"day_close:{s.id}:{d.isoformat()}:remind").one().status == "skipped"
    svc._day_close(db, s, _at(17, 15))
    final = db.query(BotMessage).filter_by(job_key=f"day_close:{s.id}:{d.isoformat()}:final").one()
    assert final.text.startswith("День закрыт")
    assert day_close.owner_line(db, s, d) == "Садик тест-дк: день закрыт."


def test_deferred_cash_figure_counts_as_named(db, world, monkeypatch):
    s, n = world["sadik"], world["n"]
    d = date.today()
    monkeypatch.setattr(grp, "ask_model", lambda prompt, image=None, mime="image/jpeg", pdf=None:
                        {"kind": "purchase", "amount": 500, "supplier": None, "sure": True})
    svc.handle_update(db, _group(n, photo_id="rc2"))
    svc.handle_update(db, _private(n, "на руках 1000"))
    texts = [it["text"] for it in day_close.items(db, s, d) if it["who"] is n]
    assert texts == []   # цифра есть, ждёт проверки чеков — держит только пункт про чек


def test_withdrawal_today_requires_account_balance(db, world):
    s, n = world["sadik"], world["n"]
    d = date.today()
    cash.withdraw(db, user=n, site_org_id=s.id, account_org_id=s.id, amount=Decimal("5000"), d=d)
    texts = [it["text"] for it in day_close.items(db, s, d) if it["who"] is n]
    assert any(t.startswith("остаток счёта Садик тест-дк после снятия") for t in texts)
    cash.bank_balance(db, user=n, org_id=s.id, actual=Decimal("10"), d=d, reason="x")
    texts = [it["text"] for it in day_close.items(db, s, d) if it["who"] is n]
    assert not any("остаток счёта" in t for t in texts)


def test_unclosed_day_goes_to_founder_next_morning(db, world, monkeypatch):
    s, n = world["sadik"], world["n"]
    f = User(name="Айдайтест Дк", role="founder", organization_id=s.id, tg_id=900000000303)
    db.add(f)
    db.flush()
    monkeypatch.setattr(svc.rules, "escalate_from", lambda db: date.today() - timedelta(days=30))
    yesterday = date.today() - timedelta(days=1)
    monkeypatch.setattr(day_close, "last_working_day", lambda db, site, d: yesterday)
    text = day_close.founder_text(db, s, yesterday)
    assert text.startswith(f"Вчера ({yesterday.strftime('%d.%m')}) день не закрыт, отвечает Мунаратест:")
    svc._escalate(db, s, _at(9, 5))
    got = db.query(BotMessage).filter_by(kind="escalate", user_id=f.id).one()
    assert "день не закрыт, отвечает Мунаратест" in got.text
