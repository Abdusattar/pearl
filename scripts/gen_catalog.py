import sys, json, os, re, httpx
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv
load_dotenv()

key = os.getenv('OPENROUTER_API_KEY')

prompt = (
    "Ty sostavlyaesh etalonnyi spravochnik produktov pitaniya dlya shkolnoy stolovoy i detskogo sadika "
    "v Kyrgyzstane (Sokuluk, Chuiskaya oblast).\n\n"
    "Zadacha: day polnyi spisok produktov kotorye realno zakupayut v shkolnykh i sadikovykh stolovykh KR. "
    "Tolko produkty pitaniya (ne khoztovary).\n\n"
    "Trebovaniya k spravochniku:\n"
    "- Standartnoe russkoe nazvanie (imenno tak pishut na nakladnykh i chekakh v KR)\n"
    "- Kategoriya na russkom\n"
    "- Edinitsa izmeneniya (kg / l / sht / g / up / pachka)\n"
    "- Ukazhi kyrgyzskoe narodnoe nazvanie esli ono realno ispolzuetsya v ustnoi torgovle\n\n"
    "Kategorii:\n"
    "1. Krypy i muka\n2. Masla i zhiry\n3. Molochnye produkty\n4. Myaso i ptitsa\n"
    "5. Ryba\n6. Yaytsa\n7. Ovoshchi\n8. Frukty\n"
    "9. Bakaleia (sakhar, sol, spetsii, uksus, tomatpasta)\n10. Khleb i vypechka\n11. Napitki\n\n"
    "Return ONLY valid JSON, no markdown:\n"
    '{"products": [{"name": "...", "category": "...", "unit": "...", "kyrgyz_name": null}]}\n\n'
    "IMPORTANT: Write all names in RUSSIAN (Cyrillic). This is critical."
)

payload = {
    'model': 'google/gemini-2.5-flash-lite',
    'messages': [{'role': 'user', 'content': prompt}],
    'max_tokens': 4096,
    'temperature': 0.2,
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
products = data['products']
print(f'Pozitsiy polucheno: {len(products)}')
print()

cur_cat = None
for p in products:
    if p['category'] != cur_cat:
        cur_cat = p['category']
        print(f'[{cur_cat}]')
    ky = f' / kyrg: {p["kyrgyz_name"]}' if p.get('kyrgyz_name') else ''
    print(f'  {p["name"]} ({p["unit"]}){ky}')

Path('data').mkdir(exist_ok=True)
with open('data/gemini_catalog.json', 'w', encoding='utf-8') as f:
    json.dump(products, f, ensure_ascii=False, indent=2)
print()
print('Saved: data/gemini_catalog.json')
