from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import Organization, Student, Group, Enrollment
from app.services.students import archive_stale_students
from app.services.billing import generate_monthly_charges, get_balances

router = APIRouter(prefix="/reports", tags=["reports"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def debtors_report(
    request: Request,
    org_id: str | None = None,
    group_id: str | None = None,
    only_debtors: bool = False,
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

    all_orgs = db.query(Organization).all()
    def descendants(oid):
        ids = {oid}
        for o in all_orgs:
            if o.parent_id == oid:
                ids |= descendants(o.id)
        return ids

    org_ids = descendants(current_org.id) if current_org else {o.id for o in accessible}

    query = db.query(Student).filter(Student.status == "active", Student.organization_id.in_(org_ids))

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

    balances = get_balances(db, [s.id for s in students])

    if only_debtors:
        students = [s for s in students if balances.get(s.id, 0) > 0]

    available_groups = (
        db.query(Group)
        .filter(Group.organization_id.in_(org_ids))
        .order_by(Group.name)
        .all()
    )

    total_debt = sum(b for b in balances.values() if b > 0)
    total_overpaid = sum(-b for b in balances.values() if b < 0)

    return templates.TemplateResponse("reports/debtors.html", {
        "request": request,
        "students": students,
        "groups_by_student": groups_by_student,
        "balances": balances,
        "available_groups": available_groups,
        "current_group_id": group_id_int,
        "only_debtors": only_debtors,
        "total_debt": total_debt,
        "total_overpaid": total_overpaid,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "current_user": user,
        "active_page": "reports",
    })
