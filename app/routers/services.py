from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import Service

router = APIRouter(prefix="/services", tags=["services"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def service_list(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)

    query = db.query(Service).filter(Service.deleted_at.is_(None))
    if current_org:
        query = query.filter(Service.organization_id == current_org.id)
    services = query.order_by(Service.name).all()

    return templates.TemplateResponse("services/list.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "services": services,
        "active_page": "services",
    })


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
        db.add(Service(organization_id=int(org_id), name=name, price=price_val))
        db.commit()

    return RedirectResponse(f"/services/?org_id={org_id}", status_code=303)


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
    if s:
        s.deleted_at = datetime.utcnow()
        db.commit()
    redirect_url = f"/services/?org_id={org_id}" if org_id else "/services/"
    return RedirectResponse(redirect_url, status_code=303)
