from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Organization, Student, User
from app.services.students import get_next_free_pin, deactivate_student

router = APIRouter(prefix="/students", tags=["students"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

ORG_KINDERGARTENS = 3


def get_mock_user(db: Session) -> User:
    return db.query(User).filter(User.deleted_at.is_(None)).first()


def get_accessible_orgs(user: User, db: Session) -> list[Organization]:
    all_orgs = db.query(Organization).all()
    if user.role in ("owner", "director"):
        return all_orgs
    if user.role == "manager":
        kinder_ids = {ORG_KINDERGARTENS}
        kinder_ids |= {o.id for o in all_orgs if o.parent_id == ORG_KINDERGARTENS}
        return [o for o in all_orgs if o.id in kinder_ids]
    return [o for o in all_orgs if o.id == user.organization_id]


def resolve_org(org_id: int | None, user: User, db: Session) -> Organization:
    accessible = get_accessible_orgs(user, db)
    if org_id:
        org = next((o for o in accessible if o.id == org_id), None)
        if org:
            return org
    return accessible[0] if accessible else None


# ── LIST ──────────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def list_students(
    request: Request,
    org_id: str | None = None,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    org_id = int(org_id) if org_id and org_id.isdigit() else None
    user = get_mock_user(db)
    if not user:
        return HTMLResponse("Нет пользователей в БД.", status_code=503)

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(org_id, user, db)

    query = db.query(Student).filter(Student.status == "active")
    if current_org:
        # Рекурсивно собираем все дочерние орги (внуки тоже)
        all_orgs = db.query(Organization).all()
        def descendants(org_id):
            ids = {org_id}
            for o in all_orgs:
                if o.parent_id == org_id:
                    ids |= descendants(o.id)
            return ids
        org_ids = descendants(current_org.id)
        query = query.filter(Student.organization_id.in_(org_ids))
    if q:
        query = query.filter(
            Student.name.ilike(f"%{q}%") | Student.pin.ilike(f"%{q}%")
        )

    students = query.order_by(Student.pin).all()

    return templates.TemplateResponse("students/list.html", {
        "request": request,
        "students": students,
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
    user = get_mock_user(db)
    if not user:
        return HTMLResponse("Нет пользователей в БД.", status_code=503)

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


# ── DEACTIVATE ────────────────────────────────────────────────────────────────

@router.post("/{student_id}/deactivate")
def deactivate(student_id: int, db: Session = Depends(get_db)):
    student = deactivate_student(db, student_id)
    db.commit()
    return RedirectResponse(f"/students/?org_id={student.organization_id}", status_code=303)
