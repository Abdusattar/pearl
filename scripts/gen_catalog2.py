import sys, json, os, re, httpx
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

key = os.getenv('OPENROUTER_API_KEY')

prompt = (
    "You are building a canonical product reference for a school and kindergarten cafeteria in Kyrgyzstan (Central Asia).\n\n"
    "Give me a COMPREHENSIVE list of food products that are actually purchased in Kyrgyz school cafeterias. "
    "Think of real markets in Bishkek/Sokuluk: Orto-Say bazaar, Osh bazaar, local stores.\n\n"
    "Rules:\n"
    "- Russian name as written on invoices/receipts in Kyrgyzstan\n"
    "- Include brand-specific variants where relevant (e.g. different fat % for milk, different grades of flour)\n"
    "- Include legumes: lentils, chickpeas, various beans\n"
    "- Include all common vegetables and fruits of the region\n"
    "- Include all common spices used in KR cooking\n"
    "- Include canned goods (tomato paste, canned fish, canned beans)\n"
    "- Include cooking oils: sunflower, cotton seed (хлопковое)\n"
    "- Include local products: kurt, suzme, airan, kymyz\n"
    "- For each: kyrgyz_name if commonly used in trade (like 'жумуртка' for eggs)\n"
    "- unit: kg/l/sht/g/pach/up\n\n"
    "Categories (use EXACTLY these Russian names):\n"
    "Крупы и мука | Масла и жиры | Молочные продукты | Мясо и птица | Рыба | Яйца | "
    "Овощи | Фрукты | Бобовые | Специи и приправы | Консервы | Хлеб и выпечка | Напитки | Сухофрукты и орехи\n\n"
    "Return ONLY valid JSON:\n"
    '{"products": [{"name": "Мука пшеничная в/с", "category": "Крупы и мука", "unit": "кг", "kyrgyz_name": "Ун"}]}\n\n'
    "Give at least 120 products. Be specific with variants (e.g. separate entries for different fat % milks)."
)

payload = {
    'model': 'google/gemini-2.5-flash-lite',
    'messages': [{'role': 'user', 'content': prompt}],
    'max_tokens': 8192,
    'temperature': 0.2,
}
resp = httpx.post(
    'https://openrouter.ai/api/v1/chat/completions',
    json=payload,
    headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
    timeout=120
)

text = resp.json()['choices'][0]['message']['content'].strip()
text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.DOTALL).strip()
idx = text.find('{')
data, _ = json.JSONDecoder().raw_decode(text, idx)
products = data['products']
print(f'Total products: {len(products)}')
print()

from collections import defaultdict
by_cat = defaultdict(list)
for p in products:
    by_cat[p['category']].append(p)

for cat, items in by_cat.items():
    print(f'[{cat}] — {len(items)} шт')
    for p in items:
        ky = f' | кырг: {p["kyrgyz_name"]}' if p.get('kyrgyz_name') else ''
        print(f'  {p["name"]} ({p["unit"]}){ky}')
    print()

Path('data').mkdir(exist_ok=True)
with open('data/gemini_catalog.json', 'w', encoding='utf-8') as f:
    json.dump(products, f, ensure_ascii=False, indent=2)
print(f'Saved {len(products)} products -> data/gemini_catalog.json')
