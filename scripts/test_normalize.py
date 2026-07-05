import sys
sys.stdout.reconfigure(encoding='utf-8')
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import SessionLocal
from app.services.normalize import normalize_items

db = SessionLocal()
test_items = [
    {'name': 'землячна',    'qty': None, 'unit_price': None, 'total_price': 6120},
    {'name': 'жум',         'qty': 24,   'unit_price': 340,  'total_price': 8160},
    {'name': 'такива',      'qty': None, 'unit_price': None, 'total_price': 360},
    {'name': 'доасоль',     'qty': 2,    'unit_price': 35,   'total_price': 70},
    {'name': 'чечев',       'qty': 5,    'unit_price': 180,  'total_price': 900},
    {'name': 'горох',       'qty': 10,   'unit_price': 60,   'total_price': 600},
    {'name': 'гречко',      'qty': 3,    'unit_price': 120,  'total_price': 360},
    {'name': 'сабиз',       'qty': 10,   'unit_price': 25,   'total_price': 250},
    {'name': 'чел гром 500','qty': 5,    'unit_price': 410,  'total_price': 2050},
    {'name': 'береке',      'qty': 2,    'unit_price': 180,  'total_price': 360},
    {'name': 'свекло',      'qty': 5,    'unit_price': 30,   'total_price': 150},
    {'name': 'и уксус 80%', 'qty': 1,    'unit_price': 90,   'total_price': 90},
]

result = normalize_items(db, test_items)
print(f"{'OCR':<22} {'Найдено':<28} Тип")
print('-' * 65)
for r in result:
    found = r.get('matched_name') or 'НЕ НАЙДЕНО'
    print(f"  {r['name']:<20} -> {found:<28} [{r['match_type']}]")
db.close()
