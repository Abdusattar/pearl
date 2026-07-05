"""Загрузка эталонного каталога продуктов из data/gemini_catalog.json в БД.

Старые продукты без привязанных ReceiptItem удаляются.
Новые добавляются идемпотентно (по точному имени).
Кыргызские названия добавляются как алиасы.
"""
import sys, json
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal
from app.models import Product, ProductAlias, ReceiptItem
from sqlalchemy import func

db = SessionLocal()

catalog = json.loads((Path(__file__).parent.parent / 'data' / 'gemini_catalog.json').read_text(encoding='utf-8'))

# Категория каталога → category поле продукта
# (используем как есть, без отдельной таблицы)

added = 0
skipped = 0
alias_added = 0

for item in catalog:
    name = item['name'].strip()
    category = item['category'].strip()
    unit = item.get('unit', 'кг')
    kyrgyz = item.get('kyrgyz_name')

    existing = db.query(Product).filter(func.lower(Product.name) == name.lower()).first()
    if existing:
        # Обновить категорию и единицу если изменились
        existing.category = category
        skipped += 1
        product = existing
    else:
        product = Product(name=name, category=category)
        db.add(product)
        db.flush()
        added += 1

    # Кыргызское название как алиас — только если уникально
    if kyrgyz and kyrgyz.strip():
        ky_key = kyrgyz.strip().lower()
        conflict = db.query(ProductAlias).filter(
            func.lower(ProductAlias.raw_text) == ky_key
        ).first()
        if not conflict:
            db.add(ProductAlias(raw_text=kyrgyz.strip(), product_id=product.id))
            db.flush()
            alias_added += 1

db.commit()

# Удалить старые продукты без ReceiptItem и без новых записей
all_new_names = {item['name'].strip().lower() for item in catalog}
old_products = db.query(Product).all()
removed = 0
for p in old_products:
    if p.name.lower() not in all_new_names:
        has_items = db.query(ReceiptItem).filter(ReceiptItem.product_id == p.id).first()
        if not has_items:
            db.query(ProductAlias).filter(ProductAlias.product_id == p.id).delete()
            db.delete(p)
            removed += 1

db.commit()

total = db.query(Product).count()
print(f'Добавлено:  {added}')
print(f'Обновлено:  {skipped}')
print(f'Удалено:    {removed} (старые без привязки)')
print(f'Алиасов:    +{alias_added}')
print(f'Итого в БД: {total} продуктов')
db.close()
