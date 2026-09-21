"""Копия всех таблиц боевой базы в CSV (COPY TO STDOUT) + счёт строк.

Нужна, когда локальный pg_dump старше сервера (16 против 18 на Railway).
Схема восстанавливается миграциями, данные — COPY FROM этих файлов.
Запуск: python scripts/backup_prod_tables.py <DATABASE_URL> <папка>
"""
import json
import sys
from pathlib import Path

import psycopg2

url, out = sys.argv[1], Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
conn = psycopg2.connect(url)
conn.set_session(readonly=True)
cur = conn.cursor()
cur.execute("select tablename from pg_tables where schemaname='public' order by tablename")
tables = [r[0] for r in cur.fetchall()]
counts = {}
for t in tables:
    with open(out / f"{t}.csv", "w", encoding="utf-8", newline="") as f:
        cur.copy_expert(f'COPY public."{t}" TO STDOUT WITH CSV HEADER', f)
    cur.execute(f'select count(*) from public."{t}"')
    counts[t] = cur.fetchone()[0]
cur.execute("select sequence_name from information_schema.sequences where sequence_schema='public'")
seqs = {}
for (s,) in cur.fetchall():
    cur.execute(f'select last_value from public."{s}"')
    seqs[s] = cur.fetchone()[0]
(out / "_counts.json").write_text(json.dumps({"tables": counts, "sequences": seqs}, ensure_ascii=False, indent=1), encoding="utf-8")
print(len(tables), "таблиц,", sum(counts.values()), "строк")
