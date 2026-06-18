"""Re-run OCR on a receipt and update DB items."""
import sys
sys.path.insert(0, ".")
from pathlib import Path
from app.database import SessionLocal
from app.models import Receipt, ReceiptItem
from app.services.ocr import analyze_receipt

MEDIA_ROOT = Path("media")

receipt_id = int(sys.argv[1]) if len(sys.argv) > 1 else 3

db = SessionLocal()
receipt = db.query(Receipt).get(receipt_id)
if not receipt:
    print(f"Receipt #{receipt_id} not found")
    sys.exit(1)

fpath = MEDIA_ROOT / receipt.file_path
print(f"Re-OCR: {fpath}")

result = analyze_receipt(str(fpath))
if not result:
    print("OCR failed")
    sys.exit(1)

print(f"amount: {result['amount']}")
print(f"items: {len(result['items'])}")
for it in result["items"]:
    print(f"  {it}")

# Update DB
receipt.ocr_raw = result["raw"]
receipt.amount_detected = result["amount"]
receipt.ocr_status = "processed"

db.query(ReceiptItem).filter(ReceiptItem.receipt_id == receipt_id).delete()
for item in result["items"]:
    db.add(ReceiptItem(
        receipt_id=receipt_id,
        name=item["name"],
        qty=item.get("qty"),
        unit_price=item.get("unit_price"),
        total_price=item["total_price"],
    ))

db.commit()
print(f"\nReceipt #{receipt_id} updated.")
