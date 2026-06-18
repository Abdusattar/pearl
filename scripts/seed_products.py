"""
Одноразовый скрипт: засеять справочник продуктов из существующих квитанций.
Запуск: python scripts/seed_products.py
Идемпотентен — повторный запуск ничего не сломает.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal
from app.services.products import get_or_create_product, ensure_alias

# canonical name → список OCR-вариантов из реальных квитанций
CATALOG = {
    "Лук":        ["nusz", "лук", "Лук", "пьяс"],
    "Картофель":  ["картошка", "картофель", "Картофель", "Картошка"],
    "Укроп":      ["укроп", "Укроп"],
    "Петрушка":   ["петрушка", "Петрушка"],
    "Капуста":    ["капуста", "Капуста"],
    "Морковь":    ["сабиз", "морковь", "Морковь"],
    "Редька":     ["редь на", "редька", "Редька"],
    "Хлеб":       ["1 ХЛЕБ БЕЛЫЙ ХЛЕБНЫЙ МИР МОСКОВСКИЙ", "хлеб", "Хлеб", "нан"],
    "Зелень":     ["золента", "зелень", "Зелень"],
    "Пирожки":    ["пирожки", "Пирожки"],
    "Тряпка":     ["тряпка", "Тряпка"],
    "Щётка":      ["щетка", "Щетка", "щётка", "Щётка"],
}

def main():
    db = SessionLocal()
    try:
        created_products = 0
        created_aliases = 0

        for canonical, aliases in CATALOG.items():
            from app.models import Product
            existing = db.query(Product).filter(
                Product.name == canonical
            ).first()
            if not existing:
                product = get_or_create_product(db, canonical)
                created_products += 1
                print(f"  + Продукт: {canonical}")
            else:
                product = existing
                print(f"  = Продукт: {canonical} (уже есть)")

            for raw in aliases:
                from app.models import ProductAlias
                from sqlalchemy import func
                exists = db.query(ProductAlias).filter(
                    func.lower(ProductAlias.raw_text) == raw.strip().lower()
                ).first()
                if not exists:
                    ensure_alias(db, raw, product.id)
                    created_aliases += 1
                    print(f"      + алиас: {raw!r}")

        db.commit()
        print(f"\nГотово: {created_products} продуктов, {created_aliases} алиасов создано.")
    except Exception as e:
        db.rollback()
        print(f"Ошибка: {e}")
        raise
    finally:
        db.close()

if __name__ == "__main__":
    main()
