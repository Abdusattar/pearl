"""Переход на новый вход: категории с уровнем, слияние и переименование
карточек, смена единицы с коэффициентом (раскладка 15.09)."""
from datetime import date
from decimal import Decimal

import pytest

from app.models import (Organization, Product, ProductAlias, StockCount, StockCountLine, User,
                        WarehouseReceipt, WriteOff)
from app.services.products import find_product, rank_candidates
from app.services.transition import (apply_layout, change_unit, ensure_categories,
                                     merge_product, rename_product)


@pytest.fixture()
def org(db):
    o = Organization(name="Садик тест-переход", type="kindergarten")
    db.add(o)
    db.flush()
    return o


def _product(db, name, unit="кг", standard=True):
    p = Product(name=name, unit=unit, is_standard=standard)
    db.add(p)
    db.flush()
    return p


def _receipt(db, product, org, qty, price):
    r = WarehouseReceipt(date=date.today(), product_id=product.id, quantity=qty,
                         price_per_unit=price, total_cost=qty * price, organization_id=org.id)
    db.add(r)
    db.flush()
    return r


def test_categories_have_levels(db):
    cats = ensure_categories(db)
    assert cats["зелень"].is_minor and cats["канцелярия"].is_minor
    assert not cats["мясо и рыба"].is_minor
    again = ensure_categories(db)          # идемпотентно
    assert again["зелень"].id == cats["зелень"].id


def test_merge_moves_history_and_keeps_pointer(db, org):
    luk = _product(db, "Лук тест-переход")
    rep = _product(db, "лук реп тест-переход", standard=False)
    db.add(ProductAlias(raw_text="пияз тест-переход", product_id=rep.id))
    _receipt(db, rep, org, 30, 35)
    db.add(WriteOff(date=date.today(), product_id=rep.id, quantity=2, organization_id=org.id))
    db.flush()

    moved = merge_product(db, rep, luk)
    assert moved["warehouse_receipts"] == 1 and moved["write_offs"] == 1
    assert db.query(WarehouseReceipt).filter_by(product_id=luk.id).count() == 1
    assert db.query(WarehouseReceipt).filter_by(product_id=rep.id).count() == 0
    assert rep.merged_into_id == luk.id and rep.is_standard is False
    # старое имя и старый алиас ведут на цель, в кандидатах слитой нет
    assert find_product(db, "лук реп тест-переход").id == luk.id
    assert find_product(db, "пияз тест-переход").id == luk.id
    ids = {c["id"] for c in rank_candidates(db, "лук реп тест-переход", standard_only=False)}
    assert luk.id in ids and rep.id not in ids


def test_merge_with_factor_rescales_qty_and_price(db, org):
    tea = _product(db, "Чай тест-переход", unit="г")
    packs = _product(db, "чай пачки тест-переход", unit="уп", standard=False)
    _receipt(db, packs, org, 3, 400)
    merge_product(db, packs, tea, factor=500)
    r = db.query(WarehouseReceipt).filter_by(product_id=tea.id).one()
    assert r.quantity == Decimal("1500.000") and r.price_per_unit == Decimal("0.80")
    assert r.total_cost == 1200                    # деньги не меняются


def test_merge_stock_count_lines_sum_into_target(db, org):
    a = _product(db, "Тряпки тест-переход", unit="шт")
    b = _product(db, "тряпка тест-переход", unit="шт", standard=False)
    user = User(name="Тест переход", role="owner", organization_id=org.id)
    db.add(user)
    db.flush()
    sc = StockCount(organization_id=org.id, count_date=date.today(), started_by=user.id)
    db.add(sc)
    db.flush()
    db.add(StockCountLine(count_id=sc.id, product_id=a.id, actual_qty=4, mode="number"))
    db.add(StockCountLine(count_id=sc.id, product_id=b.id, actual_qty=6, mode="number"))
    db.flush()
    merge_product(db, b, a)
    lines = db.query(StockCountLine).filter_by(count_id=sc.id).all()
    assert len(lines) == 1 and lines[0].product_id == a.id and lines[0].actual_qty == 10


