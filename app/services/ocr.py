import base64
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

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
    "Look at this receipt image — it may be handwritten, printed, or a mix. "
    "Extract all product line items. Return ONLY valid JSON, no markdown.\n\n"
    "Rules:\n"
    "1. If you see format 'NAME  N×P' or 'NAME  NxP' (quantity × price), parse as: "
    "name=NAME, qty=N, unit_price=P, total_price=N*P (compute it).\n"
    "2. If total_price is not written but qty and unit_price are known, compute total_price = qty * unit_price.\n"
    "3. If the same delivery appears twice (e.g. 4810 + 4810 = 9620), list the items twice.\n"
    "4. Ignore phone numbers, supplier/person names, and pure arithmetic lines.\n"
    "5. Read product names exactly as written — do not translate or normalize.\n\n"
    "Format: {\"amount\": <grand total number or null>, "
    "\"items\": [{\"name\": \"<exact text>\", \"qty\": <number or null>, "
    "\"unit_price\": <number or null>, \"total_price\": <number or null>}]}"
)


def compute_hash(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()


def analyze_receipt(file_path: str) -> dict | None:
    """Send receipt image to OpenRouter vision model, return structured data."""
    if not _OR_KEY:
        print("[OCR] OPENROUTER_API_KEY not set — skipping OCR", flush=True)
        return None

    try:
        with open(file_path, "rb") as f:
            img_bytes = f.read()
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
            if total is None:
                continue
            if unit_price is None and qty and qty > 0:
                unit_price = round(total / qty, 2)
            items.append({
                "name": str(it.get("name") or "").strip(),
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
