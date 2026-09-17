"""Одна отправка формы — одна запись (17.09).

Номер формы ставит страница (`new/base.html`). Обработчик перед записью зовёт
`done_url`: лок по номеру держится до commit, так что второй такой же запрос
ждёт первого и видит, что всё уже записано. После записи — `remember` в той же
транзакции. Между `done_url` и commit обработчик не должен делать commit, иначе
лок отпустится раньше времени.
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.models import FormSubmission
from app.services.dedup_guard import acquire_submission_lock

_TOKEN = re.compile(r"^[A-Za-z0-9-]{16,64}$")


def clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value if _TOKEN.match(value) else None


def done_url(db: Session, token: str | None) -> str | None:
    """Куда вести, если форма с этим номером уже записана; None — писать."""
    if not token:
        return None
    acquire_submission_lock(db, "form", token)
    row = db.get(FormSubmission, token)
    return row.result_url if row else None


def remember(db: Session, token: str | None, user_id: int, url: str) -> None:
    if token:
        db.add(FormSubmission(token=token, user_id=user_id, result_url=url))
