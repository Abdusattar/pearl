"""Test OCR via the actual ocr.py service function + cost estimate."""
import sys, json, base64, os, requests
sys.path.insert(0, ".")
from dotenv import load_dotenv
load_dotenv()

path = r"media/receipts/5242699173746907002.jpg"

# Direct call to get usage stats
key = os.getenv("OPENROUTER_API_KEY")
with open(path, "rb") as f:
    b64 = base64.b64encode(f.read()).decode()

from app.services.ocr import _USER_PROMPT, _OR_MODEL
payload = {
    "model": _OR_MODEL,
    "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": _USER_PROMPT},
    ]}],
    "max_tokens": 2048,
    "temperature": 0.1,
}
resp = requests.post(
    "https://openrouter.ai/api/v1/chat/completions",
    json=payload,
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    timeout=90,
)
data = resp.json()
usage = data.get("usage", {})
prompt_tokens = usage.get("prompt_tokens", 0)
completion_tokens = usage.get("completion_tokens", 0)
total_tokens = usage.get("total_tokens", 0)

# gemini-2.5-flash-lite: $0.10/Mtok input, $0.40/Mtok output
cost_usd = (prompt_tokens * 0.10 + completion_tokens * 0.40) / 1_000_000
cost_som = cost_usd * 87  # ~87 сом за $1

print(f"Модель:      {_OR_MODEL}")
print(f"Input tok:   {prompt_tokens}")
print(f"Output tok:  {completion_tokens}")
print(f"Total tok:   {total_tokens}")
print(f"Стоимость:   ${cost_usd:.6f}  (~{cost_som:.4f} сом)")
print(f"\n--- За 100 чеков: ${cost_usd*100:.4f} (~{cost_som*100:.2f} сом) ---")
