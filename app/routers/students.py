from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Organization, Student, User, Group, Enrollment, Service, StudentService, Charge
from app.services.students import (
    get_next_free_pin, deactivate_student, update_student, archive_stale_students,
)
from app.services.billing import (
    generate_monthly_charges, get_balance, add_manual_charge, set_student_services,
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

    query = db.query(Student).filter(Student.status == "active")
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
    name: str = Form(...),
    org_id_selected: int = Form(...),
    parent_contact: str = Form(""),
    monthly_fee: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_mock_user(db)
    accessible = get_accessible_orgs(user, db)
    all_orgs = db.query(Organization).all()
    has_children = {o.parent_id for o in all_orgs if o.parent_id is not None}
    leaf_orgs = [o for o in accessible if o.id not in has_children]

    name = name.strip()
    if not name:
        next_pin = get_next_free_pin(db)
        return templates.TemplateResponse("students/add.html", {
            "request": request,
            "next_pin": next_pin,
            "leaf_orgs": leaf_orgs,
            "current_org_id": org_id_selected,
            "accessible_orgs": accessible,
            "current_user": user,
            "active_page": "students",
            "error": "Введите имя ребёнка",
        })

    pin = get_next_free_pin(db)
    extra = {}
    if monthly_fee.strip():
        try:
            extra["monthly_fee"] = float(monthly_fee.strip())
        except ValueError:
            pass

    student = Student(
        organization_id=org_id_selected,
        name=name,
        pin=pin,
        status="active",
        parent_contact=parent_contact.strip() or None,
        extra=extra or None,
    )
    db.add(student)
    db.commit()

    return RedirectResponse(f"/students/?org_id={org_id_selected}", status_code=303)


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
    charges = (
        db.query(Charge)
        .filter(Charge.student_id == student.id)
        .order_by(Charge.date.desc())
        .all()
    )
    return {
        "student": student,
        "groups": groups,
        "current_group_id": current_enrollment.group_id if current_enrollment else None,
        "services": services,
        "active_service_ids": active_service_ids,
        "balance": get_balance(db, student.id),
        "charges": charges,
        "current_org_id": student.organization_id,
        "active_page": "students",
        "error": error,
    }


@router.get("/{student_id}/edit", response_class=HTMLResponse)
def edit_student_form(student_id: int, request: Request, db: Session = Depends(get_db)):
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
    })
    return templates.TemplateResponse("students/edit.html", ctx)


@router.post("/{student_id}/edit", response_class=HTMLResponse)
def edit_student(
    student_id: int,
    request: Request,
    name: str = Form(...),
    group_id: str = Form(""),
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

    if not name.strip():
        ctx = _edit_context(db, student, error="Введите имя ребёнка")
        ctx.update({
            "request": request,
            "accessible_orgs": get_accessible_orgs(user, db),
            "current_user": user,
        })
        return templates.TemplateResponse("students/edit.html", ctx)

    gid = int(group_id) if group_id.isdigit() else None
    update_student(db, student_id, name, gid)

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

    return RedirectResponse(f"/students/?org_id={student.organization_id}", status_code=303)


# ── РУЧНОЕ НАЧИСЛЕНИЕ (корректировка долга) ─────────────────────────────────────

@router.post("/{student_id}/charge")
def create_charge(
    student_id: int,
    request: Request,
    amount: str = Form(...),
    description: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        return RedirectResponse("/students/", status_code=302)

    try:
        amount_val = float(amount)
    except ValueError:
        amount_val = 0

    if amount_val:
        add_manual_charge(db, student_id, amount_val, description.strip() or "Корректировка", date.today())
        db.commit()

    return RedirectResponse(f"/students/{student_id}/edit", status_code=303)


# ── DEACTIVATE ────────────────────────────────────────────────────────────────

@router.post("/{student_id}/deactivate")
def deactivate(student_id: int, db: Session = Depends(get_db)):
    student = deactivate_student(db, student_id)
    db.commit()
    return RedirectResponse(f"/students/?org_id={student.organization_id}", status_code=303)
