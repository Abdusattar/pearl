from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import Service, Organization, ServicePriceHistory, User

router = APIRouter(prefix="/services", tags=["services"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Менять уже установленную цену услуги — по прямому запросу Абдусаттара (13.07,
# расширено 22.07 — временно добавлены сам Абдусаттар и Махабат). Заводить новую
# услугу с ценой (создание) — не ограничено, только правка существующей.
# id проверены напрямую по прод-БД (SELECT id, role FROM users) — реальные:
# 1 Абдусаттар (owner), 4 Махабат (staff), 5 Айдай (founder), 6 Талас (owner).
PRICE_EDITORS = {1, 4, 5, 6}


@router.get("/", response_class=HTMLResponse)
def service_list(request: Request, org_id: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)

    query = db.query(Service).filter(Service.deleted_at.is_(None))
    if current_org:
        query = query.filter(Service.organization_id == current_org.id)
    services = query.order_by(Service.is_tuition.desc(), Service.name).all()

    price_history = {}
    if services:
        rows = (
            db.query(ServicePriceHistory, User.name)
            .outerjoin(User, User.id == ServicePriceHistory.changed_by)
            .filter(ServicePriceHistory.service_id.in_([s.id for s in services]))
            .order_by(ServicePriceHistory.effective_date.desc(), ServicePriceHistory.changed_at.desc())
            .all()
        )
        for h, user_name in rows:
            price_history.setdefault(h.service_id, []).append({
                "date": h.effective_date.strftime("%d.%m.%Y"),
                "price": f"{float(h.price):,.0f}".replace(",", " "),
                "who": user_name or "— (при создании)",
                "when": h.changed_at.strftime("%d.%m %H:%M") if h.changed_at else "",
            })

    return templates.TemplateResponse("services/list.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "current_org": current_org,
        "services": services,
        "price_history": price_history,
        "today": date.today().isoformat(),
        "active_page": "services",
        "can_edit_price": user.id in PRICE_EDITORS,
        "error": error,
    })


@router.post("/frozen-percent")
def update_frozen_percent(
    request: Request,
    percent: str = Form(...),
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    base_url = f"/services/?org_id={org_id}" if org_id else "/services/"
    if user.id not in PRICE_EDITORS:
        sep = "&" if "?" in base_url else "?"
        msg = quote("Менять этот процент может только Айдай или Талас")
        return RedirectResponse(f"{base_url}{sep}error={msg}", status_code=303)
    org = db.query(Organization).get(int(org_id)) if org_id.isdigit() else None
    try:
        percent_val = float(percent)
    except ValueError:
        percent_val = None
    if org and percent_val is not None and 0 <= percent_val <= 100:
        org.frozen_discount_percent = percent_val
        db.commit()
    return RedirectResponse(base_url, status_code=303)


@router.post("/", response_class=HTMLResponse)
def create_service(
    request: Request,
    name: str = Form(...),
    price: str = Form(...),
    org_id: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)

    name = name.strip()
    try:
        price_val = float(price)
    except ValueError:
        price_val = 0

    if name and price_val > 0:
        service = Service(organization_id=int(org_id), name=name, price=price_val)
        db.add(service)
        db.flush()
        db.add(ServicePriceHistory(
            service_id=service.id, price=price_val,
            effective_date=date.today(), changed_by=user.id,
        ))
        db.commit()

    return RedirectResponse(f"/services/?org_id={org_id}", status_code=303)


@router.post("/{service_id}/price")
def update_service_price(
    service_id: int,
    request: Request,
    price: str = Form(...),
    effective_date: str = Form(""),
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    base_url = f"/services/?org_id={org_id}" if org_id else "/services/"
    if user.id not in PRICE_EDITORS:
        sep = "&" if "?" in base_url else "?"
        msg = quote("Менять цену может только Айдай или Талас")
        return RedirectResponse(f"{base_url}{sep}error={msg}", status_code=303)
    s = db.query(Service).get(service_id)
    try:
        price_val = float(price)
    except ValueError:
        price_val = 0
    try:
        eff_date = date.fromisoformat(effective_date) if effective_date else date.today()
    except ValueError:
        eff_date = date.today()
    if s and price_val > 0:
        s.price = price_val
        db.add(ServicePriceHistory(
            service_id=s.id, price=price_val,
            effective_date=eff_date, changed_by=user.id,
        ))
        db.commit()
    return RedirectResponse(base_url, status_code=303)


@router.post("/{service_id}/delete")
def delete_service(
    service_id: int,
    request: Request,
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    s = db.query(Service).get(service_id)
    # «Обучение» — базовый тариф, а не опциональная услуга; удаление обнулило
    # бы начисление учёбы всем детям объекта молча. Не удаляем.
    if s and not s.is_tuition:
        s.deleted_at = datetime.utcnow()
        db.commit()
    redirect_url = f"/services/?org_id={org_id}" if org_id else "/services/"
    return RedirectResponse(redirect_url, status_code=303)
