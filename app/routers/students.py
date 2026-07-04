from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Organization, Student, User, Group, Enrollment, Service, StudentService
from app.services.students import (
    get_next_free_pin, update_student, compose_name, archive_stale_students,
)
from app.services.billing import (
    generate_monthly_charges, get_balance, get_ledger, set_student_services,
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
    if status in ("active", "inactive"):
        query = query.filter(Student.status == status)
    else:
        query = query.filter(Student.status.in_(("active", "inactive")))
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

    return templates.TemplateResponse("students/list.html", {
        "request": request,
        "students": students,
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

    return templates.TemplateResponse("students/add.html", {
        "request": request,
        "next_pin": next_pin,
        "leaf_orgs": leaf_orgs,
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

    last_name = last_name.strip()
    first_name = first_name.strip()
    if not last_name or not first_name:
        next_pin = get_next_free_pin(db)
        return templates.TemplateResponse("students/add.html", {
            "request": request,
            "next_pin": next_pin,
            "leaf_orgs": leaf_orgs,
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
    services = (
        db.query(Service)
        .filter(Service.organization_id == student.organization_id, Service.deleted_at.is_(None))
        .order_by(Service.name)
        .all()
    )
    active_service_ids = {
        ss.service_id for ss in db.query(StudentService).filter(
            StudentService.student_id == student.id, StudentService.end_date.is_(None)
        )
    }
    active_services = [s for s in services if s.id in active_service_ids]
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
    monthly_fee: str = Form(""),
    service_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        return RedirectResponse("/students/", status_code=302)

    extra = dict(student.extra or {})
    if monthly_fee.strip():
        try:
            extra["monthly_fee"] = float(monthly_fee.strip())
        except ValueError:
            pass
    else:
        extra.pop("monthly_fee", None)
    student.extra = extra or None

    set_student_services(db, student_id, service_ids)
    db.commit()

    return RedirectResponse(f"/students/{student_id}/edit?saved=billing", status_code=303)


# ── БАЛАНС И ИСТОРИЯ (просмотр + ручная корректировка) ───────────────────────────

@router.get("/{student_id}/history", response_class=HTMLResponse)
def student_history(student_id: int, request: Request, source: str | None = None, db: Session = Depends(get_db)):
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

    return templates.TemplateResponse("students/history.html", {
        "request": request,
        "student": student,
        "balance": get_balance(db, student.id),
        "ledger": get_ledger(db, student.id),
        "current_org_id": student.organization_id,
        "back_url": back_url,
        "back_label": back_label,
        "active_page": active_page,
        "accessible_orgs": get_accessible_orgs(user, db),
        "current_user": user,
    })


