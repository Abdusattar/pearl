"""OCR model comparison test."""
import base64
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

KEY = os.getenv("OPENROUTER_API_KEY")
URL = "https://openrouter.ai/api/v1/chat/completions"

MODELS = [
    "google/gemini-2.5-flash-lite",
]

RECEIPTS = [
    ("media/receipts/5242699173746906970.jpg", "пони"),   # 18 позиций, сложный почерк
    ("media/receipts/5242699173746906974.jpg", "профиль"), # 2 позиции, простой
]

PROMPT = (
    "Look at this handwritten receipt image. Read every filled row in the table exactly as written — "
    "do not guess or invent names. Return ONLY valid JSON, no markdown.\n"
    "Format: {\"amount\": <total number or null>, "
    "\"items\": [{\"name\": \"<exact text from image>\", \"qty\": <number or null>, "
    "\"unit_price\": <number or null>, \"total_price\": <number>}]}"
)


def run(model: str, img_path: str, expected: str) -> None:
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": PROMPT},
            ],
        }],
        "max_tokens": 2048,
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://pearl.local",
        "X-Title": "Pearl OCR Test",
    }
    try:
        resp = requests.post(URL, json=payload, headers=headers, timeout=90)
        if resp.status_code != 200:
            print(f"HTTP {resp.status_code}: {resp.text[:300]}")
            return
        text = resp.json()["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        print(text[:3000])
        found = expected.lower() in text.lower()
        print(f"\n>>> '{expected}' found: {found}")
    except Exception as e:
        print(f"EXCEPTION: {e}")


def main():
    for model in MODELS:
        for img_path, expected in RECEIPTS:
            print(f"\n{'='*60}")
            print(f"MODEL: {model}  |  {img_path.split('/')[-1]}")
            print("=" * 60)
            run(model, img_path, expected)


if __name__ == "__main__":
    main()