def test_rename_keeps_old_name_as_alias(db):
    p = _product(db, "мясо филе тест-переход")
    rename_product(db, p, "Говядина филе тест-переход")
    assert p.name == "Говядина филе тест-переход"
    assert find_product(db, "мясо филе тест-переход").id == p.id
    other = _product(db, "Занято тест-переход")
    with pytest.raises(ValueError):
        rename_product(db, p, "занято тест-переход")


def test_change_unit_rescales_history(db, org):
    tea = _product(db, "Чай кг тест-переход", unit="кг")
    _receipt(db, tea, org, Decimal("0.5"), 800)
    change_unit(db, tea, "г", 1000)
    r = db.query(WarehouseReceipt).filter_by(product_id=tea.id).one()
    assert tea.unit == "г" and r.quantity == 500 and r.price_per_unit == Decimal("0.8")


def test_apply_layout_dry_run_writes_nothing(db, org):
    luk = _product(db, "Лук тест-раскладка")
    rep = _product(db, "лук реп тест-раскладка", standard=False)
    taxi = _product(db, "такси тест-раскладка", standard=False)
    _receipt(db, rep, org, 10, 30)
    rows = [
        {"id": str(luk.id), "name": luk.name, "new_category": "овощи и фрукты", "level": "склад", "merge_into": ""},
        {"id": str(rep.id), "name": rep.name, "new_category": "овощи и фрукты", "level": "склад", "merge_into": str(luk.id)},
        {"id": str(taxi.id), "name": taxi.name, "new_category": "не товар", "level": "—", "merge_into": ""},
        {"id": "999999", "name": "Нет такой", "new_category": "зелень", "level": "мелочь", "merge_into": ""},
    ]
    lines = apply_layout(db, rows, renames={luk.id: "Лук репчатый тест-раскладка"}, dry_run=True)
    text = "\n".join(lines)
    assert "слита в «Лук тест-раскладка»" in text and "warehouse_receipts 1" in text
    assert "не товар" in text and "Нет такой" in text
    assert "«Лук тест-раскладка» → «Лук репчатый тест-раскладка»" in text
    db.expire_all()
    assert db.get(Product, rep.id).merged_into_id is None
    assert db.get(Product, luk.id).category_id is None
    assert db.get(Product, luk.id).name == "Лук тест-раскладка"

    lines = apply_layout(db, rows, renames={luk.id: "Лук репчатый тест-раскладка"}, dry_run=False)
    db.expire_all()
    assert db.get(Product, rep.id).merged_into_id == luk.id
    assert db.get(Product, luk.id).product_category.name == "овощи и фрукты"
    assert db.get(Product, taxi.id).retired_at is not None
    assert find_product(db, "такси тест-раскладка") is None
    assert db.get(Product, luk.id).name == "Лук репчатый тест-раскладка"
    # повторный прогон ничего не меняет
    again = apply_layout(db, rows, renames={luk.id: "Лук репчатый тест-раскладка"}, dry_run=False)
    assert [l for l in again if not l.startswith("!")] == []


def test_unit_change_goes_before_merge(db, org):
    """Пачка чая 500 г сливается в карточку, которая в той же раскладке
    переводится из кг в г: коэффициент слияния задан в граммах, поэтому
    смена единицы цели должна пройти первой (найдено на diff по проду 15.09)."""
    tea = _product(db, "Чай кг тест-порядок", unit="кг")
    packs = _product(db, "чай уп тест-порядок", unit="уп", standard=False)
    _receipt(db, tea, org, Decimal("0.5"), 800)
    _receipt(db, packs, org, 3, 400)
    rows = [
        {"id": str(tea.id), "name": tea.name, "new_category": "напитки и вода", "level": "склад", "merge_into": ""},
        {"id": str(packs.id), "name": packs.name, "new_category": "напитки и вода", "level": "склад", "merge_into": str(tea.id)},
    ]
    apply_layout(db, rows, unit_changes={tea.id: ("г", 1000)}, merge_factors={packs.id: 500}, dry_run=False)
    db.expire_all()
    qtys = sorted(r.quantity for r in db.query(WarehouseReceipt).filter_by(product_id=tea.id))
    assert qtys == [Decimal("500.000"), Decimal("1500.000")]
    assert db.get(Product, tea.id).unit == "г"
