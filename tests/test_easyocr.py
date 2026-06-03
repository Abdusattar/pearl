"""Test EasyOCR (no LLM) on a receipt."""
import easyocr
import sys

path = r"media/receipts/5242699173746906974.jpg"
reader = easyocr.Reader(["ru", "en"], gpu=False)
results = reader.readtext(path)
for bbox, text, confidence in results:
    print(f"{confidence:.2f}  {text}")
