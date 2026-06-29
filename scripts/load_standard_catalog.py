"""Загрузка эталонного каталога из data/standard_products.json.

- Старые продукты без ReceiptItem удаляются
- Новые добавляются с is_standard=True
- Существующие по имени: is_standard=True + обновляются unit/category
- Алиасы добавляются идемпотентно
"""
import sys, json
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal
from app.models import Product, ProductAlias, ReceiptItem
from sqlalchemy import func

db = SessionLocal()

catalog = json.loads(
    (Path(__file__).parent.parent / 'data' / 'standard_products.json').read_text(encoding='utf-8')
)

added = updated = alias_added = removed = 0
standard_names = {item['name'].strip().lower() for item in catalog}

for item in catalog:
    name     = item['name'].strip()
    category = item['category'].strip()
    unit     = item.get('unit', 'кг')
    aliases  = item.get('aliases', [])

    existing = db.query(Product).filter(func.lower(Product.name) == name.lower()).first()
    if existing:
        existing.category    = category
        existing.unit        = unit
        existing.is_standard = True
        product = existing
        updated += 1
    else:
        product = Product(name=name, category=category, unit=unit, is_standard=True)
        db.add(product)
        db.flush()
        added += 1

    for raw in aliases:
        raw = raw.strip()
        conflict = db.query(ProductAlias).filter(
            func.lower(ProductAlias.raw_text) == raw.lower()
        ).first()
        if not conflict:
            db.add(ProductAlias(raw_text=raw, product_id=product.id))
            db.flush()
            alias_added += 1

db.commit()

# Удалить нестандартные продукты без ReceiptItem
old_products = db.query(Product).filter(Product.is_standard == False).all()
for p in old_products:
    has_items = db.query(ReceiptItem).filter(ReceiptItem.product_id == p.id).first()
    if not has_items:
        db.query(ProductAlias).filter(ProductAlias.product_id == p.id).delete()
        db.delete(p)
        removed += 1

db.commit()

total = db.query(Product).count()
standard = db.query(Product).filter(Product.is_standard == True).count()
print(f'Добавлено:       {added}')
print(f'Обновлено:       {updated}')
print(f'Алиасов:         +{alias_added}')
print(f'Удалено старых:  {removed}')
print(f'Итого в БД:      {total} (эталонных: {standard})')
db.close()
