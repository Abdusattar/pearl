from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Organization, Student, User, Group, Enrollment, Service, StudentService, AuditLog
from app.services.students import (
    get_next_free_pin, update_student, compose_name, archive_stale_students,
    set_first_enrollment_start,
)
from app.services.billing import (
    generate_monthly_charges, get_balance, get_ledger, set_student_services, get_tuition_service,
    continuous_since_from_enrollments, tuition_base_price,
)
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org

router = APIRouter(prefix="/students", tags=["students"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))



# ── LIST ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def list_students(
    request: Request,
    org_id: str | None = None,
    q: str | None = None,
    group_id: str | None = None,
    status: str = "active",
    db: Session = Depends(get_db),
):
    org_id = int(org_id) if org_id and org_id.isdigit() else None
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    archive_stale_students(db)
    generate_monthly_charges(db)
    db.commit()

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)

    # Рекурсивно собираем все дочерние орги (внуки тоже)
    all_orgs = db.query(Organization).all()
    def descendants(oid):
        ids = {oid}
        for o in all_orgs:
            if o.parent_id == oid:
                ids |= descendants(o.id)
        return ids

    org_ids = descendants(current_org.id) if current_org else {o.id for o in accessible}

    query = db.query(Student)
    if status in ("active", "inactive", "frozen"):
        query = query.filter(Student.status == status)
    else:
        query = query.filter(Student.status.in_(("active", "inactive", "frozen")))
    if current_org:
        query = query.filter(Student.organization_id.in_(org_ids))
    if q:
        query = query.filter(
            Student.name.ilike(f"%{q}%") | Student.pin.ilike(f"%{q}%")
        )

    group_id_int = int(group_id) if group_id and group_id.isdigit() else None
    if group_id_int:
        query = query.filter(Student.id.in_(
            db.query(Enrollment.student_id).filter(
                Enrollment.group_id == group_id_int, Enrollment.end_date.is_(None)
            )
        ))

    students = query.order_by(Student.pin).all()

    groups_by_student = {}
    if students:
        rows = (
            db.query(Enrollment.student_id, Group.name)
            .join(Group, Group.id == Enrollment.group_id)
            .filter(
                Enrollment.student_id.in_([s.id for s in students]),
                Enrollment.end_date.is_(None),
            )
            .all()
        )
        groups_by_student = {sid: gname for sid, gname in rows}

    available_groups = (
        db.query(Group)
        .filter(Group.organization_id.in_(org_ids))
        .order_by(Group.name)
        .all()
    )

    # Список по умолчанию сгруппирован по группе (алфавитный порядок групп,
    # внутри группы — по PIN, как и раньше). Дети без группы — секцией в конце.
    grouped_students = []
    for g in available_groups:
        members = [s for s in students if groups_by_student.get(s.id) == g.name]
        if members:
            grouped_students.append({"name": g.name, "students": members})
    no_group = [s for s in students if s.id not in groups_by_student]
    if no_group:
        grouped_students.append({"name": "Без группы", "students": no_group})

    return templates.TemplateResponse("students/list.html", {
        "request": request,
        "students": students,
        "grouped_students": grouped_students,
        "available_groups": available_groups,
        "current_group_id": group_id_int,
        "groups_by_student": groups_by_student,
        "q": q or "",
        "status": status,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "current_user": user,
        "active_page": "students",
    })


# ── BULK-EDIT (дата поступления/группа/статус разом, для бэкфилла) ────────────

