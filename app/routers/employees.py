from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import Employee
from app.services.unit_economics import monthly_payroll

router = APIRouter(prefix="/employees", tags=["employees"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _guard(request: Request, db: Session):
    """Оклады — чувствительные данные, доступ только owner/founder
    (не Мунаре/Махабат/Айжан) — решено 10.07."""
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=302)
    if user.role not in ("owner", "founder"):
        return None, RedirectResponse("/", status_code=302)
    return user, None


@router.get("/", response_class=HTMLResponse)
def employee_list(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)
    employees = (
        db.query(Employee)
        .filter(Employee.organization_id == current_org.id)
        .order_by(Employee.status, Employee.full_name)
        .all()
        if current_org else []
    )
    payroll = monthly_payroll(db, current_org.id) if current_org else 0
    active_count = sum(1 for e in employees if e.status == "active")

    return templates.TemplateResponse("employees/list.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "employees": employees,
        "payroll": payroll,
        "active_count": active_count,
        "active_page": "employees",
    })


@router.post("/", response_class=HTMLResponse)
def create_employee(
    request: Request,
    org_id: str = Form(...),
    full_name: str = Form(...),
    role: str = Form(default=""),
    salary: float = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect

    db.add(Employee(
        organization_id=int(org_id), full_name=full_name.strip(),
        role=role.strip() or None, salary=salary, status="active",
        created_by=user.id,
    ))
    db.commit()
    return RedirectResponse(f"/employees/?org_id={org_id}", status_code=303)


@router.get("/{employee_id}/edit", response_class=HTMLResponse)
def edit_employee_form(employee_id: int, request: Request, saved: str | None = None, db: Session = Depends(get_db)):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect

    employee = db.get(Employee, employee_id)
    if not employee:
        return RedirectResponse("/employees/", status_code=302)
    accessible = get_accessible_orgs(user, db)

    return templates.TemplateResponse("employees/edit.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": employee.organization_id,
        "employee": employee,
        "saved": saved,
        "active_page": "employees",
    })


@router.post("/{employee_id}/edit", response_class=HTMLResponse)
def edit_employee(
    employee_id: int,
    request: Request,
    full_name: str = Form(...),
    role: str = Form(default=""),
    salary: float = Form(...),
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect

    employee = db.get(Employee, employee_id)
    if not employee:
        return RedirectResponse("/employees/", status_code=302)

    employee.full_name = full_name.strip()
    employee.role = role.strip() or None
    employee.salary = salary
    employee.status = status
    db.commit()
    return RedirectResponse(f"/employees/{employee_id}/edit?saved=1", status_code=303)
