from datetime import date as date_cls
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import Employee
from app.services import payroll as payroll_service, podotchet
from app.services.unit_economics import monthly_payroll

router = APIRouter(prefix="/employees", tags=["employees"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _guard(request: Request, db: Session):
    """Оклады — чувствительные данные, доступ только owner/founder/staff
    (не Мунаре/Айжан) — решено 10.07. Махабат (role=staff) добавлена 27.07:
    она главный учётчик Сокулука и уже знает зарплаты сотрудников на практике,
    секретность от неё смысла не имеет — пусть ведёт ведомость сама.
    Мунара (role=manager) добавлена 07.09: получалась инверсия — оператор видел
    оклады, а управляющая садиками, у которой деньги на руках, нет."""
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=302)
    if user.role not in ("owner", "founder", "staff", "manager"):
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


@router.get("/payroll", response_class=HTMLResponse)
def payroll_sheet(request: Request, org_id: str | None = None, month: str | None = None,
                  db: Session = Depends(get_db)):
    """Ведомость выдачи за месяц — по строке на сотрудника.

    Заменяет проводку «ФОТ» одной суммой на всех: по ней не видно, кто сколько
    получил и кому ещё должны, а выдают по факту (аванс, часть, неполный месяц)."""
    user, redirect = _guard(request, db)
    if redirect:
        return redirect

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)
    if not current_org:
        return RedirectResponse("/employees/", status_code=302)

    try:
        period = date_cls.fromisoformat(month) if month else payroll_service.month_start()
    except ValueError:
        period = payroll_service.month_start()
    period = period.replace(day=1)

    sheet = payroll_service.month_sheet(db, current_org.id, period)
    return templates.TemplateResponse("employees/payroll.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id,
        "current_org_name": current_org.name,
        "sheet": sheet,
        "totals": payroll_service.totals(sheet),
        "period": period,
        "today": date_cls.today().isoformat(),
        # Выдавать можно только те деньги, что реально есть в кассе объекта —
        # иначе касса уходит в минус молча, как это уже случилось (07.09).
        "cash": podotchet.get_org_balance(db, current_org.id),
        "active_page": "employees",
    })


@router.post("/payroll")
def pay_salaries(
    request: Request,
    org_id: str = Form(...),
    month: str = Form(...),
    pay_date: str = Form(default=""),
    employee_id: list[str] = Form(default=[]),
    amount: list[str] = Form(default=[]),
    db: Session = Depends(get_db),
):
    """Провести выдачу: по одному расходу на каждого, кому вписали сумму."""
    user, redirect = _guard(request, db)
    if redirect:
        return redirect

    back = f"/employees/payroll?org_id={org_id}&month={month}"
    try:
        period = date_cls.fromisoformat(month).replace(day=1)
        on_date = date_cls.fromisoformat(pay_date) if pay_date else date_cls.today()
    except ValueError:
        return RedirectResponse(f"{back}&error={quote('Неверная дата')}", status_code=303)

    org_id_int = int(org_id)
    employees = {
        e.id: e for e in db.query(Employee).filter(
            Employee.organization_id == org_id_int, Employee.status == "active"
        ).all()
    }

    to_pay: list[tuple[Employee, Decimal]] = []
    for i, emp_str in enumerate(employee_id):
        if not emp_str.isdigit() or int(emp_str) not in employees:
            continue
        raw = (amount[i] if i < len(amount) else "").strip().replace(" ", "").replace(",", ".")
        if not raw:
            continue
        try:
            value = Decimal(raw)
        except InvalidOperation:
            continue
        if value > 0:
            to_pay.append((employees[int(emp_str)], value))

    if not to_pay:
        return RedirectResponse(f"{back}&error={quote('Не указано ни одной суммы')}", status_code=303)

    total = sum((v for _, v in to_pay), Decimal(0))
    cash = podotchet.get_org_balance(db, org_id_int)
    if total > cash:
        msg = f"В кассе {cash:,.2f} с, а выдаёте {total:,.2f} с — сначала заведите снятие со счёта"
        return RedirectResponse(f"{back}&error={quote(msg)}", status_code=303)

    for employee, value in to_pay:
        payroll_service.pay(db, organization_id=org_id_int, employee=employee,
                            amount=value, period=period, on_date=on_date, user_id=user.id)
    db.commit()
    return RedirectResponse(back, status_code=303)


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


@router.get("/add", response_class=HTMLResponse)
def employee_add_form(request: Request, org_id: str | None = None, saved: str | None = None,
                       db: Session = Depends(get_db)):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect

    org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)
    return templates.TemplateResponse("employees/add.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": get_accessible_orgs(user, db),
        "current_org_id": org.id if org else None,
        "saved": saved,
        "active_page": "employees",
    })


@router.post("/add", response_class=HTMLResponse)
def employee_add(
    request: Request,
    org_id: str | None = Form(None),
    full_name: str = Form(...),
    role: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect

    # resolve_org фильтрует через get_accessible_orgs — staff не может
    # подсунуть чужой org_id, даже если поменяет его в запросе руками.
    org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)
    if not org:
        return RedirectResponse("/employees/add", status_code=303)

    db.add(Employee(
        organization_id=org.id, full_name=full_name.strip(),
        role=role.strip() or None, salary=0, status="active",
        created_by=user.id,
    ))
    db.commit()
    return RedirectResponse(f"/employees/add?org_id={org.id}&saved=1", status_code=303)


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
