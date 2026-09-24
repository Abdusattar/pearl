"""Обзор нового входа (16.09, макет блок 6)."""
from datetime import date

import pytest

from app.models import Organization, User
from app.routers import new_buy as buy_router
from app.routers import new_overview as ov_router
from app.services import overview as svc


@pytest.fixture()
def site(db):
    root = Organization(name="Тест корень об", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-об", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    school = Organization(name="Школа тест-об", type="school", parent_id=root.id, site_id=sadik.id)
    db.add(school)
    db.flush()
    return sadik, school


def _login(monkeypatch, user, site):
    monkeypatch.setattr(ov_router, "get_current_user", lambda request, db: user)
    monkeypatch.setattr(buy_router, "resolve_org", lambda org_id, user, db: site)


def test_founder_sees_both_columns(client, db, site, monkeypatch):
    sadik, school = site
    f = User(name="Айдай тест-об", role="founder", organization_id=sadik.id)
    db.add(f)
    db.flush()
    _login(monkeypatch, f, sadik)
    page = client.get("/new/overview")
    assert page.status_code == 200
    assert "Школа тест-об" in page.text and "Садик тест-об" in page.text
    assert "На счетах" in page.text and "Родители" in page.text and "просрочено" in page.text


def test_manager_sees_only_kindergarten(client, db, site, monkeypatch):
    sadik, school = site
    mu = User(name="Мунара тест-об", role="manager", organization_id=sadik.id)
    db.add(mu)
    db.flush()
    _login(monkeypatch, mu, sadik)
    assert [o.id for o in svc.visible_orgs(db, sadik.id, mu)] == [sadik.id]
    page = client.get("/new/overview")
    assert page.status_code == 200 and "Школа тест-об" not in page.text


def test_status_line_counts_warnings_not_info():
    assert svc.status_line([]) == "Всё в порядке"
    assert svc.status_line([{"kind": "info"}]) == "Всё в порядке"
    assert svc.status_line([{"kind": "warn"}, {"kind": "warn"}, {"kind": "info"}]) == "Две вещи требуют внимания"
