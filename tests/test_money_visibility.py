"""Деньги видны по объекту (владелец 24.09): Мунара и Махабат — только садик; Айжан (общий
директор), владелец, учредители — всё. Ни в Кассе, ни в «Сегодня», ни в истории."""
from datetime import date
from decimal import Decimal

import pytest

from app.models import Organization, User
from app.routers import new_buy as buy_router
from app.routers import new_cash as cash_router
from app.routers import new_today as today_router
from app.services import cash as svc
from app.services import today


@pytest.fixture()
def world(db):
    root = Organization(name="Корень тест-вд", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-вд", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    school = Organization(name="Школа тест-вд", type="school", parent_id=root.id, site_id=sadik.id)
    db.add(school)
    db.flush()
    m = User(name="Махабат тест-вд", role="staff", organization_id=sadik.id, password_hash="x")
    mu = User(name="Мунара тест-вд", role="manager", organization_id=sadik.id, password_hash="x")
    a = User(name="Айжан тест-вд", role="director", organization_id=school.id, password_hash="x")
    o = User(name="Владелец тест-вд", role="owner", organization_id=sadik.id, password_hash="x")
    db.add_all([m, mu, a, o])
    db.flush()
    svc.recount(db, user=mu, site_org_id=sadik.id, pocket_user_id=mu.id, actual=Decimal("5000"), d=date.today(), reason="старт")
    svc.recount(db, user=a, site_org_id=sadik.id, pocket_user_id=a.id, actual=Decimal("90055"), d=date.today(), reason="старт")
    svc.bank_balance(db, user=mu, org_id=sadik.id, actual=Decimal("135"), d=date.today(), reason="старт")
    svc.bank_balance(db, user=a, org_id=school.id, actual=Decimal("2089650"), d=date.today(), reason="старт")
    db.flush()
    return {"sadik": sadik, "school": school, "m": m, "mu": mu, "a": a, "o": o}


def _names(st):
    return {r["user"].name for r in st["cash"]["rows"]}, {x["org"].name for x in st["accounts"]}


def test_manager_and_staff_see_only_sadik(db, world):
    for who in (world["mu"], world["m"]):
        people, accs = _names(svc.state(db, world["sadik"].id, viewer=who))
        assert "Айжан тест-вд" not in people and "Мунара тест-вд" in people
        assert "Школа тест-вд" not in accs
    hist = svc.history(db, world["sadik"].id, viewer=world["mu"])
    assert not any("Айжан" in h["title"] or "Школа" in h["title"] for h in hist)
    assert any("Мунара" in h["title"] for h in hist)


def test_director_and_owner_see_everything(db, world):
    for who in (world["a"], world["o"]):
        people, accs = _names(svc.state(db, world["sadik"].id, viewer=who))
        assert {"Айжан тест-вд", "Мунара тест-вд"} <= people and "Школа тест-вд" in accs
    hist = svc.history(db, world["sadik"].id, viewer=world["a"])
    assert any("Школа" in h["title"] for h in hist) and any("Мунара" in h["title"] for h in hist)


def test_today_figures_hide_school_from_sadik_people(db, world):
    f = today.now_figures(db, world["sadik"].id, viewer=world["mu"])
    assert all(p["name"] != "Айжан тест-вд" for p in f["pockets"])
    assert all(a["name"] != "Школа тест-вд" for a in f["accounts"])
    assert f["cash"] == 5000.0
    f = today.now_figures(db, world["sadik"].id, viewer=world["o"])
    assert any(p["name"] == "Айжан тест-вд" for p in f["pockets"]) and f["cash"] == 95055.0


def test_cash_page_for_manager_has_no_school_numbers(client, db, world, monkeypatch):
    db.commit()
    monkeypatch.setattr(cash_router, "get_current_user", lambda request, db: world["mu"])
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: world["sadik"])
    page = client.get("/new/cash").text
    assert "2 089 650" not in page and "90 055" not in page and "5 000" in page
    monkeypatch.setattr(today_router, "get_current_user", lambda request, db: world["mu"])
    page = client.get("/new/today").text
    assert "2 089 650" not in page and "90 055" not in page
