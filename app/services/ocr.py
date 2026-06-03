import base64
import hashlib
import json
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

_OR_KEY = os.getenv("OPENROUTER_API_KEY")
_OR_MODEL = "google/gemini-2.5-flash-lite"
_OR_URL = "https://openrouter.ai/api/v1/chat/completions"

_USER_PROMPT = (
    "Look at this handwritten receipt image. Read every filled row in the table exactly as written — "
    "do not guess or invent names. Return ONLY valid JSON, no markdown.\n"
    "Format: {\"amount\": <total number or null>, "
    "\"items\": [{\"name\": \"<exact text from image>\", \"qty\": <number or null>, "
    "\"unit_price\": <number or null>, \"total_price\": <number>}]}"
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
        ext = file_path.rsplit(".", 1)[-1].lower()
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
        resp = requests.post(_OR_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()

        text = resp.json()["choices"][0]["message"]["content"].strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.DOTALL).strip()

        data = json.loads(text)
        amount = _safe_num(data.get("amount"))
        items = []
        for it in data.get("items") or []:
            total = _safe_num(it.get("total_price"))
            if total is None:
                continue
            qty = _safe_num(it.get("qty"))
            unit_price = _safe_num(it.get("unit_price"))
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
