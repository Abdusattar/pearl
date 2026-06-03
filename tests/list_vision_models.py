import os, requests
from dotenv import load_dotenv
load_dotenv()
key = os.getenv("OPENROUTER_API_KEY")
resp = requests.get("https://openrouter.ai/api/v1/models", headers={"Authorization": f"Bearer {key}"})
models = resp.json()["data"]
vision = [
    m for m in models
    if "vl" in m["id"].lower()
    or "vision" in m["id"].lower()
    or "image" in str(m.get("architecture", {})).lower()
    or "multimodal" in str(m.get("architecture", {})).lower()
]
for m in sorted(vision, key=lambda x: x["id"]):
    pricing = m.get("pricing", {})
    price = float(pricing.get("prompt", 0)) * 1_000_000
    print(f"{m['id']:65s}  ${price:.3f}/Mtok")
