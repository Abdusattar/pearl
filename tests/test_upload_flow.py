"""
Тест загрузки квитанций через HTTP API.
Запускает 3 сценария, показывает что OCR распознал, ссылки для ручного confirm.

Сценарии:
  1. org_id=4 — Садик Сокулук
  2. org_id=5 — Садик Кожомкул
  3. org_id=3 — Оба садика (split)

Запуск: python tests/test_upload_flow.py
Приложение должно быть на localhost:8001.
"""
import re
import sys
from pathlib import Path

import requests

BASE = "http://localhost:8001"
MEDIA = Path(__file__).parent.parent / "media" / "receipts"

# org_id=4 Сокулук, 5 Кожомкул, 3 Оба садика
SCENARIOS = [
    ("Садик Сокулук",       4),
    ("Садик Кожомкул",      5),
    ("Оба садика (split)",  3),
]


def get_images(n: int) -> list[Path]:
    imgs = sorted(p for p in MEDIA.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if len(imgs) < n:
        print(f"[!] Только {len(imgs)} квитанций, нужно {n}")
        sys.exit(1)
    return imgs[:n]


def upload(img: Path, receipt_org_id: int) -> tuple[int | None, str]:
    """POST /expenses/upload. Возвращает (receipt_id, confirm_url)."""
    with open(img, "rb") as f:
        resp = requests.post(
            f"{BASE}/expenses/upload",
            data={"org_id": receipt_org_id, "receipt_org_id": receipt_org_id},
            files={"file": (img.name, f, "image/jpeg")},
            allow_redirects=True,
            timeout=120,
        )
    m = re.search(r"/expenses/(\d+)/confirm", resp.url)
    if not m:
        return None, resp.url
    rid = int(m.group(1))
    return rid, resp.url


def parse_ocr(html: str) -> dict:
    """Вытаскивает из HTML confirm-страницы: amount + позиции."""
    amount = None
    # ищем value атрибута поля amount
    m = re.search(r'name="amount"[^>]*value="([^"]*)"', html)
    if m:
        amount = m.group(1)

    names   = re.findall(r'name="item_name"[^>]*value="([^"]*)"', html)
    totals  = re.findall(r'name="item_total_price"[^>]*value="([^"]*)"', html)
    qtys    = re.findall(r'name="item_qty"[^>]*value="([^"]*)"', html)
    items = list(zip(names, qtys, totals))

    return {"amount": amount, "items": items}


def run():
    images = get_images(len(SCENARIOS))
    print(f"Сервер: {BASE}\n")

    for (label, org_id), img in zip(SCENARIOS, images):
        sep = "=" * 62
        print(sep)
        print(f"  {label}  |  org_id={org_id}  |  {img.name}")
        print(sep)

        rid, url = upload(img, org_id)
        if rid is None:
            print(f"  ОШИБКА: нет /confirm в редиректе. URL: {url}\n")
            continue

        print(f"  receipt_id : {rid}")

        conf_resp = requests.get(
            f"{BASE}/expenses/{rid}/confirm?org_id={org_id}", timeout=30
        )
        ocr = parse_ocr(conf_resp.text)

        print(f"  Сумма OCR  : {ocr['amount'] or '(пусто)'}")
        if ocr["items"]:
            print(f"  Позиций    : {len(ocr['items'])}")
            for name, qty, total in ocr["items"][:12]:
                print(f"    • {name:<40} qty={qty or '—'}  итого={total}")
        else:
            print("  Позиций    : не найдено (или OCR вернул пустой результат)")

        print(f"\n  Подтвердить: {BASE}/expenses/{rid}/confirm?org_id={org_id}")
        print()

    print("Готово. Открой ссылки выше и проверь OCR вручную.")


if __name__ == "__main__":
    run()