@router.get("/bulk-dates", response_class=HTMLResponse)
def bulk_dates_form(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    org_id = int(org_id) if org_id and org_id.isdigit() else None
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)

    students = (
        db.query(Student)
        .filter(Student.organization_id == current_org.id)
        .order_by(Student.name)
        .all()
    ) if current_org else []

    groups = (
        db.query(Group)
        .filter(Group.organization_id == current_org.id)
        .order_by(Group.name)
        .all()
    ) if current_org else []

    enrollments_by_student: dict[int, list[Enrollment]] = {}
    if students:
        rows = (
            db.query(Enrollment)
            .filter(Enrollment.student_id.in_([s.id for s in students]))
            .order_by(Enrollment.start_date.asc(), Enrollment.id.asc())
            .all()
        )
        for e in rows:
            enrollments_by_student.setdefault(e.student_id, []).append(e)

    # Бейдж "сколько будет платить" по тарифу (22.07) — та же формула, что
    # billing._tuition_fee(), но по уже загрученным enrollments_by_student
    # (без запроса на каждого ребёнка) — чтобы Махабат сразу видела результат
    # правки даты, не открывая /services/.
    tuition_service = get_tuition_service(db, current_org.id) if current_org else None
    new_price = float(tuition_service.price) if tuition_service else None
    legacy_active = bool(
        current_org and current_org.legacy_tariff_cutoff
        and current_org.legacy_tariff_price is not None
        and current_org.legacy_tariff_until and date.today() <= current_org.legacy_tariff_until
    )

    student_rows = []
    for s in students:
        rows = enrollments_by_student.get(s.id, [])
        active = next((e for e in rows if e.end_date is None), None)

        tariff_badge = None
        if new_price is not None and s.status in ("active", "frozen"):
            base = new_price
            if legacy_active:
                since = continuous_since_from_enrollments(rows)
                if since and since < current_org.legacy_tariff_cutoff:
                    base = float(current_org.legacy_tariff_price)
            tariff_badge = max(0.0, base - float(s.discount_amount or 0))

        student_rows.append({
            "id": s.id,
            "name": s.name,
            "status": s.status,
            "group_id": active.group_id if active else None,
            "start_date": rows[0].start_date.isoformat() if rows else "",
            "tariff_badge": tariff_badge,
        })

    return templates.TemplateResponse("students/bulk_dates.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "groups": groups,
        "student_rows": student_rows,
    })


@router.post("/bulk-dates")
def bulk_dates_save(
    request: Request,
    org_id: str = Form(...),
    student_id: list[int] = Form(default=[]),
    group_id: list[str] = Form(default=[]),
    status: list[str] = Form(default=[]),
    start_date: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    if not student_id:
        return RedirectResponse(f"/students/bulk-dates?org_id={org_id}", status_code=303)

    # Форма всегда шлёт ВСЕ строки таблицы (даже нетронутые — их текущие значения
    # просто едут назад как есть), не только изменённую. При ~80 детях на объект
    # старая версия дёргала по 2-4 отдельных запроса в БД на каждую строку внутри
    # питоновского цикла (update_student()/set_first_enrollment_start() сами делают
    # свои SELECT) — правка одного ребёнка ощущалась как "зависание". Тут — один
    # батч-запрос на всех детей/зачисления объекта разом (как уже делает GET-версия
    # этой же страницы), и запись только по строкам, где значение реально изменилось.
    students = {s.id: s for s in db.query(Student).filter(Student.id.in_(student_id)).all()}
    enrollments_by_student: dict[int, list[Enrollment]] = {}
    for e in db.query(Enrollment).filter(Enrollment.student_id.in_(student_id)) \
            .order_by(Enrollment.start_date.asc(), Enrollment.id.asc()).all():
        enrollments_by_student.setdefault(e.student_id, []).append(e)

    for i, sid in enumerate(student_id):
        student = students.get(sid)
        if not student:
            continue
        st = status[i] if i < len(status) else "active"
        gid_raw = group_id[i] if i < len(group_id) else ""
        gid = int(gid_raw) if gid_raw.isdigit() else None
        date_raw = (start_date[i] if i < len(start_date) else "").strip()

        s_rows = enrollments_by_student.get(sid, [])
        current_enrollment = next((e for e in s_rows if e.end_date is None), None)
        current_gid = current_enrollment.group_id if current_enrollment else None
        first_start = s_rows[0].start_date if s_rows else None

        if st != student.status or gid != current_gid:
            update_student(
                db, sid, student.last_name or "", student.first_name or "",
                student.patronymic or "", gid, st, student.parent_name, student.parent_contact,
            )
        if date_raw:
            try:
                new_date = date.fromisoformat(date_raw)
                if new_date != first_start:
                    set_first_enrollment_start(db, sid, new_date)
            except ValueError:
                pass

    db.commit()
    return RedirectResponse(f"/students/bulk-dates?org_id={org_id}", status_code=303)


# ── ADD ───────────────────────────────────────────────────────────────────────

@router.get("/add", response_class=HTMLResponse)
def add_student_form(
    request: Request,
    org_id: str | None = None,
    db: Session = Depends(get_db),
):
    org_id = int(org_id) if org_id and org_id.isdigit() else None
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)
    next_pin = get_next_free_pin(db)

    # Только листовые орги для выбора (куда добавляем ребёнка)
    all_orgs = db.query(Organization).all()
    has_children = {o.parent_id for o in all_orgs if o.parent_id is not None}
    leaf_orgs = [o for o in accessible if o.id not in has_children]

    groups = (
        db.query(Group)
        .filter(Group.organization_id == current_org.id, Group.deleted_at.is_(None))
        .order_by(Group.name)
        .all()
    ) if current_org else []

    return templates.TemplateResponse("students/add.html", {
        "request": request,
        "next_pin": next_pin,
        "leaf_orgs": leaf_orgs,
        "groups": groups,
        "current_org_id": current_org.id if current_org else None,
        "accessible_orgs": accessible,
        "current_user": user,
        "active_page": "students",
        "error": None,
    })


