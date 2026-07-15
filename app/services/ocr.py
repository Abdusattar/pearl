import base64
import hashlib
import io
import json
import os
import re
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image, ImageOps

_LOG_FILE = Path(__file__).parent.parent.parent / "logs" / "openrouter_usage.jsonl"

# Цены gemini-2.5-flash-lite ($/M токенов)
_PRICE = {"input": 0.10, "output": 0.40}


def _log_usage(model: str, usage: dict, file_path: str, ok: bool):
    _LOG_FILE.parent.mkdir(exist_ok=True)
    inp = usage.get("prompt_tokens", 0)
    out = usage.get("completion_tokens", 0)
    cost = round((inp * _PRICE["input"] + out * _PRICE["output"]) / 1_000_000, 6)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "file": Path(file_path).name,
        "tokens_in": inp,
        "tokens_out": out,
        "cost_usd": cost,
        "ok": ok,
    }
    with open(_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

load_dotenv()

_OR_KEY = os.getenv("OPENROUTER_API_KEY")
_OR_MODEL = "google/gemini-2.5-flash-lite"
_OR_URL = "https://openrouter.ai/api/v1/chat/completions"

_USER_PROMPT = (
    "Look at this receipt image — it may be handwritten, printed, or a mix, and different "
    "receipts use very different layouts. These receipts are from Kyrgyzstan and are written "
    "in Russian, using the Cyrillic alphabet — expect Cyrillic letters, not Latin. If handwriting "
    "is unclear, transcribe your best guess using Cyrillic letters that match the pen strokes; "
    "never substitute similar-looking Latin letters or output a Latin/English word instead of an "
    "illegible Cyrillic one. Extract all product line items. Return ONLY valid JSON, no markdown.\n\n"
    "Rules:\n"
    "1. If you see format 'NAME  N×P' or 'NAME  NxP' (quantity × price), parse as: "
    "name=NAME, qty=N, unit_price=P, total_price=N*P (compute it).\n"
    "2. If total_price is not written but qty and unit_price are known, compute total_price = qty * unit_price.\n"
    "3. If the same delivery appears twice (e.g. 4810 + 4810 = 9620), list the items twice.\n"
    "4. Names and their quantity×price formula may sit in two SEPARATE columns (e.g. a left column "
    "of checklist item names, a right column of arithmetic) instead of one line. Pair each name with "
    "the formula on the SAME visual row. Never put a number or arithmetic expression into the "
    "\"name\" field, and never shift a pairing by one row — if you cannot confidently pair a name "
    "with a row, leave name null rather than guessing or inserting digits.\n"
    "5. Sometimes one logical item is split across two adjacent visual lines: a line with a name "
    "and NO numbers at all, immediately followed by a line with numbers and NO name (e.g. a long "
    "name wraps to the next table row, or a printed cash-register receipt prints the name alone "
    "then 'price*qty' alone on the next line). Merge such a name-only + numbers-only pair into ONE "
    "item. Do NOT merge two lines that each already contain both a name and a number — those are "
    "two separate items, even if adjacent and similar. Ignore VAT/НДС/НСП lines and payment-status "
    "lines (e.g. \"ТОВАР... ПОЛНЫЙ РАСЧЕТ\") — they are not items.\n"
    "6. Ignore anything that is not part of THIS purchase list: phone numbers, supplier/person "
    "names, pure arithmetic/subtotal lines, rotated or upside-down text bleeding through from the "
    "reverse side of reused paper, and unrelated documents in the same photo (tax/patent forms, "
    "other receipts). The final total of the list goes in \"amount\", never as an item.\n"
    "7. Read product names exactly as written — do not translate or normalize.\n\n"
    "Format: {\"amount\": <grand total number or null>, "
    "\"items\": [{\"name\": \"<exact text or null>\", \"qty\": <number or null>, "
    "\"unit_price\": <number or null>, \"total_price\": <number or null>}]}"
)


def compute_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def _normalize_orientation(img_bytes: bytes) -> bytes:
    """Повернуть фото по EXIF-тегу orientation перед отправкой в OCR —
    телефоны часто пишут повёрнутое фото + флаг поворота вместо готовых пикселей,
    а vision-модель читает пиксели как есть."""
    try:
        img = Image.open(io.BytesIO(img_bytes))
        fmt = img.format or "JPEG"
        transposed = ImageOps.exif_transpose(img)
        buf = io.BytesIO()
        transposed.save(buf, format=fmt)
        return buf.getvalue()
    except Exception:
        return img_bytes


def analyze_receipt(file_path: str) -> dict | None:
    """Send receipt image to OpenRouter vision model, return structured data."""
    if not _OR_KEY:
        print("[OCR] OPENROUTER_API_KEY not set — skipping OCR", flush=True)
        return None

    try:
        with open(file_path, "rb") as f:
            img_bytes = f.read()
        img_bytes = _normalize_orientation(img_bytes)
        b64 = base64.b64encode(img_bytes).decode()
        ext = str(file_path).rsplit(".", 1)[-1].lower()
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"

        payload = {
            "model": _OR_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        {"type": "text", "text": _USER_PROMPT},
                    ],
                }
            ],
            "max_tokens": 2048,
            "temperature": 0.1,
        }
        headers = {
            "Authorization": f"Bearer {_OR_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://pearl.local",
            "X-Title": "Pearl Receipt OCR",
        }
        resp = httpx.post(_OR_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        resp_json = resp.json()
        _log_usage(_OR_MODEL, resp_json.get("usage", {}), file_path, ok=True)

        text = resp_json["choices"][0]["message"]["content"].strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()

        # Gemini иногда добавляет текст после JSON — raw_decode берёт первый объект
        idx = text.find("{")
        if idx == -1:
            raise ValueError("JSON object not found in OCR response")
        data, _ = json.JSONDecoder().raw_decode(text, idx)
        amount = _safe_num(data.get("amount"))
        items = []
        for it in data.get("items") or []:
            total = _safe_num(it.get("total_price"))
            qty = _safe_num(it.get("qty"))
            unit_price = _safe_num(it.get("unit_price"))
            # Вычислить total если Gemini его не вернул, но есть qty × unit_price
            if total is None and qty and unit_price:
                total = round(qty * unit_price, 2)
            name = str(it.get("name") or "").strip()
            # Без имени это не позиция товара (даже если какие-то цифры распознались) —
            # человек добавит такую строку вручную, если она вообще нужна.
            if not name:
                continue
            if unit_price is None and total is not None and qty and qty > 0:
                unit_price = round(total / qty, 2)
            items.append({
                "name": name,
                "qty": qty,
                "unit_price": unit_price,
                "total_price": total,
            })

        return {"raw": text, "amount": amount, "items": items}

    except Exception as e:
        print(f"[OCR ERROR] {e}", flush=True)
        _log_usage(_OR_MODEL, {}, file_path, ok=False)
        return None


def run_ocr(file_path: str) -> dict | None:
    result = analyze_receipt(file_path)
    if result:
        return {"raw": result["raw"], "amount": result["amount"]}
    return None


def _safe_num(val) -> float | None:
    if val is None:
        return None
    try:
        return float(str(val).replace(" ", "").replace(",", "."))
    except (ValueError, TypeError):
        return None
