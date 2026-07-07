"""AI-нормализация OCR-строк квитанции против каталога продуктов."""
import json
import os
import re

import httpx
from sqlalchemy.orm import Session

from app.models import Product

_OR_KEY = os.getenv("OPENROUTER_API_KEY")
_OR_URL = "https://openrouter.ai/api/v1/chat/completions"
_OR_MODEL = "google/gemini-2.5-flash-lite"


def _build_catalog_text(db: Session) -> str:
    """Строит каталог для Gemini: эталонные первыми, временные — отдельно."""
    products = db.query(Product).order_by(
        Product.is_standard.desc(), Product.category, Product.name
    ).all()
    lines = []
    for p in products:
        flag = "STD" if p.is_standard else "TMP"
        lines.append(f"{p.id}|{p.name}|{p.category or ''}|{flag}")
    return "\n".join(lines)


def normalize_items(db: Session, raw_items: list[dict]) -> list[dict]:
    """
    Принимает список OCR-строк: [{"name": "...", "qty": ..., "unit_price": ..., "total_price": ...}]
    Возвращает тот же список с добавленными полями:
      - matched_product_id: int | None
      - matched_name: str | None
      - match_type: "exact" | "ai_standard" | "ai_provisional" | "none"
      - is_standard: bool
    """
    if not raw_items or not _OR_KEY:
        for item in raw_items:
            item.update({"matched_product_id": None, "matched_name": None,
                         "match_type": "none", "is_standard": False, "suggested_name": None})
        return raw_items

    catalog_text = _build_catalog_text(db)
    ocr_lines = "\n".join(f"{i+1}. {it['name']}" for i, it in enumerate(raw_items))

    prompt = (
        "You are a product matcher for a school/kindergarten purchasing system in Kyrgyzstan "
        "(food, household chemicals, construction materials, toys).\n\n"
        "CATALOG (id|name|category|flag) — STD=standard, TMP=provisional:\n"
        f"{catalog_text}\n\n"
        "OCR ITEMS from handwritten receipt:\n"
        f"{ocr_lines}\n\n"
        "For each OCR item, find the best matching product from the catalog.\n"
        "Rules:\n"
        "- OCR may contain typos, Kyrgyz words, brand names, or noise\n"
        "- Match by meaning, not just spelling (жум=яйца, такива=тыква, сабиз=морковь, пияз=лук)\n"
        "- Prefer STD products over TMP products when both could match\n"
        "- If item is clearly unreadable garbage (not a real product at all), use null for product_id AND clean_name\n"
        "- If the OCR text is purely digits, an arithmetic expression, or looks like a handwritten "
        "note/annotation rather than a product name (e.g. a quantity note, a person's name, a "
        "reminder), use null for product_id AND clean_name — do not guess a product from it\n"
        "- Return the catalog product ID (integer) for matches\n\n"
        "If there is NO catalog match (product_id is null) but the item IS CONFIDENTLY a real, "
        "readable product name,\n"
        "propose \"clean_name\": a short GENERIC standardized name in the same style as the catalog —\n"
        "strip brand names, grades, weights, package sizes, variants. Examples:\n"
        "\"Говядина тушёная высший сорт Казахская 500гр\" → \"Говядина тушёная\"\n"
        "\"Цемент М400 50кг\" → \"Цемент\"\n"
        "\"Fairy лимон 500мл\" → \"Средство для мытья посуды\"\n"
        "If genuinely unreadable/garbage, OR you are not confident it names a specific product, "
        "clean_name must be null too — a wrong guess is worse than saying null.\n\n"
        "Return ONLY valid JSON:\n"
        '{"matches": [{"n": 1, "product_id": 63, "product_name": "Картофель", "is_standard": true, "clean_name": null}, '
        '{"n": 2, "product_id": null, "product_name": null, "is_standard": false, "clean_name": "Говядина тушёная"}]}'
    )

    try:
        resp = httpx.post(
            _OR_URL,
            json={
                "model": _OR_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1024,
                "temperature": 0.1,
            },
            headers={"Authorization": f"Bearer {_OR_KEY}", "Content-Type": "application/json"},
            timeout=45,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()
        idx = text.find("{")
        data, _ = json.JSONDecoder().raw_decode(text, idx)
        matches = {m["n"]: m for m in data["matches"]}
    except Exception as e:
        print(f"[NORMALIZE ERROR] {e}", flush=True)
        for item in raw_items:
            item.update({"matched_product_id": None, "matched_name": None,
                         "match_type": "none", "is_standard": False, "suggested_name": None})
        return raw_items

    for i, item in enumerate(raw_items):
        m = matches.get(i + 1, {})
        pid = m.get("product_id")
        pname = m.get("product_name")
        ai_standard = bool(m.get("is_standard", False))
        clean_name = m.get("clean_name")

        if pid:
            # Верифицируем флаг прямо из БД — не доверяем Gemini на 100%
            product = db.get(Product, pid)
            real_standard = product.is_standard if product else False
            item["matched_product_id"] = pid
            item["matched_name"] = pname
            item["is_standard"] = real_standard
            item["match_type"] = "ai_standard" if real_standard else "ai_provisional"
            item["suggested_name"] = None
        else:
            item["matched_product_id"] = None
            item["matched_name"] = None
            item["match_type"] = "none"
            item["is_standard"] = False
            # Каталог не нашёл совпадения — если это читаемый товар, ИИ предлагает
            # стандартизированное имя (без бренда/сорта/веса), человек его подтверждает/правит.
            item["suggested_name"] = clean_name.strip() if isinstance(clean_name, str) and clean_name.strip() else None

    return raw_items
