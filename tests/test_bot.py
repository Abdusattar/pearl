"""Бот (блок 7, 16.09): тексты, расписание, ответы в личке. Без Telegram."""
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.models import BotMessage, Organization, Reconciliation, User
from app.services import bot as svc
from app.services import cash


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень бт", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-бт", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    school = Organization(name="Школа тест-бт", type="school", parent_id=root.id, site_id=sadik.id)
    db.add(school)
    db.flush()
    return sadik


@pytest.fixture()
def people(db, site, monkeypatch):
    m = User(name="Махабат тест-бт", role="staff", organization_id=site.id, tg_id=900000000001)
    f = User(name="Айдай тест-бт", role="founder", organization_id=site.id, tg_id=900000000002)
    db.add_all([m, f])
    db.flush()
    monkeypatch.setattr(svc, "site_for_bot", lambda db: site)
    monkeypatch.delenv(svc.TOKEN_ENV, raising=False)
    return m, f


def _upd(tg_id, text=None, private=True, photo=False):
    return {"message": {"chat": {"id": tg_id if private else -100, "type": "private" if private else "supergroup"},
                        "from": {"id": tg_id}, "text": text, **({"photo": [{"file_id": "x", "file_size": 1}]} if photo else {})}}


def test_unknown_user_gets_own_id(db, site, people):
    reply = svc.handle_update(db, _upd(123456789, "привет"))
    assert "123456789" in reply and "Абдусаттару" in reply


def test_group_messages_are_ignored(db, site, people):
    m, _ = people
    assert svc.handle_update(db, _upd(m.tg_id, "40000 в субботу", private=False)) is None


def test_pocket_number_is_not_written_by_bot(db, site, people):
    """21.09: бот не пишет пересчёт мимо проверки Махабат, пятничного «верно?» больше нет."""
    m, _ = people
    cash.withdraw(db, user=m, site_org_id=site.id, account_org_id=site.id, amount=Decimal(10000), d=date.today())
    for text in ("да", "8000", "8000 отдала за хлеб"):
        svc.handle_update(db, _upd(m.tg_id, text))
    assert db.query(Reconciliation).filter_by(kind="pocket", subject_id=m.id).count() == 0
    reply = svc.handle_update(db, _upd(m.tg_id, "5000"))
    assert "Кассе" in reply


def test_owner_ok_forwards_summary_to_founders(db, site, people, monkeypatch):
    m, f = people
    owner = User(name="Владелец тест-бт", role="owner", organization_id=site.id, tg_id=900000000009)
    db.add(owner)
    db.flush()
    monkeypatch.setattr(svc, "OWNER_USER_ID", owner.id)
    reply = svc.handle_update(db, _upd(owner.tg_id, "сводка"))
    assert "Отправить Айдай" in reply
    assert db.query(BotMessage).filter_by(kind="founders_review", status="pending").count() == 1
    reply = svc.handle_update(db, _upd(owner.tg_id, "ок"))
    assert "Отправил" in reply
    outs = db.query(BotMessage).filter_by(kind="founders_summary", user_id=f.id).all()
    assert len(outs) == 1 and "Наличных" in outs[0].text


def test_schedule_is_idempotent(db, site, people, monkeypatch):
    m, f = people
    friday = datetime(2026, 9, 18, 17, 5)   # пятница: вопроса «на руках X?» больше нет (21.09)
    sent = svc.run_scheduled(db, friday)
    assert not any(k.startswith("pocket:") for k in sent)
    assert db.query(BotMessage).filter_by(kind="pocket_ask", user_id=m.id).count() == 0
    monday = datetime(2030, 9, 16, 9, 1)   # не сегодняшний: локальный сервер пишет свои задачи в ту же базу
    sent = svc.run_scheduled(db, monday)
    assert "group_signals:2030-09-16" in sent and "founders:2030-09-16" in sent
