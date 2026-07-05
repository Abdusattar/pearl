"""
Прогоняет все квитанции из media/receipts/ через OCR, собирает уникальные позиции
с частотой, классифицирует Python-логикой (без доп. вызовов AI),
выводит data/ocr_catalog.md.

Кэш: data/ocr_scan_cache.json — если прервать, повторный запуск продолжит с места.
"""
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.ocr import analyze_receipt

ROOT = Path(__file__).parent.parent
RECEIPTS_DIR = ROOT / "media" / "receipts"
CACHE_FILE = ROOT / "data" / "ocr_scan_cache.json"
OUT_MD = ROOT / "data" / "ocr_catalog.md"

EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# ── Ключевые слова для классификации «продукты питания» ──────────────────────
FOOD_KEYWORDS = {
    # крупы / мука
    "мука", "рис", "пшено", "гречка", "перловка", "овсян", "геркулес",
    "макарон", "вермишель", "рожки", "крупа", "манка", "кукуруз",
    # масла
    "масло", "маргарин", "май",
    # бобовые
    "горох", "фасоль", "нут", "чечевица", "боб",
    # специи / базовые
    "соль", "сахар", "перец", "зира", "кориандр", "куркума", "лавр",
    "уксус", "томат", "паста", "кетчуп",
    # молочка
    "молоко", "кефир", "сметан", "творог", "сыр", "ряженк", "сливк",
    "айран", "сузьм", "курт", "масло слив",
    # яйца
    "яйц", "жумуртк",
    # мясо / птица
    "мясо", "говяд", "баранин", "курица", "курин", "фарш", "сосиск",
    "сардельк", "колбас", "тоок", "эт",
    # рыба
    "рыб", "минтай", "горбуш", "сельд", "консерв", "балык",
    # овощи
    "картоф", "картошк", "морков", "лук", "капуст", "свекл", "помидор",
    "огурц", "перец", "чеснок", "кабачк", "баклажан", "тыкв", "редис",
    "редьк", "укроп", "петрушк", "кинза", "шпинат", "сельдерей",
    "сабиз", "пияз", "ашкабак", "бадыран",
    # фрукты
    "яблок", "груш", "банан", "апельсин", "мандарин", "лимон", "виноград",
    "арбуз", "дын", "абрикос", "персик", "слив", "вишн", "клубник", "гранат",
    "алма", "өрүк",
    # сухофрукты / орехи
    "изюм", "курага", "урук", "чернослив", "финик", "инжир", "орех",
    "арахис", "семечк", "жаңгак",
    # хлеб / выпечка
    "хлеб", "лаваш", "сушк", "сухар", "нан", "булк", "батон",
    # напитки / чай
    "чай", "какао", "кисель", "компот", "шиповник", "цикорий",
    # общие пищевые
    "продукт", "питан", "еда", "зелень", "овощ", "фрукт", "ягод",
}


def is_food(name: str) -> bool:
    low = name.lower()
    return any(kw in low for kw in FOOD_KEYWORDS)


def load_cache() -> dict:
    if CACHE_FILE.exists():
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def collect_images() -> list[Path]:
    imgs = []
    for p in RECEIPTS_DIR.rglob("*"):
        if p.suffix.lower() in EXTS:
            imgs.append(p)
    return sorted(imgs)


def main():
    images = collect_images()
    print(f"Найдено изображений: {len(images)}")

    cache = load_cache()
    done = len(cache)
    print(f"Уже в кэше: {done} / {len(images)}")

    for i, img in enumerate(images):
        key = str(img)
        if key in cache:
            continue

        print(f"[{i+1}/{len(images)}] {img.name} ...", end=" ", flush=True)
        result = analyze_receipt(str(img))
        if result:
            items = result.get("items") or []
            cache[key] = [it["name"] for it in items if it.get("name")]
            print(f"позиций: {len(cache[key])}")
        else:
            cache[key] = []
            print("нет данных")

        save_cache(cache)
        time.sleep(0.5)

    # ── Агрегация ─────────────────────────────────────────────────────────────
    all_names: list[str] = []
    for names in cache.values():
        all_names.extend(names)

    counter = Counter(n.strip() for n in all_names if n.strip())

    food: list[tuple[str, int]] = []
    other: list[tuple[str, int]] = []

    for name, cnt in counter.most_common():
        if cnt < 2:  # встречается только 1 раз — пропускаем шум
            continue
        if is_food(name):
            food.append((name, cnt))
        else:
            other.append((name, cnt))

    # ── Markdown ──────────────────────────────────────────────────────────────
    lines = [
        "# Каталог позиций из реальных чеков\n",
        f"> Чеков обработано: {len(cache)} | Уникальных позиций (≥2 упоминания): {len(food)+len(other)}\n",
        "---\n",
        f"## Продукты питания ({len(food)} позиций)\n",
        "| Название (из чека) | Чеков |\n",
        "|-------------------|-------|\n",
    ]
    for name, cnt in food:
        lines.append(f"| {name} | {cnt} |\n")

    lines += [
        f"\n## Прочее — часто встречается ({len(other)} позиций)\n",
        "| Название (из чека) | Чеков |\n",
        "|-------------------|-------|\n",
    ]
    for name, cnt in other:
        lines.append(f"| {name} | {cnt} |\n")

    OUT_MD.write_text("".join(lines), encoding="utf-8")
    print(f"\nГотово → {OUT_MD}")
    print(f"  Продукты питания: {len(food)}")
    print(f"  Прочее:           {len(other)}")


if __name__ == "__main__":
    main()
