"""Check last receipts and their items in DB."""
import sys
sys.path.insert(0, ".")
from app.database import SessionLocal
from app.models import Receipt, ReceiptItem

db = SessionLocal()
receipts = db.query(Receipt).order_by(Receipt.id.desc()).limit(5).all()
for r in receipts:
    print(f"\n--- Receipt #{r.id} | status={r.ocr_status} | amount={r.amount_detected} ---")
    print(f"file: {r.file_path}")
    print(f"raw: {(r.ocr_raw or '')[:120]}")
    items = db.query(ReceiptItem).filter(ReceiptItem.receipt_id == r.id).all()
    for it in items:
        print(f"  item: name={repr(it.name)}  qty={it.qty}  unit_price={it.unit_price}  total={it.total_price}")
db.close()
