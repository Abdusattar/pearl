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
from pathlib import Path
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs
from app.models import Student, Transaction, Enrollment, Group, Organization, OptimaLog

router = APIRouter(prefix="/optima", tags=["optima"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
log = logging.getLogger(__name__)

# Лог попыток видит только системный интегратор — Абдусаттар, не по роли
# (13.07). Не PILOT_USER_IDS/PRICE_EDITORS — это техническая диагностика,
# не бизнес-функция, остальным пилот-пользователям она не нужна.
LOG_VIEWERS = {1}

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

    def respond(result: int, comment: str = "", prv_txn: str = "") -> Response:
        try:
            db.add(OptimaLog(
                command=command, account=account, txn_id=txn_id, sum=sum,
                result_code=result, comment=comment, client_ip=client_ip,
            ))
            db.commit()
        except Exception:
            db.rollback()
        return _xml(txn_id, result, comment, prv_txn=prv_txn, sum_val=sum)

    # Валидация PIN
    if not PIN_RE.match(account):
        log.warning("Optima bad PIN format: %r", account)
        return respond(ERR_BAD_FORMAT, "Неверный формат PIN")

    # Поиск ребёнка
    student = db.query(Student).filter(Student.pin == account).first()

    if not student:
        return respond(ERR_NOT_FOUND, "PIN не найден")

    if student.status != "active":
        return respond(ERR_INACTIVE, "Ребёнок выбыл")

    # ── CHECK ────────────────────────────────────────────────────────────────
    if command == "check":
        group = (
            db.query(Group.name)
            .join(Enrollment, Enrollment.group_id == Group.id)
            .filter(Enrollment.student_id == student.id, Enrollment.end_date.is_(None))
            .scalar()
        )
        org = db.get(Organization, student.organization_id)
        org_label = f"{org.name} Жемчужина" if org else None
        parts = [student.name]
        if org_label:
            parts.append(org_label)
        if group:
            parts.append(f"группа {group}")
        comment = ", ".join(parts)
        return respond(OK, comment)

    # ── PAY ──────────────────────────────────────────────────────────────────
    if command == "pay":
        # Идемпотентность — одна и та же txn_id должна давать тот же ответ
        existing = db.query(Transaction).filter(
            Transaction.external_txn_id == txn_id
        ).first()
        if existing:
            log.info("Optima duplicate txn_id=%s → prv_txn=%s", txn_id, existing.id)
            return respond(OK, "OK", prv_txn=str(existing.id))

        # Парсим сумму
        try:
            amount = Decimal(sum)
            if amount <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            return respond(ERR_OTHER, "Неверная сумма")

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
            return respond(OK, "OK", prv_txn=str(txn.id))

        except Exception as e:
            db.rollback()
            log.error("Optima pay DB error txn_id=%s: %s", txn_id, e)
            return respond(ERR_TEMP, "Временная ошибка")

    # Неизвестная команда
    log.warning("Optima unknown command: %r", command)
    return respond(ERR_OTHER, f"Неизвестная команда: {command}")


@router.get("/log", response_class=HTMLResponse)
def optima_log(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.id not in LOG_VIEWERS:
        return RedirectResponse("/", status_code=302)

    rows = db.query(OptimaLog).order_by(OptimaLog.created_at.desc()).limit(200).all()
    accessible = get_accessible_orgs(user, db)

    return templates.TemplateResponse("optima/log.html", {
        "request": request,
        "current_user": user,
        "rows": rows,
        "accessible_orgs": accessible,
        "current_org_id": accessible[0].id if accessible else None,
        "active_page": "optima_log",
    })
