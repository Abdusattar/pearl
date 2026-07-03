"""
Эндпоинт для интеграции с Optima (протокол QIWI поставщика услуг).
Optima сама шлёт GET-запросы сюда при каждом платеже через терминал/приложение.

Поток:
  1. command=check  → проверить PIN, вернуть ФИО ребёнка и группу/класс
  2. command=pay    → записать платёж в БД, вернуть наш ID транзакции

Доступ: только с IP Optima (79.142.16.0/20) — настроить на уровне nginx/firewall при деплое.
"""
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Student, Transaction, Enrollment, Group

router = APIRouter(prefix="/optima", tags=["optima"])
log = logging.getLogger(__name__)

PIN_RE = re.compile(r'^\d{4}$')

# Коды завершения согласно протоколу (Приложение А)
OK              = 0
ERR_TEMP        = 1    # нефатальная — Optima повторит
ERR_BAD_FORMAT  = 4    # фатальная — неверный формат PIN
ERR_NOT_FOUND   = 5    # фатальная — PIN не найден
ERR_INACTIVE    = 79   # фатальная — ребёнок выбыл
ERR_OTHER       = 300  # фатальная — прочая ошибка поставщика


def _xml(osmp_txn_id: str, result: int, comment: str = "",
         prv_txn: str = "", sum_val: str = "") -> Response:
    body = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<response>\n'
        f'    <osmp_txn_id>{osmp_txn_id}</osmp_txn_id>\n'
        f'    <prv_txn>{prv_txn}</prv_txn>\n'
        f'    <sum>{sum_val}</sum>\n'
        f'    <result>{result}</result>\n'
        f'    <comment>{comment}</comment>\n'
        f'</response>'
    )
    return Response(content=body, media_type="application/xml; charset=utf-8")


@router.get("/payment")
def optima_payment(
    request: Request,
    command: str = "",
    account: str = "",
    txn_id: str = "",
    sum: str = "0",
    txn_date: str = "",
    db: Session = Depends(get_db),
):
    client_ip = request.client.host if request.client else "unknown"
    log.info("Optima %s account=%s txn_id=%s sum=%s ip=%s", command, account, txn_id, sum, client_ip)

    # Валидация PIN
    if not PIN_RE.match(account):
        log.warning("Optima bad PIN format: %r", account)
        return _xml(txn_id, ERR_BAD_FORMAT, "Неверный формат PIN", sum_val=sum)

    # Поиск ребёнка
    student = db.query(Student).filter(Student.pin == account).first()

    if not student:
        return _xml(txn_id, ERR_NOT_FOUND, "PIN не найден", sum_val=sum)

    if student.status != "active":
        return _xml(txn_id, ERR_INACTIVE, "Ребёнок выбыл", sum_val=sum)

    # ── CHECK ────────────────────────────────────────────────────────────────
    if command == "check":
        group = (
            db.query(Group.name)
            .join(Enrollment, Enrollment.group_id == Group.id)
            .filter(Enrollment.student_id == student.id, Enrollment.end_date.is_(None))
            .scalar()
        )
        comment = f"{student.name} — {group}" if group else student.name
        return _xml(txn_id, OK, comment, sum_val=sum)

    # ── PAY ──────────────────────────────────────────────────────────────────
    if command == "pay":
        # Идемпотентность — одна и та же txn_id должна давать тот же ответ
        existing = db.query(Transaction).filter(
            Transaction.external_txn_id == txn_id
        ).first()
        if existing:
            log.info("Optima duplicate txn_id=%s → prv_txn=%s", txn_id, existing.id)
            return _xml(txn_id, OK, "OK", prv_txn=str(existing.id), sum_val=sum)

        # Парсим сумму
        try:
            amount = Decimal(sum)
            if amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            return _xml(txn_id, ERR_OTHER, "Неверная сумма", sum_val=sum)

        # Парсим дату (YYYYMMDDHHMMSS → date)
        pay_date = date.today()
        if txn_date and len(txn_date) >= 8:
            try:
                pay_date = datetime.strptime(txn_date[:8], "%Y%m%d").date()
            except ValueError:
                pass

        # Записываем платёж
        try:
            txn = Transaction(
                organization_id=student.organization_id,
                type="income",
                amount=amount,
                student_id=student.id,
                description=f"Optima — {student.name}",
                date=pay_date,
                external_txn_id=txn_id,
            )
            db.add(txn)
            db.commit()
            db.refresh(txn)
            log.info("Optima pay OK txn_id=%s prv_txn=%s student=%s amount=%s",
                     txn_id, txn.id, student.name, amount)
            return _xml(txn_id, OK, "OK", prv_txn=str(txn.id), sum_val=sum)

        except Exception as e:
            db.rollback()
            log.error("Optima pay DB error txn_id=%s: %s", txn_id, e)
            return _xml(txn_id, ERR_TEMP, "Временная ошибка", sum_val=sum)

    # Неизвестная команда
    log.warning("Optima unknown command: %r", command)
    return _xml(txn_id, ERR_OTHER, f"Неизвестная команда: {command}", sum_val=sum)
