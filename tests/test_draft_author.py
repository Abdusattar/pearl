"""Чек из чата от Айжан (23.09): черновик сразу «для школы, из её кармана» — подтверждает Махабат."""
import pytest

from app.models import Organization, Receipt, User
from app.routers import new_buy as buy_router


@pytest.fixture()
def world(db, monkeypatch):
    root = Organization(name="Корень тест-ав", type="root")
    db.add(root)
    db.flush()
    sadik = Organization(name="Садик тест-ав", type="kindergarten", parent_id=root.id)
    db.add(sadik)
    db.flush()
    school = Organization(name="Школа тест-ав", type="school", parent_id=root.id, site_id=sadik.id)
    db.add(school)
    db.flush()
    counter = User(name="Учётчик тест-ав", role="staff", organization_id=sadik.id)
    director = User(name="Директор тест-ав", role="director", organization_id=school.id)
    db.add_all([counter, director])
    db.flush()
    monkeypatch.setattr(buy_router, "get_current_user", lambda request, db: counter)
    monkeypatch.setattr(buy_router, "_site", lambda user, db: sadik)
    rc = Receipt(organization_id=sadik.id, file_path="receipts/test-av.jpg", ocr_status="pending", created_by=director.id)
    db.add(rc)
    db.flush()
    return {"school": school, "director": director, "rc": rc}


def test_draft_from_director_defaults_to_school_and_her_pocket(client, world):
    page = client.get(f"/new/buy?receipt={world['rc'].id}")
    assert page.status_code == 200
    html = page.text
    assert f'value="{world["school"].id}" checked' in html
    assert f'<option value="{world["director"].id}" selected>' in html
