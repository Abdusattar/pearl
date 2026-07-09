from datetime import date as date_type
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import Attendance, Enrollment, Group, Student

router = APIRouter(prefix="/attendance", tags=["attendance"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def attendance_form(
    request: Request,
    org_id: str | None = None,
    date_str: str | None = Query(None, alias="date"),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)
    target_date = date_type.fromisoformat(date_str) if date_str else date_type.today()

    groups = []
    if current_org:
        db_groups = (
            db.query(Group)
            .filter(Group.organization_id == current_org.id, Group.deleted_at.is_(None))
            .order_by(Group.name)
            .all()
        )
        existing = {
            a.student_id: a.present
            for a in db.query(Attendance).filter(
                Attendance.organization_id == current_org.id, Attendance.date == target_date
            ).all()
        }
        for g in db_groups:
            rows = (
                db.query(Student, Enrollment)
                .join(Enrollment, Enrollment.student_id == Student.id)
                .filter(
                    Enrollment.group_id == g.id,
                    Enrollment.end_date.is_(None),
                    Student.status == "active",
                    Student.deleted_at.is_(None),
                )
                .order_by(Student.last_name, Student.first_name)
                .all()
            )
            students = [{
                "id": s.id,
                "name": s.name,
                "absent": not existing.get(s.id, True),
            } for s, _ in rows]
            if students:
                groups.append({"group": g, "students": students})

    return templates.TemplateResponse("attendance/form.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "groups": groups,
        "target_date": target_date.isoformat(),
        "today": date_type.today().isoformat(),
        "active_page": "attendance",
    })


@router.post("/", response_class=HTMLResponse)
def save_attendance(
    request: Request,
    org_id: str = Form(...),
    date_: str = Form(..., alias="date"),
    absent_id: List[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    target_date = date_type.fromisoformat(date_)
    absent_set = set(absent_id)

    active_student_ids = [
        row[0] for row in
        db.query(Student.id).join(Enrollment, Enrollment.student_id == Student.id).filter(
            Student.organization_id == int(org_id),
            Enrollment.end_date.is_(None),
            Student.status == "active",
            Student.deleted_at.is_(None),
        ).distinct().all()
    ]

    existing = {
        a.student_id: a
        for a in db.query(Attendance).filter(
            Attendance.organization_id == int(org_id), Attendance.date == target_date
        ).all()
    }

    for sid in active_student_ids:
        present = sid not in absent_set
        if sid in existing:
            existing[sid].present = present
        else:
            db.add(Attendance(
                student_id=sid, date=target_date, present=present,
                organization_id=int(org_id), created_by=user.id,
            ))
    db.commit()
    return RedirectResponse(f"/attendance/?org_id={org_id}&date={date_}&saved=1", status_code=303)
