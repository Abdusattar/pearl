import sys, json, os, re, httpx, time
from pathlib import Path
from collections import defaultdict
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

key = os.getenv('OPENROUTER_API_KEY')

BATCHES = [
    {
        "cats": "Крупы и мука | Масла и жиры | Бобовые | Специи и приправы | Консервы",
        "hint": "Include: all flour grades (v/s, 1s), rice variants, all cereals (psheno, grechka, perlovka, ovyanka, kukuruznaya), pasta types, all oils (sunflower, cotton, butter, margarine), all legumes (lentils red+green, chickpeas, peas split+whole, various beans), all spices used in KR cooking (zira, koriander, kurkuma, bay leaf, red+black pepper, paprika, garlic powder), vinegar variants, tomato paste, canned fish, canned veggies",
    },
    {
        "cats": "Молочные продукты | Яйца | Мясо и птица | Рыба",
        "hint": "Include: milk 2.5% and 3.2%, kefir variants, smetana 20%+25%, tvorog, syr tvyordiy, ryazhenka, slivki, airan, suzme, kurt; chicken whole+parts+mince, beef+mince, lamb, pork, sausages, sosiskiy; mintai, gorbush, selyod, canned fish; eggs",
    },
    {
        "cats": "Овощи | Фрукты | Сухофрукты и орехи | Хлеб и выпечка | Напитки",
        "hint": "Include ALL vegetables common in KR: potato, carrot, onion, cabbage, beet, tomato, cucumber, pepper, garlic, zucchini, eggplant, pumpkin, radish, green onion, dill, parsley, cilantro, celery, spinach, leek; ALL fruits: apple, pear, banana, orange, mandarin, lemon, grape, watermelon, melon, apricot, peach, plum, cherry, strawberry, pomegranate; dried fruits: raisins, dried apricots (uruk), prunes, dates, figs; nuts: walnut, peanut, sunflower seeds; bread types, lavash, sushki, sukhari; tea black+green, rosehip, chicory, cocoa, kisel, kompot",
    },
]

all_products = []

for i, batch in enumerate(BATCHES):
    print(f"\n=== Batch {i+1}: {batch['cats'][:50]}... ===")
    prompt = (
        f"You are building a canonical product list for a Kyrgyz school cafeteria (Sokuluk, Kyrgyzstan).\n"
        f"Give products for ONLY these categories: {batch['cats']}\n"
        f"Hints: {batch['hint']}\n\n"
        "Rules:\n"
        "- Russian name as on Kyrgyz invoices/receipts\n"
        "- Include specific variants (fat%, grade, type)\n"
        "- kyrgyz_name: Kyrgyz word if commonly used in local trade (null otherwise)\n"
        "- unit: кг / л / шт / г / пач / уп\n\n"
        "Return ONLY valid JSON (no markdown, no trailing text):\n"
        '{"products": [{"name": "Мука пшеничная в/с", "category": "Крупы и мука", "unit": "кг", "kyrgyz_name": "Ун"}]}'
    )

    payload = {
        'model': 'google/gemini-2.5-flash-lite',
        'messages': [{'role': 'user', 'content': prompt}],
        'max_tokens': 3000,
        'temperature': 0.15,
    }
    resp = httpx.post(
        'https://openrouter.ai/api/v1/chat/completions',
        json=payload,
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        timeout=90
    )
    text = resp.json()['choices'][0]['message']['content'].strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.DOTALL).strip()
    idx = text.find('{')
    data, _ = json.JSONDecoder().raw_decode(text, idx)
    batch_products = data['products']
    print(f"  Got {len(batch_products)} products")
    all_products.extend(batch_products)
    time.sleep(1)

# Print full catalog
print(f"\n{'='*50}")
print(f"TOTAL: {len(all_products)} products")
print(f"{'='*50}\n")

by_cat = defaultdict(list)
for p in all_products:
    by_cat[p['category']].append(p)

for cat, items in sorted(by_cat.items()):
    print(f"[{cat}] — {len(items)} шт")
    for p in items:
        ky = f" | кырг: {p['kyrgyz_name']}" if p.get('kyrgyz_name') else ''
        print(f"  {p['name']} ({p['unit']}){ky}")
    print()

Path('data').mkdir(exist_ok=True)
with open('data/gemini_catalog.json', 'w', encoding='utf-8') as f:
    json.dump(all_products, f, ensure_ascii=False, indent=2)
print(f"Saved -> data/gemini_catalog.json")