@router.post("/add", response_class=HTMLResponse)
def add_student(
    request: Request,
    last_name: str = Form(...),
    first_name: str = Form(...),
    patronymic: str = Form(""),
    org_id_selected: int = Form(...),
    group_id: str = Form(""),
    parent_name: str = Form(""),
    parent_contact: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    accessible = get_accessible_orgs(user, db)
    all_orgs = db.query(Organization).all()
    has_children = {o.parent_id for o in all_orgs if o.parent_id is not None}
    leaf_orgs = [o for o in accessible if o.id not in has_children]
    groups = (
        db.query(Group)
        .filter(Group.organization_id == org_id_selected, Group.deleted_at.is_(None))
        .order_by(Group.name)
        .all()
    )

    last_name = last_name.strip()
    first_name = first_name.strip()
    if not last_name or not first_name:
        next_pin = get_next_free_pin(db)
        return templates.TemplateResponse("students/add.html", {
            "request": request,
            "next_pin": next_pin,
            "leaf_orgs": leaf_orgs,
            "groups": groups,
            "current_org_id": org_id_selected,
            "accessible_orgs": accessible,
            "current_user": user,
            "active_page": "students",
            "error": "Заполните фамилию и имя ребёнка",
        })

    pin = get_next_free_pin(db)

    student = Student(
        organization_id=org_id_selected,
        name=compose_name(last_name, first_name, patronymic),
        last_name=last_name,
        first_name=first_name,
        patronymic=patronymic.strip() or None,
        pin=pin,
        status="active",
        parent_name=parent_name.strip() or None,
        parent_contact=parent_contact.strip() or None,
    )
    db.add(student)
    db.flush()

    gid = int(group_id) if group_id.isdigit() else None
    if gid:
        db.add(Enrollment(student_id=student.id, group_id=gid, start_date=date.today()))

    db.commit()

    return RedirectResponse(f"/students/{student.id}/edit", status_code=303)


# ── EDIT ──────────────────────────────────────────────────────────────────────

def _edit_context(db: Session, student: Student, error: str | None = None):
    groups = (
        db.query(Group)
        .filter(Group.organization_id == student.organization_id)
        .order_by(Group.name)
        .all()
    )
    current_enrollment = (
        db.query(Enrollment)
        .filter(Enrollment.student_id == student.id, Enrollment.end_date.is_(None))
        .first()
    )
    # «Обучение» (is_tuition) — не чекбокс, применяется автоматически всем,
    # в список опциональных доп.услуг не попадает
    services = (
        db.query(Service)
        .filter(
            Service.organization_id == student.organization_id,
            Service.deleted_at.is_(None),
            Service.is_tuition.is_(False),
        )
        .order_by(Service.name)
        .all()
    )
    active_service_ids = {
        ss.service_id for ss in db.query(StudentService).filter(
            StudentService.student_id == student.id, StudentService.end_date.is_(None)
        )
    }
    active_services = [s for s in services if s.id in active_service_ids]
    base_tuition_price = tuition_base_price(db, student)
    org = db.query(Organization).get(student.organization_id)
    return {
        "student": student,
        "groups": groups,
        "current_group_id": current_enrollment.group_id if current_enrollment else None,
        "current_group_name": current_enrollment.group.name if current_enrollment else None,
        "services": services,
        "active_service_ids": active_service_ids,
        "active_services": active_services,
        "current_org_id": student.organization_id,
        "active_page": "students",
        "error": error,
        "default_monthly_fee": base_tuition_price,
        "tuition_fee": max(0.0, base_tuition_price - float(student.discount_amount or 0)),
        "frozen_discount_percent": float(org.frozen_discount_percent) if org else 50.0,
        # Новая карточка (ещё нет группы) — сразу в редактируемом виде, без лишнего
        # клика «Изменить»: смотреть там пока нечего (16.07, тот же принцип, что и
        # у /menu/ — пустой день/новая карточка не прячется за просмотр).
        "personal_needs_edit": current_enrollment is None and student.status == "active",
    }


@router.get("/{student_id}/edit", response_class=HTMLResponse)
def edit_student_form(student_id: int, request: Request, saved: str | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        return RedirectResponse("/students/", status_code=302)

    ctx = _edit_context(db, student)
    ctx.update({
        "request": request,
        "accessible_orgs": get_accessible_orgs(user, db),
        "current_user": user,
        "saved": saved,
    })
    return templates.TemplateResponse("students/edit.html", ctx)


@router.post("/{student_id}/edit", response_class=HTMLResponse)
def edit_student(
    student_id: int,
    request: Request,
    last_name: str = Form(...),
    first_name: str = Form(...),
    patronymic: str = Form(""),
    group_id: str = Form(""),
    status: str = Form("active"),
    parent_name: str = Form(""),
    parent_contact: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        return RedirectResponse("/students/", status_code=302)

    if not last_name.strip() or not first_name.strip():
        ctx = _edit_context(db, student, error="Заполните фамилию и имя ребёнка")
        ctx.update({
            "request": request,
            "accessible_orgs": get_accessible_orgs(user, db),
            "current_user": user,
        })
        return templates.TemplateResponse("students/edit.html", ctx)

    gid = int(group_id) if group_id.isdigit() else None
    update_student(db, student_id, last_name, first_name, patronymic, gid, status, parent_name, parent_contact)
    db.commit()

    return RedirectResponse(f"/students/{student_id}/edit?saved=personal", status_code=303)


@router.post("/{student_id}/billing", response_class=HTMLResponse)
def edit_student_billing(
    student_id: int,
    request: Request,
    service_ids: list[int] = Form(default=[]),
    discount_amount: str = Form(""),
    discount_reason: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        return RedirectResponse("/students/", status_code=302)

    try:
        discount_som = float(discount_amount.strip()) if discount_amount.strip() else 0.0
    except ValueError:
        discount_som = -1  # заведомо невалидное — попадёт в проверку ниже

    base_price = tuition_base_price(db, student)
    if not (0 <= discount_som <= base_price):
        ctx = _edit_context(db, student, error=f"Скидка должна быть числом от 0 до {base_price:.0f} сом")
        ctx.update({"request": request, "accessible_orgs": get_accessible_orgs(user, db), "current_user": user})
        return templates.TemplateResponse("students/edit.html", ctx)
    if discount_som > 0 and not discount_reason.strip():
        ctx = _edit_context(db, student, error="Укажи причину скидки — без неё скидку не поставить")
        ctx.update({"request": request, "accessible_orgs": get_accessible_orgs(user, db), "current_user": user})
        return templates.TemplateResponse("students/edit.html", ctx)

    old_amount = float(student.discount_amount or 0)
    if discount_som != old_amount or (discount_som > 0 and discount_reason.strip() != (student.discount_reason or "")):
        db.add(AuditLog(
            entity_type="student_discount", entity_id=student.id, action="update", user_id=user.id,
            old_data={"discount_amount": old_amount, "discount_reason": student.discount_reason},
            new_data={"discount_amount": discount_som, "discount_reason": discount_reason.strip() or None},
        ))
        student.discount_set_by = user.id
        student.discount_set_at = datetime.now()

    student.discount_amount = discount_som
    student.discount_reason = discount_reason.strip() or None

    set_student_services(db, student_id, service_ids)
    db.commit()

    return RedirectResponse(f"/students/{student_id}/edit?saved=billing", status_code=303)


# ── БАЛАНС И ИСТОРИЯ (просмотр + ручная корректировка) ───────────────────────────

@router.get("/{student_id}/history", response_class=HTMLResponse)
def student_history(
    student_id: int, request: Request, source: str | None = None,
    month: str | None = None, db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        return RedirectResponse("/students/", status_code=302)

    if source == "income":
        back_url, back_label, active_page = "/income/", "← Назад к оплатам", "income"
    else:
        back_url, back_label, active_page = f"/students/{student_id}/edit", "← Личные данные и оплата", "students"

    # Баланс — всегда по всей истории целиком, фильтр по месяцу его не трогает
    # (иначе долг на экране разъехался бы с реальным). Список ниже — можно
    # сузить до месяца, если история давно копится (22.07, найдено на аудите
    # модуля «Дети» — таблица без пагинации могла расти неограниченно).
    full_ledger = get_ledger(db, student.id)
    months = sorted({row["date"].strftime("%Y-%m") for row in full_ledger}, reverse=True)
    ledger = [row for row in full_ledger if not month or row["date"].strftime("%Y-%m") == month]

    return templates.TemplateResponse("students/history.html", {
        "request": request,
        "student": student,
        "balance": get_balance(db, student.id),
        "ledger": ledger,
        "months": months,
        "current_month": month,
        "source": source,
        "current_org_id": student.organization_id,
        "back_url": back_url,
        "back_label": back_label,
        "active_page": active_page,
        "accessible_orgs": get_accessible_orgs(user, db),
        "current_user": user,
    })


