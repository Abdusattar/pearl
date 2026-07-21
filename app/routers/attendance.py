from calendar import monthrange
from datetime import date as date_type, timedelta
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

REASON_LABELS = {"vacation": "Отпуск", "sick": "Болезнь", "other": "Другое"}
MAX_RANGE_DAYS = 366


def _active_roster_count(db: Session, org_id: int, target_date: date_type) -> int:
    """Сколько детей формально должны быть отмечены на эту дату — та же логика,
    что и на дневной странице (активен, зачислен, ещё не выбыл). Не то же самое,
    что число реально существующих строк Attendance — их может не быть вовсе,
    если день тронут только через диапазон отсутствия."""
    return (
        db.query(Student.id)
        .join(Enrollment, Enrollment.student_id == Student.id)
        .filter(
            Student.organization_id == org_id,
            Student.status == "active",
            Student.deleted_at.is_(None),
            Enrollment.end_date.is_(None),
            Enrollment.start_date <= target_date,
        )
        .distinct()
        .count()
    )


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
    total_students = 0
    total_absent = 0
    if current_org:
        db_groups = (
            db.query(Group)
            .filter(Group.organization_id == current_org.id, Group.deleted_at.is_(None))
            .order_by(Group.name)
            .all()
        )
        existing = {
            a.student_id: a
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
                    Enrollment.start_date <= target_date,
                    Student.status == "active",
                    Student.deleted_at.is_(None),
                )
                .order_by(Student.last_name, Student.first_name)
                .all()
            )
            students = []
            for s, _ in rows:
                rec = existing.get(s.id)
                absent = (not rec.present) if rec else False
                students.append({
                    "id": s.id,
                    "name": s.name,
                    "absent": absent,
                    "reason": REASON_LABELS.get(rec.reason) if rec and absent else None,
                })
            if students:
                groups.append({"group": g, "students": students})
                total_students += len(students)
                total_absent += sum(1 for st in students if st["absent"])

    return templates.TemplateResponse("attendance/form.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "groups": groups,
        "has_saved": bool(existing) if current_org else False,
        "target_date": target_date.isoformat(),
        "today": date_type.today().isoformat(),
        "total_students": total_students,
        "total_present": total_students - total_absent,
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
            Enrollment.start_date <= target_date,
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
            if present:
                # ручная отметка "пришёл" перекрывает ранее заведённую причину
                # отсутствия (например, диапазон отпуска/болезни на эту дату)
                existing[sid].reason = None
                existing[sid].comment = None
        else:
            db.add(Attendance(
                student_id=sid, date=target_date, present=present,
                organization_id=int(org_id), created_by=user.id,
            ))
    db.commit()
    return RedirectResponse(f"/attendance/?org_id={org_id}&date={date_}&saved=1", status_code=303)


@router.post("/absence-range")
def save_absence_range(
    request: Request,
    org_id: str = Form(...),
    student_id: int = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    reason: str = Form(...),
    comment: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    start = date_type.fromisoformat(start_date)
    end = date_type.fromisoformat(end_date)
    if end < start:
        start, end = end, start
    if (end - start).days > MAX_RANGE_DAYS:
        return RedirectResponse(
            f"/attendance/journal?org_id={org_id}&error=Слишком+длинный+период+(>{MAX_RANGE_DAYS}+дней),+проверьте+даты",
            status_code=303,
        )

    existing = {
        a.date: a
        for a in db.query(Attendance).filter(
            Attendance.student_id == student_id,
            Attendance.date >= start,
            Attendance.date <= end,
        ).all()
    }

    comment_value = comment.strip() or None
    d = start
    while d <= end:
        if d in existing:
            existing[d].present = False
            existing[d].reason = reason
            existing[d].comment = comment_value
        else:
            db.add(Attendance(
                student_id=student_id, date=d, present=False,
                reason=reason, comment=comment_value,
                organization_id=int(org_id), created_by=user.id,
            ))
        d += timedelta(days=1)
    db.commit()
    return RedirectResponse(f"/attendance/journal?org_id={org_id}&saved=1", status_code=303)


def _month_bounds(anchor: date_type) -> tuple[date_type, date_type]:
    last_day = monthrange(anchor.year, anchor.month)[1]
    return anchor.replace(day=1), anchor.replace(day=last_day)


@router.get("/journal", response_class=HTMLResponse)
def attendance_journal(
    request: Request,
    org_id: str | None = None,
    month: str | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)

    today = date_type.today()
    if month:
        y, m = month.split("-")
        month_anchor = date_type(int(y), int(m), 1)
    else:
        month_anchor = today.replace(day=1)
    month_start, month_end = _month_bounds(month_anchor)

    days = []
    absences = []
    students_options = []

    if current_org:
        rows = (
            db.query(Attendance)
            .filter(
                Attendance.organization_id == current_org.id,
                Attendance.date >= month_start,
                Attendance.date <= month_end,
            )
            .order_by(Attendance.date)
            .all()
        )
        by_date: dict[date_type, list] = {}
        for a in rows:
            by_date.setdefault(a.date, []).append(a)

        d = month_start
        while d <= min(month_end, today):
            marks = by_date.get(d, [])
            if marks:
                total = _active_roster_count(db, current_org.id, d)
                absent = sum(1 for a in marks if not a.present)
                days.append({
                    "date": d.isoformat(), "label": d.strftime("%d.%m"),
                    "total": total, "present": total - absent, "absent": absent,
                })
            d += timedelta(days=1)
        days.reverse()

        absent_rows = sorted((a for a in rows if not a.present), key=lambda a: (a.student_id, a.date))
        current = None
        for a in absent_rows:
            if (current and current["student_id"] == a.student_id
                    and current["reason"] == a.reason and current["comment"] == a.comment
                    and (a.date - current["end_d"]).days == 1):
                current["end_d"] = a.date
                current["days"] += 1
            else:
                if current:
                    absences.append(current)
                current = {
                    "student_id": a.student_id, "reason": a.reason, "comment": a.comment,
                    "start_d": a.date, "end_d": a.date, "days": 1,
                }
        if current:
            absences.append(current)

        student_ids = {a["student_id"] for a in absences}
        names = {s.id: s.name for s in db.query(Student).filter(Student.id.in_(student_ids)).all()} if student_ids else {}
        for a in absences:
            a["student_name"] = names.get(a["student_id"], "?")
            a["start"] = a["start_d"].strftime("%d.%m")
            a["end"] = a["end_d"].strftime("%d.%m")
        absences.sort(key=lambda a: a["start_d"], reverse=True)

        active_rows = (
            db.query(Student, Group)
            .join(Enrollment, Enrollment.student_id == Student.id)
            .join(Group, Group.id == Enrollment.group_id)
            .filter(
                Student.organization_id == current_org.id,
                Student.status == "active",
                Student.deleted_at.is_(None),
                Enrollment.end_date.is_(None),
            )
            .order_by(Group.name, Student.last_name, Student.first_name)
            .all()
        )
        students_options = [{"id": s.id, "name": s.name, "group": g.name} for s, g in active_rows]

    prev_month = (month_start - timedelta(days=1)).replace(day=1)
    next_month = month_end + timedelta(days=1)

    return templates.TemplateResponse("attendance/journal.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "days": days,
        "absences": absences,
        "students": students_options,
        "reason_labels": REASON_LABELS,
        "month_label": month_anchor.strftime("%m.%Y"),
        "prev_month": prev_month.strftime("%Y-%m"),
        "next_month": next_month.strftime("%Y-%m"),
        "today": today.isoformat(),
        "active_page": "attendance",
    })
