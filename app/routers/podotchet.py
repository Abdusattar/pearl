from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_accessible_orgs, get_current_user, resolve_org
from app.models import (
    CapitalWithdrawal, CashFunding, Organization, Reconciliation, Supplier, User,
)
from app.services import podotchet, reconciliation, supplier_ledger
from app.services.dedup_guard import acquire_submission_lock
from app.services.warehouse import get_inventory_summary

router = APIRouter(prefix="/podotchet", tags=["podotchet"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Доступ: владельцы + те, кто реально держит деньги на руках (Мунара сейчас,
# Махабат с сентября) — шире, чем /dashboard (owner/founder/staff), потому что
# Мунара по факту role=manager, не staff (25.08).
ALLOWED_ROLES = ("owner", "founder", "staff", "manager")


def _guard(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=302)
    if user.role not in ALLOWED_ROLES:
        return None, RedirectResponse("/", status_code=302)
    return user, None


def _business_orgs(db: Session) -> list[Organization]:
    """Все реальные бизнесы (Школа/Сокулук/Кожомкул), не только доступные
    текущему пользователю — на пополнении нужно указать чей это подотчёт даже
    если сам пользователь ограничен пилотом одного объекта (25.08)."""
    all_orgs = db.query(Organization).all()
    has_children = {o.parent_id for o in all_orgs if o.parent_id is not None}
    return [o for o in all_orgs if o.id not in has_children]


def _founders(db: Session) -> list[User]:
    return db.query(User).filter(User.role == "founder", User.deleted_at.is_(None)).order_by(User.name).all()


def _resolve_founder(db: Session, founder_id_str: str) -> User | None:
    if not founder_id_str or not founder_id_str.isdigit():
        return None
    founder_ids = {u.id for u in _founders(db)}
    if int(founder_id_str) not in founder_ids:
        return None
    return db.query(User).filter(User.id == int(founder_id_str)).first()


def _resolve_business_org(db: Session, org_id_str: str) -> Organization | None:
    """org_id страницы должен быть реальным бизнесом (лист дерева), не
    родительским узлом («Жемчужина», «Садики») — иначе снятие уходит в
    подотчёт, который ни один расход никогда не сможет списать (owner/founder
    видят родительские узлы в org-select наверху, могут выбрать по ошибке)."""
    if not org_id_str or not org_id_str.isdigit():
        return None
    business_ids = {o.id for o in _business_orgs(db)}
    if int(org_id_str) not in business_ids:
        return None
    return db.query(Organization).filter(Organization.id == int(org_id_str)).first()


def _resolve_holder(db: Session, org: Organization) -> User | None:
    """Единственный держатель кассы объекта (Organization.cash_recipient_user_id,
    настраивается на /settings/) — с 04.09 подотчёт больше не даёт выбрать
    отчитывающегося свободно: ровно один держатель на бизнес убирает саму
    возможность одновременно открытых бакетов у двух человек, из-за которой
    FIFO по подотчёту не может точно понять, чьими деньгами оплачен расход."""
    if not org.cash_recipient_user_id:
        return None
    return db.query(User).filter(
        User.id == org.cash_recipient_user_id, User.deleted_at.is_(None)
    ).first()


def _parse_amount(raw: str) -> float:
    return float((raw or "0").replace(" ", "").replace(",", "."))


def _parse_date(raw: str):
    return datetime.strptime(raw, "%Y-%m-%d").date()


@router.get("/", response_class=HTMLResponse)
def podotchet_page(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)
    if not current_org:
        return RedirectResponse("/", status_code=302)

    business_orgs = _business_orgs(db)
    business_ids = {o.id for o in business_orgs}
    if current_org.id not in business_ids:
        # Выбран родительский узел («Жемчужина», «Садики») — подотчёт всегда
        # про конкретный объект, переключаем на ближайший реальный бизнес,
        # а не показываем пустую/бессмысленную страницу.
        fallback = next((o for o in accessible if o.id in business_ids), None) \
            or (business_orgs[0] if business_orgs else None)
        if fallback:
            return RedirectResponse(f"/podotchet/?org_id={fallback.id}", status_code=302)

    today = date.today()
    expected = podotchet.get_expected_balance(db, current_org.id, today)
    cash_state = podotchet.get_cash_state(db, current_org.id)
    ledger = list(cash_state["buckets"])
    ledger.sort(key=lambda b: (b["date"], b["id"]), reverse=True)
    holder = _resolve_holder(db, current_org)
    inventory = get_inventory_summary(db, {current_org.id})

    users_by_id = {u.id: u.name for u in db.query(User).all()}
    orgs_by_id = {o.id: o.name for o in db.query(Organization).all()}

    balances = podotchet.get_balances_by_person(db, current_org.id)
    person_cards = [
        {"name": users_by_id.get(uid, "?"), "amount": amt}
        for uid, amt in sorted(balances.items(), key=lambda kv: -kv[1])
    ]

    since = expected["since"] or date(2026, 1, 1)
    category_spend = podotchet.get_spend_by_category(db, current_org.id, since, today)
    flows = podotchet.get_cross_org_flows(db, since, today)
    flow_rows = [
        {"from": orgs_by_id.get(f["from_org_id"], "?"), "to": orgs_by_id.get(f["to_org_id"], "?"), "amount": f["amount"]}
        for f in flows
    ]

    snapshots = reconciliation.history(db, current_org.id)
    # Долги поставщиков — общие для организации, не привязаны к объекту
    # (Supplier без organization_id), поэтому показываем весь список: сверка
    # долга нужна как раз чтобы завести суммы, которых в системе ещё нет.
    supplier_rows = [
        {
            "id": s.id,
            "name": s.name,
            "debt": supplier_ledger.get_supplier_balance(db, s.id),
        }
        for s in db.query(Supplier).order_by(Supplier.name).all()
    ]
    expected_cash = cash_state["net"]
    last_cash = reconciliation.latest(db, current_org.id, reconciliation.CASH)
    days_since_snapshot = (today - expected["since"]).days if expected["since"] else None

    ledger_rows = []
    for b in ledger:
        status_reported = b["amount"] - b["remaining"]
        ledger_rows.append({
            **b,
            "taken_by_name": users_by_id.get(b["taken_by"], "?"),
            "accountable_name": users_by_id.get(b["accountable_user_id"], "?"),
            "source_org_name": orgs_by_id.get(b["source_organization_id"]) if b["source_organization_id"] else None,
            "source_founder_name": users_by_id.get(b["source_founder_id"]) if b.get("source_founder_id") else None,
            "reported": status_reported,
            "fully_reported": b["remaining"] <= podotchet.DUST,
        })

    founders = _founders(db)
    founder_capital = [
        {**f, "name": users_by_id.get(f["founder_user_id"], "?")}
        for f in podotchet.get_founder_capital(db, current_org.id)
    ]
    capital_movements = [
        {**m, "founder_name": users_by_id.get(m["founder_user_id"], "?")}
        for m in podotchet.get_capital_movements(db, current_org.id)
    ]

    return templates.TemplateResponse("podotchet/index.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id,
        "current_org_name": current_org.name,
        "active_page": "podotchet",
        "business_orgs": business_orgs,
        "holder": holder,
        "inventory": inventory,
        "expected": expected,
        "person_cards": person_cards,
        "category_spend": category_spend,
        "flow_rows": flow_rows,
        "snapshots": snapshots,
        "supplier_rows": supplier_rows,
        "expected_cash": expected_cash,
        "last_cash": last_cash,
        "kind_labels": reconciliation.KIND_LABELS,
        # Остаток кассы берём из расчёта, а не суммой карточек людей: деньги,
        # пересчитанные на сверке, принадлежат объекту, и если держатель кассы
        # не назначен, карточки их не покажут, а в кассе они есть.
        "cash_on_hand": cash_state["on_hand"],
        "cash_baseline": cash_state["baseline"],
        "cash_baseline_remaining": cash_state["baseline_remaining"],
        "uncovered": max(Decimal("0"), -cash_state["net"]),
        "ledger_rows": ledger_rows,
        "founders": founders,
        "founder_capital": founder_capital,
        "capital_movements": capital_movements,
        "today": today.isoformat(),
        "days_since_snapshot": days_since_snapshot,
        "users_lookup": users_by_id,
        # Пороги «мелкого» расхождения отдаём в шаблон, а не дублируем в JS:
        # форма показывает требование объяснить разницу до отправки, и считать
        # она должна ровно то же, что потом проверит сервер (08.09).
        "small_delta_percent": float(reconciliation.SMALL_DELTA_PERCENT),
        "big_delta": float(reconciliation.BIG_DELTA),
    })


@router.post("/withdraw")
def create_withdrawal(
    request: Request,
    amount: str = Form(...),
    date_str: str = Form(..., alias="date"),
    comment: str = Form(default=""),
    org_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """Снять деньги — всегда для объекта, выбранного наверху страницы; кто
    физически снял не спрашивается отдельно — это тот, кто открыл форму.
    Отчитывается всегда единственный держатель кассы объекта (04.09,
    Organization.cash_recipient_user_id) — свободный выбор убрали, чтобы не
    получать одновременно двух держателей, для которых FIFO подотчёта не может
    точно понять, чьими деньгами оплачен расход."""
    user, redirect = _guard(request, db)
    if redirect:
        return redirect
    redirect_url = f"/podotchet/?org_id={org_id}"

    try:
        amount_val = _parse_amount(amount)
        date_val = _parse_date(date_str)
    except ValueError:
        return RedirectResponse(f"{redirect_url}&error=Неверная сумма или дата", status_code=303)
    if amount_val <= 0:
        return RedirectResponse(f"{redirect_url}&error=Сумма должна быть больше нуля", status_code=303)

    org = _resolve_business_org(db, org_id)
    if not org:
        return RedirectResponse(f"{redirect_url}&error=Выберите конкретный объект наверху страницы", status_code=303)

    holder = _resolve_holder(db, org)
    if not holder:
        msg = "Для этого объекта не задан держатель кассы — задайте в Настройках"
        return RedirectResponse(f"{redirect_url}&error={quote(msg)}", status_code=303)

    db.add(CashFunding(
        organization_id=org.id,
        source_type="withdrawal",
        amount=amount_val,
        date=date_val,
        taken_by=user.id,
        accountable_user_id=holder.id,
        comment=comment.strip() or None,
        created_by=user.id,
    ))
    db.commit()
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/borrow")
def create_borrow(
    request: Request,
    amount: str = Form(...),
    date_str: str = Form(..., alias="date"),
    source_organization_id: str = Form(...),
    comment: str = Form(default=""),
    org_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """Одолжили наличными у другого бизнеса — получатель всегда текущий
    объект страницы, «откуда» выбирается отдельно. Деньги никогда не были на
    счету получателя — это всегда direct_cash, не withdrawal. Отчитывается —
    держатель кассы объекта-получателя (см. create_withdrawal)."""
    user, redirect = _guard(request, db)
    if redirect:
        return redirect
    redirect_url = f"/podotchet/?org_id={org_id}"

    try:
        amount_val = _parse_amount(amount)
        date_val = _parse_date(date_str)
    except ValueError:
        return RedirectResponse(f"{redirect_url}&error=Неверная сумма или дата", status_code=303)
    if amount_val <= 0:
        return RedirectResponse(f"{redirect_url}&error=Сумма должна быть больше нуля", status_code=303)

    org = _resolve_business_org(db, org_id)
    if not org:
        return RedirectResponse(f"{redirect_url}&error=Выберите конкретный объект наверху страницы", status_code=303)

    src = _resolve_business_org(db, source_organization_id)
    if not src or src.id == org.id:
        return RedirectResponse(f"{redirect_url}&error=Укажите, у какого другого бизнеса одолжили", status_code=303)

    holder = _resolve_holder(db, org)
    if not holder:
        msg = "Для этого объекта не задан держатель кассы — задайте в Настройках"
        return RedirectResponse(f"{redirect_url}&error={quote(msg)}", status_code=303)

    db.add(CashFunding(
        organization_id=org.id,
        source_type="direct_cash",
        amount=amount_val,
        date=date_val,
        taken_by=user.id,
        accountable_user_id=holder.id,
        source_organization_id=src.id,
        comment=comment.strip() or None,
        created_by=user.id,
    ))
    db.commit()
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/founder-fund")
def create_founder_fund(
    request: Request,
    amount: str = Form(...),
    date_str: str = Form(..., alias="date"),
    founder_user_id: str = Form(...),
    comment: str = Form(default=""),
    org_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """Учредитель дал наличные в кассу — зеркало /borrow, только источник не
    другой бизнес, а конкретный учредитель. Деньги никогда не были на счету —
    всегда direct_cash, участвуют в обычном FIFO-пуле подотчёта. Отчитывается —
    держатель кассы объекта (см. create_withdrawal)."""
    user, redirect = _guard(request, db)
    if redirect:
        return redirect
    redirect_url = f"/podotchet/?org_id={org_id}"

    try:
        amount_val = _parse_amount(amount)
        date_val = _parse_date(date_str)
    except ValueError:
        return RedirectResponse(f"{redirect_url}&error=Неверная сумма или дата", status_code=303)
    if amount_val <= 0:
        return RedirectResponse(f"{redirect_url}&error=Сумма должна быть больше нуля", status_code=303)

    org = _resolve_business_org(db, org_id)
    if not org:
        return RedirectResponse(f"{redirect_url}&error=Выберите конкретный объект наверху страницы", status_code=303)

    founder = _resolve_founder(db, founder_user_id)
    if not founder:
        return RedirectResponse(f"{redirect_url}&error=Укажите, кто из учредителей внёс деньги", status_code=303)

    holder = _resolve_holder(db, org)
    if not holder:
        msg = "Для этого объекта не задан держатель кассы — задайте в Настройках"
        return RedirectResponse(f"{redirect_url}&error={quote(msg)}", status_code=303)

    db.add(CashFunding(
        organization_id=org.id,
        source_type="direct_cash",
        amount=amount_val,
        date=date_val,
        taken_by=user.id,
        accountable_user_id=holder.id,
        source_founder_id=founder.id,
        comment=comment.strip() or None,
        created_by=user.id,
    ))
    db.commit()
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/founder-withdraw")
def create_founder_withdraw(
    request: Request,
    amount: str = Form(...),
    date_str: str = Form(..., alias="date"),
    founder_user_id: str = Form(...),
    comment: str = Form(default=""),
    org_id: str = Form(...),
    db: Session = Depends(get_db),
):
    """Учредитель забрал наличные из кассы себе — изъятие капитала, не
    операционный расход. Уменьшает пул подотчёта (см. get_podotchet_ledger),
    счёта не касается — деньги уже были в кассе, не сняты повторно с банка."""
    user, redirect = _guard(request, db)
    if redirect:
        return redirect
    redirect_url = f"/podotchet/?org_id={org_id}"

    try:
        amount_val = _parse_amount(amount)
        date_val = _parse_date(date_str)
    except ValueError:
        return RedirectResponse(f"{redirect_url}&error=Неверная сумма или дата", status_code=303)
    if amount_val <= 0:
        return RedirectResponse(f"{redirect_url}&error=Сумма должна быть больше нуля", status_code=303)

    org = _resolve_business_org(db, org_id)
    if not org:
        return RedirectResponse(f"{redirect_url}&error=Выберите конкретный объект наверху страницы", status_code=303)

    founder = _resolve_founder(db, founder_user_id)
    if not founder:
        return RedirectResponse(f"{redirect_url}&error=Укажите, кто из учредителей забрал деньги", status_code=303)

    db.add(CapitalWithdrawal(
        organization_id=org.id,
        founder_user_id=founder.id,
        amount=amount_val,
        date=date_val,
        comment=comment.strip() or None,
        created_by=user.id,
    ))
    db.commit()
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/capital-withdrawal/{withdrawal_id}/delete")
def delete_founder_withdraw(
    request: Request,
    withdrawal_id: int,
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect
    redirect_url = f"/podotchet/?org_id={org_id}" if org_id else "/podotchet/"

    withdrawal = db.query(CapitalWithdrawal).filter(
        CapitalWithdrawal.id == withdrawal_id, CapitalWithdrawal.deleted_at.is_(None)
    ).first()
    if withdrawal:
        withdrawal.deleted_at = func.now()
        db.commit()
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/fund/{funding_id}/delete")
def delete_funding(
    request: Request,
    funding_id: int,
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect
    redirect_url = f"/podotchet/?org_id={org_id}" if org_id else "/podotchet/"
    err_sep = "&" if org_id else "?"

    funding = db.query(CashFunding).filter(CashFunding.id == funding_id, CashFunding.deleted_at.is_(None)).first()
    if funding and funding.source_transaction_id is not None:
        # Создана автоматически оплатой разовой услуги (25.08) — удалить
        # только отсюда нельзя, доход в балансе ребёнка останется висеть.
        # Откат — на странице «Услуги», там же, где отмечали оплату.
        msg = "Эта запись создана оплатой услуги — удалите её на странице «Услуги»"
        return RedirectResponse(f"{redirect_url}{err_sep}error={quote(msg)}", status_code=303)
    if funding:
        funding.deleted_at = func.now()
        db.commit()
    return RedirectResponse(redirect_url, status_code=303)


def _reconcile_error(redirect_url: str, sep: str, msg: str, *, kind: str,
                     balance: str = "", date_str: str = "", comment: str = "",
                     subject_id: str = "") -> RedirectResponse:
    """Вернуть на страницу с ошибкой, не потеряв введённое.

    Раньше редирект уносил только текст ошибки, и человек набирал сумму, дату и
    пояснение заново — на телефоне это отдельное мучение (08.09). Значения
    возвращаются в query и подставляются обратно ровно в ту форму, из которой
    пришли: `err_kind` (+ `err_subject` для поставщика) говорит шаблону, какую."""
    parts = [
        f"error={quote(msg)}",
        f"err_kind={quote(kind)}",
        f"err_balance={quote(balance)}",
        f"err_date={quote(date_str)}",
        f"err_comment={quote(comment)}",
    ]
    if subject_id:
        parts.append(f"err_subject={quote(subject_id)}")
    return RedirectResponse(f"{redirect_url}{sep}" + "&".join(parts), status_code=303)


@router.post("/reconcile")
def add_reconciliation(
    request: Request,
    balance: str = Form(...),
    date_str: str = Form(..., alias="date"),
    organization_id: str = Form(...),
    kind: str = Form(default=reconciliation.ACCOUNT),
    subject_id: str = Form(default=""),
    comment: str = Form(default=""),
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Сверка счёта, кассы или долга поставщику — одна форма на все три вида.
    Ожидаемая сумма считается здесь и сохраняется вместе с разницей: если её
    не записать сейчас, восстановить потом будет нельзя (07.09)."""
    user, redirect = _guard(request, db)
    if redirect:
        return redirect
    redirect_url = f"/podotchet/?org_id={org_id}" if org_id else "/podotchet/"
    error_sep = "&" if org_id else "?"

    if kind not in reconciliation.KIND_LABELS:
        return RedirectResponse(f"{redirect_url}{error_sep}error=Неизвестный вид сверки", status_code=303)

    def _back(msg: str) -> RedirectResponse:
        return _reconcile_error(redirect_url, error_sep, msg, kind=kind, balance=balance,
                                date_str=date_str, comment=comment, subject_id=subject_id)

    try:
        balance_val = _parse_amount(balance)
        date_val = _parse_date(date_str)
    except ValueError:
        return _back("Неверная сумма или дата")

    subject = int(subject_id) if subject_id.isdigit() else None
    if kind == reconciliation.SUPPLIER_DEBT and subject is None:
        return _back("Не выбран поставщик")

    # Двойной сабмит — не гипотеза: 08.09 сверка кассы Сокулука записалась
    # дважды с разницей в 44 секунды (id 4 и 5, одинаковые суммы). На расчёт
    # это не влияет (берётся последняя), но реестр корректировок, который
    # собственники смотрят как список событий, задваивался. Тот же
    # acquire_submission_lock, что на /students/add и оплате разовой услуги.
    acquire_submission_lock(
        db, "reconcile", f"{organization_id}:{kind}:{subject}:{date_val}:{balance_val}"
    )
    recent = (
        db.query(Reconciliation)
        .filter(
            Reconciliation.organization_id == int(organization_id),
            Reconciliation.kind == kind,
            Reconciliation.date == date_val,
            Reconciliation.actual_amount == Decimal(str(balance_val)),
            Reconciliation.cancelled_at.is_(None),
            Reconciliation.created_at >= func.now() - text("interval '30 seconds'"),
        )
        .first()
    )
    if recent is not None:
        # Молча уводим на страницу: человек нажал дважды, ошибки не было —
        # его сверка уже сохранена.
        return RedirectResponse(redirect_url, status_code=303)

    # Долг поставщику правится один раз — по прямому требованию заказчика
    # (07.09): корректировка нужна, чтобы завести долг «с прошлого года»,
    # которого в системе нет, а не чтобы подгонять цифру каждый месяц. Дальше
    # долг двигают закупы и платежи. Ошиблись — отмените запись с причиной,
    # она останется видна в реестре корректировок.
    if kind == reconciliation.SUPPLIER_DEBT:
        existing = reconciliation.latest(db, int(organization_id),
                                         reconciliation.SUPPLIER_DEBT, subject)
        if existing is not None:
            return _back(
                f"Долг этому поставщику уже корректировали {existing.date.strftime('%d.%m.%Y')}. "
                "Отмените ту запись, если она неверна."
            )

    rec = reconciliation.create(
        db, organization_id=int(organization_id), kind=kind, actual=balance_val,
        user_id=user.id, on_date=date_val, subject_id=subject, reason=comment,
    )
    # Расхождение без объяснения — это ровно та дыра, ради которой всё
    # затевалось: цифра меняется, причина неизвестна. Мелкие расхождения
    # (сдача, округление) пропускаем без пояснения, заметные — нет.
    if reconciliation.severity(rec.expected_amount, rec.delta) == "big" and not rec.reason:
        db.rollback()
        return _back("Разница заметная — напишите, что произошло, без этого сверка не сохранится")

    db.commit()
    return RedirectResponse(redirect_url, status_code=303)


@router.get("/corrections", response_class=HTMLResponse)
def corrections_registry(request: Request, org_id: str = "", db: Session = Depends(get_db)):
    """Реестр всех корректировок остатков — касса, счёт, долги поставщикам.

    Отдельная страница, потому что это не рутина, а события, которые собственник
    должен иметь возможность проверить одним взглядом (требование заказчика
    07.09). Доступ шире, чем у подотчёта: сюда должны попадать founder'ы, даже
    если они не участвуют в операционке."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    rows = reconciliation.all_corrections(db)
    users_by_id = {u.id: u.name for u in db.query(User).all()}
    orgs_by_id = {o.id: o.name for o in db.query(Organization).all()}
    suppliers_by_id = {s.id: s.name for s in db.query(Supplier).all()}

    items = [
        {
            "rec": r,
            "org_name": orgs_by_id.get(r.organization_id, "?"),
            "kind_label": reconciliation.KIND_LABELS.get(r.kind, r.kind),
            "subject_name": suppliers_by_id.get(r.subject_id) if r.subject_id else None,
            "author": users_by_id.get(r.created_by, "?"),
            "canceller": users_by_id.get(r.cancelled_by) if r.cancelled_by else None,
            "severity": reconciliation.severity(r.expected_amount, r.delta),
        }
        for r in rows
    ]

    return templates.TemplateResponse("podotchet/corrections.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": get_accessible_orgs(user, db),
        "current_org_id": int(org_id) if org_id.isdigit() else None,
        "items": items,
        "active_page": "corrections",
    })


@router.post("/reconcile/{rec_id}/cancel")
def cancel_reconciliation(
    rec_id: int,
    request: Request,
    reason: str = Form(default=""),
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """Отмена сверки вместо удаления — строка остаётся видна с причиной.
    Иначе исправление опечатки ничем не отличалось бы от заметания следов."""
    user, redirect = _guard(request, db)
    if redirect:
        return redirect
    redirect_url = f"/podotchet/?org_id={org_id}" if org_id else "/podotchet/"
    error_sep = "&" if org_id else "?"

    if not reason.strip():
        msg = quote("Напишите, почему отменяете сверку")
        return RedirectResponse(f"{redirect_url}{error_sep}error={msg}", status_code=303)

    if reconciliation.cancel(db, rec_id, user.id, reason) is None:
        msg = quote("Сверка не найдена или уже отменена")
        return RedirectResponse(f"{redirect_url}{error_sep}error={msg}", status_code=303)
    db.commit()
    return RedirectResponse(redirect_url, status_code=303)
