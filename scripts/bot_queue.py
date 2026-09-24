"""Очередь бота в режиме надзора (владелец 24.09: «бот отправляет тебе, что хочет сказать
людям, ты говоришь ок — он отправляет»).

Пока в Настройках bot_paused = true, всё, что бот хотел написать людям, лежит в bot_messages
со статусом «paused». Этот скрипт показывает очередь, выпускает сообщение (как есть или с
поправленным текстом) или гасит его.

    python scripts/bot_queue.py list
    python scripts/bot_queue.py send 233
    python scripts/bot_queue.py send 233 --text "Мунара, на руках 38 322? По записям 38 322. Да / нет"
    python scripts/bot_queue.py skip 233 225 221

Нужны DATABASE_URL (прод) и TELEGRAM_TOKEN в окружении.
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx
import psycopg2


def _db():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def cmd_list(args):
    with _db() as c, c.cursor() as cur:
        cur.execute("""select m.id, m.created_at::time(0), m.kind, m.chat_id, coalesce(u.name, '—'), m.text
                       from bot_messages m left join users u on u.id = m.user_id
                       where m.status = 'paused' order by m.id""")
        rows = cur.fetchall()
    if not rows:
        print("очередь пуста")
    for mid, t, kind, chat, who, text in rows:
        where = "группа" if chat and chat < 0 else who
        print(f"#{mid} {t} [{kind}] → {where}: {text.replace(chr(10), ' ⏎ ')[:300]}")


def cmd_send(args):
    token = os.environ["TELEGRAM_TOKEN"]
    with _db() as c, c.cursor() as cur:
        cur.execute("select chat_id, text, payload from bot_messages where id = %s and status = 'paused'", (args.id,))
        row = cur.fetchone()
        if row is None:
            sys.exit(f"#{args.id}: нет в очереди")
        chat_id, text, payload = row
        text = args.text or text
        body = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if (payload or {}).get("reply_to"):
            body["reply_parameters"] = {"message_id": payload["reply_to"], "allow_sending_without_reply": True}
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", json=body, timeout=20)
        ok = r.status_code == 200
        cur.execute("update bot_messages set status = %s, text = %s where id = %s",
                    ("sent" if ok else "failed", text, args.id))
        print(f"#{args.id}: {'отправлено' if ok else 'ошибка ' + r.text[:200]}")


def cmd_skip(args):
    with _db() as c, c.cursor() as cur:
        cur.execute("update bot_messages set status = 'skipped' where id = any(%s) and status = 'paused'", (args.ids,))
        print(f"погашено: {cur.rowcount}")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    s = sub.add_parser("send")
    s.add_argument("id", type=int)
    s.add_argument("--text")
    s.set_defaults(fn=cmd_send)
    k = sub.add_parser("skip")
    k.add_argument("ids", type=int, nargs="+")
    k.set_defaults(fn=cmd_skip)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
