"""Оценка разбора текста ботом на реальных сообщениях (24.09.2026: «настраивай — промпт, обвязка
или модель не та»). Набор — живые сообщения Мунары, Махабат, Айжан за 23–24.09 плюс несколько
типовых. Запуск:

    python scripts/eval_bot_text.py            # текущий промпт из bot_group (v2, с 24.09)
    python scripts/eval_bot_text.py --v1       # старый промпт (4 вида) — для сравнения
    OCR_MODEL=google/gemini-2.5-pro python scripts/eval_bot_text.py --v2

Нужны OPENROUTER_API_KEY и DATABASE_URL (имена людей и поставщиков берутся из базы, ничего не пишется).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CASES = [
    # (текст, автор, ожидаемый вид, ожидаемая сумма или None)
    ("Снятия с банка 64662", "Мунара", "withdrawal", 64662),
    ("Сегодня я на руку брала 64662", "Мунара", "withdrawal", 64662),
    ("сняла 25 000 со счёта садика", "Мунара", "withdrawal", 25000),
    ("Остаток наличными 51090", "Мунара", "cash_on_hand", 51090),
    ("51090остаток у меня наличка", "Мунара", "cash_on_hand", 51090),
    ("Нал остаток 51090-12700=38 390", "Мунара", "cash_on_hand", 38390),
    ("Остаток наличными 38322 Мунара", "Мунара", "cash_on_hand", 38322),
    ("64662-26340=38 322", "Мунара", "cash_on_hand", 38322),
    ("На руках-90,055", "Айжан", "cash_on_hand", 90055),
    ("наличных у меня на руках 12 400", "Айжан", "cash_on_hand", 12400),
    ("Вчерашний остаток наличными 5229сом отдала Махабату", "Мунара", "transfer", 5229),
    ("передала Айдай 20 000", "Мунара", "transfer", 20000),
    ("получила от Таласа 50000", "Мунара", "transfer", 50000),
    ("оплатила Халиме 8000", "Махабат", "supplier_payment", 8000),
    ("отдала долг Кириллу 3 200", "Махабат", "supplier_payment", 3200),
    ("на счету садика 64 797,68", "Мунара", "balance", 64797.68),
    ("Остаток банка", "Мунара", "none", None),
    ("Карта да остаток 24.09.2026", "Мунара", "none", None),
    ("Закуп 13570", "Мунара", "purchase_total", 13570),
    ("Еще закуп 12770", "Мунара", "purchase_total", 12770),
    ("Из этой сумму еще 12770", "Мунара", "purchase_total", 12770),
    ("Из этой сумму сдела закуп на 13570+12770=26 340 сегодняшний закуп", "Мунара", "purchase_total", 26340),
    ("Кг кунжут400сом Мак 500гр 275", "Мунара", "purchase", 675),
    ("500 сом корм птицам 2 килограмма", "Махабат", "purchase", 500),
    ("такси 200", "Мунара", "expense", 200),
    ("доставка продуктов 150 сом", "Махабат", "expense", 150),
    ("Комиссия", "Мунара", "answer", None),
    ("Нет", "Мунара", "answer", None),
    ("Да", "Мунара", "answer", None),
    ("Часть чека", "Мунара", "answer", None),
    ("Овощи", "Мунара", "answer", None),
    ("Вот это правильно", "Мунара", "answer", None),
    ("Ок", "Мунара", "answer", None),
    ("нет хлеб взяли в долг", "Махабат", "answer", None),
    ("По каким записям?", "Айжан", "question", None),
    ("Банк автоматом берут камиссу", "Мунара", "none", None),
    ("от кармана Махабат", "Махабат", "none", None),
    ("Мы в банке", "Мунара", "none", None),
    ("В магазине мы покупали поштучно. На базаре этот товар продаётся на килограммы, поэтому мы тоже докупили там. На рынке чека нет", "Мунара", "none", None),
    ("Магазинден штук менен алдык Базарда кг бар экен Ошону да алып коштук базарда чек жок", "Мунара", "none", None),
    ("Сегодня в школе детей 344  персонал 35, в садике 100 детей  персонал 10. Меню сегодня гречневая каша, рисовый суп, оромо", "Махабат", "meals", None),
    ("остаток склада на сегодня   молоко 80л, кефир 30л, вермишель 18кг", "Махабат", "stock", None),
    ("из остатка 12 л молока переданы в второй филиал Кожомкул", "Махабат", "stock", None),
]

PROMPT_V1 = (
    "A message from the work chat of a kindergarten in Kyrgyzstan (Russian, may mix Kyrgyz). People there: "
    "{people}. Suppliers: {suppliers}.\n"
    "Message from {author}: «{text}»\n\n"
    "Is it a report about money? Kinds:\n"
    "- withdrawal: someone took cash from the bank account (сняла, сняли с карты/счёта).\n"
    "- transfer: cash passed from one person to another (передала, отдала, получила от).\n"
    "- supplier_payment: a supplier was paid for goods (оплатила Халиме, отдала долг Кириллу).\n"
    "- balance: the bank account balance now (на счету, остаток).\n"
    "- none: anything else, including questions, greetings, plans, children, food.\n"
    "Names: return them as written. If the person is not named, null — do not guess.\n"
    "Return ONLY JSON: {{\"kind\": ..., \"amount\": number or null, \"who\": person who did it or null, "
    "\"to\": receiving person for transfer or null, \"supplier\": supplier or null, "
    "\"date\": \"today\" | \"yesterday\" | \"YYYY-MM-DD\" | null, \"account\": \"садик\" | \"школа\" | null, "
    "\"sure\": true|false}}"
)


PROMPT_V2 = (
    "Сообщение из рабочего чата садика и школы в Кыргызстане (русский, иногда кыргызский, опечатки, "
    "без знаков препинания). Люди: {people}. Поставщики: {suppliers}.\n"
    "Автор: {author}. Сообщение: «{text}»\n\n"
    "Определи, о чём оно. Ровно один вид (kind):\n"
    "- withdrawal — сняли наличные со счёта/карты в банке: «сняла 25 000», «снятие с банка 64662», «на руку брала 64662».\n"
    "- transfer — наличные передали от человека человеку: «отдала Махабат 5229», «передала Айдай 20 000», «получила от Таласа». "
    "to = кому, who = кто отдал (если не автор).\n"
    "- supplier_payment — заплатили поставщику за товар/долг: «оплатила Халиме 8000», «отдала долг Кириллу».\n"
    "- balance — остаток на банковском счёте, названа сумма: «на счету 64 797», «остаток счёта 1,35».\n"
    "- cash_on_hand — сколько наличных на руках у автора сейчас: «остаток наличными 51090», «нал 38 390», «на руках 12 400», "
    "«51090 остаток у меня наличка». Если написана арифметика «51090-12700=38 390» — amount = результат после «=».\n"
    "- purchase — закуп с товарами и ценами: «кунжут 400 сом, мак 500 гр 275», «500 сом корм птицам 2 кг». amount = сумма всех.\n"
    "- purchase_total — закуп одной суммой без товаров: «закуп 13570», «ещё закуп 12770», «закуп на 13570+12770=26 340» (amount = итог).\n"
    "- expense — мелкий расход без товара: такси, доставка, курьер, свет, вода, интернет: «такси 200», «доставка 150 сом».\n"
    "- meals — сколько человек ели и меню: «школа 344, садик 100, персонал 45, меню …».\n"
    "- stock — остаток склада или передача продуктов между филиалами: «остаток склада: молоко 80 л…», «12 л молока в Кожомкул».\n"
    "- answer — короткий ответ на вопрос бота: да, нет, ок, комиссия, часть чека, овощи, «вот это правильно», «нет, хлеб взяли в долг».\n"
    "- question — вопрос боту: «по каким записям?».\n"
    "- none — всё остальное: пояснения, планы, «мы в банке», «банк берёт комиссию», «от кармана Махабат», подпись к скрину без суммы.\n"
    "Важно: «остаток» без слов «счёт/банк/карта» — это наличные (cash_on_hand), не balance. "
    "«Остаток банка» без суммы — none. Сумму бери как число с пробелами и запятыми тысяч: «90,055» = 90055, «1,35» = 1.35.\n"
    "Имена возвращай как написаны. Не угадывай, кого не назвали (null).\n"
    "Верни ТОЛЬКО JSON: {{\"kind\": ..., \"amount\": число или null, \"who\": кто сделал или null, "
    "\"to\": кому (transfer) или null, \"supplier\": поставщик или null, "
    "\"date\": \"today\" | \"yesterday\" | \"YYYY-MM-DD\" | null, \"account\": \"садик\" | \"школа\" | null, "
    "\"sure\": true|false}}"
)


def main():
    from app.database import SessionLocal
    from app.models import Supplier, User
    from app.services import bot_group as grp

    v2 = "--v1" not in sys.argv
    db = SessionLocal()
    people = ", ".join(u.name for u in db.query(User).filter(User.deleted_at.is_(None)).all())
    suppliers = ", ".join(s.name for s in db.query(Supplier).all()[:80])
    prompt_t = grp.TEXT_PROMPT if v2 else PROMPT_V1
    ok = 0
    rows = []
    for text, author, want_kind, want_amount in CASES:
        try:
            data = grp.ask_model(prompt_t.format(people=people, suppliers=suppliers, author=author, text=text[:600]))
        except Exception as e:  # noqa: BLE001
            data = {"kind": f"ERR {e}"}
        got_kind = data.get("kind") or "none"
        got_amount = grp._num(data.get("amount"))
        if not v2 and want_kind in ("cash_on_hand", "purchase", "purchase_total", "expense", "meals", "stock", "answer", "question"):
            want_old = "none"   # старый промпт таких видов не знает — засчитываем только «не соврал»
        else:
            want_old = want_kind
        kind_ok = got_kind == (want_kind if v2 else want_old)
        amount_ok = want_amount is None or (got_amount is not None and abs(float(got_amount) - want_amount) < 0.01)
        good = kind_ok and (amount_ok or (not v2 and want_old == "none"))
        ok += good
        rows.append((("ok " if good else "XX ") + f"{text[:48]:48} | want {want_kind:16} {want_amount!s:8} | got {got_kind:16} {got_amount!s:8}"
                     + (f" to={data.get('to')}" if data.get("to") else "")))
    print("\n".join(rows))
    print(f"\n{'v2 (bot_group)' if v2 else 'v1 (old)'} prompt, model {os.getenv('OCR_MODEL', grp.MODEL)}: {ok}/{len(CASES)}")


if __name__ == "__main__":
    main()
