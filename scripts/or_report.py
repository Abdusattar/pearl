"""
Отчёт по расходам OpenRouter за сегодня (и за всё время).
Запуск: python scripts/or_report.py
"""
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

LOG = Path(__file__).parent.parent / "logs" / "openrouter_usage.jsonl"

BUDGET_DAILY_USD  = 0.50   # порог предупреждения в день
BUDGET_TOTAL_USD  = 10.00  # порог предупреждения за всё время


def load():
    if not LOG.exists():
        return []
    with open(LOG, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def report():
    rows = load()
    if not rows:
        print("Лог пустой — OCR ещё не вызывался.")
        return

    today = date.today().isoformat()
    by_day = defaultdict(list)
    for r in rows:
        day = r["ts"][:10]
        by_day[day].append(r)

    today_rows = by_day.get(today, [])

    def summary(items):
        calls   = len(items)
        ok      = sum(1 for r in items if r.get("ok"))
        t_in    = sum(r.get("tokens_in", 0) for r in items)
        t_out   = sum(r.get("tokens_out", 0) for r in items)
        cost    = sum(r.get("cost_usd", 0) for r in items)
        return calls, ok, t_in, t_out, cost

    c, ok, ti, to, cost = summary(today_rows)
    tc, tok, tti, tto, tcost = summary(rows)

    print(f"═══ OpenRouter Usage Report ═══")
    print(f"  Дата: {today}")
    print()
    print(f"  Сегодня:")
    print(f"    Вызовов: {c}  (успешных: {ok})")
    print(f"    Токены:  {ti:,} in / {to:,} out")
    print(f"    Расход:  ${cost:.4f}")
    if cost >= BUDGET_DAILY_USD:
        print(f"    ⚠️  ПРЕВЫШЕН дневной порог ${BUDGET_DAILY_USD}")

    print()
    print(f"  За всё время:")
    print(f"    Вызовов: {tc}")
    print(f"    Токены:  {tti:,} in / {tto:,} out")
    print(f"    Расход:  ${tcost:.4f}")
    if tcost >= BUDGET_TOTAL_USD:
        print(f"    ⚠️  ПРЕВЫШЕН общий порог ${BUDGET_TOTAL_USD}")

    print()
    print(f"  По дням:")
    for day in sorted(by_day)[-7:]:
        _, _, _, _, dcost = summary(by_day[day])
        bar = "█" * max(1, int(dcost / 0.01))
        flag = " ⚠️" if dcost >= BUDGET_DAILY_USD else ""
        print(f"    {day}  ${dcost:.4f}  {bar}{flag}")


if __name__ == "__main__":
    report()
