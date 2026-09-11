"""Дым: страница склада рендерится и правка/удаление строки работают через HTTP."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models import (AuditLog, Organization, Product, User, WarehouseReceipt,
                        WriteOff)


@pytest.fixture()
def setup(db):
    org = Organization(name="__Смоук-склад__", type="садик")
    db.add(org); db.flush()
    import bcrypt
    user = User(name="__Тестовый учётчик__", role="staff", organization_id=org.id,
                password_hash=bcrypt.hashpw(b"test-pass", bcrypt.gensalt()).decode())
    db.add(user); db.flush()
    p = Product(name="__Масло тест__", unit="кг", category="масла")
    db.add(p); db.flush()
    db.add(WarehouseReceipt(date=date.today() - timedelta(days=1), product_id=p.id,
                            quantity=Decimal("0.4"), price_per_unit=Decimal("300"),
                            total_cost=Decimal("120"), organization_id=org.id))
    w = WriteOff(date=date.today(), product_id=p.id, quantity=Decimal("200"),
                 organization_id=org.id, reason="питание детей", created_by=user.id)
    db.add(w); db.flush()
    return {"org": org, "user": user, "product": p, "writeoff": w}


def _login(client, user):
    client.post("/login", data={"user_id": user.id, "password": "test-pass"})


def test_page_renders(client, setup):
    _login(client, setup["user"])
    r = client.get(f"/warehouse/?org_id={setup['org'].id}")
    assert r.status_code == 200, r.text[:500]
    html = r.text
    assert "Расход по дням" in html
    assert "__Масло тест__" in html
    assert "в минус" in html, "минус должен быть показан отдельно"


def test_edit_through_http(client, db, setup):
    _login(client, setup["user"])
    wid = setup["writeoff"].id
    r = client.post(f"/warehouse/writeoff/{wid}/edit",
                    data={"org_id": str(setup["org"].id), "quantity": "0,2"},
                    follow_redirects=False)
    assert r.status_code == 302, r.text[:400]
    db.expire_all()
    assert float(db.get(WriteOff, wid).quantity) == pytest.approx(0.2)
    log = db.query(AuditLog).filter_by(entity_type="write_off", entity_id=wid).all()
    assert log and log[-1].action == "update"


def test_edit_over_stock_refused(client, db, setup):
    _login(client, setup["user"])
    wid = setup["writeoff"].id
    r = client.post(f"/warehouse/writeoff/{wid}/edit",
                    data={"org_id": str(setup["org"].id), "quantity": "999"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert "err=" in r.headers["location"]
    db.expire_all()
    assert float(db.get(WriteOff, wid).quantity) == pytest.approx(200), "значение не должно меняться"


def test_delete_through_http(client, db, setup):
    _login(client, setup["user"])
    wid = setup["writeoff"].id
    r = client.post(f"/warehouse/writeoff/{wid}/delete",
                    data={"org_id": str(setup["org"].id)}, follow_redirects=False)
    assert r.status_code == 302
    db.expire_all()
    assert db.get(WriteOff, wid).deleted_at is not None
