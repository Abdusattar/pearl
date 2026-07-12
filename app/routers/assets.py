from datetime import date as date_type, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs, resolve_org
from app.models import Asset
from app.services.unit_economics import DEFAULT_USEFUL_LIFE_MONTHS, asset_monthly_amortization, monthly_amortization

router = APIRouter(prefix="/assets", tags=["assets"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

CATEGORIES = ["мебель", "оборудование", "игровой инвентарь", "прочее"]


@router.get("/", response_class=HTMLResponse)
def asset_list(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role == "staff":
        return RedirectResponse("/", status_code=302)

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)
    assets = (
        db.query(Asset)
        .filter(Asset.organization_id == current_org.id, Asset.deleted_at.is_(None))
        .order_by(Asset.purchase_date.desc())
        .all()
        if current_org else []
    )
    total_cost = sum(float(a.cost) for a in assets)
    amortization_by_id = {a.id: asset_monthly_amortization(a) for a in assets}
    total_amortization = monthly_amortization(db, current_org.id) if current_org else 0

    return templates.TemplateResponse("assets/list.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id if current_org else None,
        "assets": assets,
        "total_cost": total_cost,
        "amortization_by_id": amortization_by_id,
        "total_amortization": total_amortization,
        "categories": CATEGORIES,
        "default_life": DEFAULT_USEFUL_LIFE_MONTHS,
        "today": date_type.today().isoformat(),
        "active_page": "assets",
    })


@router.post("/", response_class=HTMLResponse)
def create_asset(
    request: Request,
    org_id: str = Form(...),
    name: str = Form(...),
    description: str = Form(default=""),
    category: str = Form(default="прочее"),
    purchase_date: str = Form(...),
    cost: float = Form(...),
    useful_life_months: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role == "staff":
        return RedirectResponse("/", status_code=302)

    life = int(useful_life_months) if useful_life_months.strip() else DEFAULT_USEFUL_LIFE_MONTHS.get(category)
    asset = Asset(
        name=name.strip(),
        description=description.strip() or None,
        category=category,
        purchase_date=date_type.fromisoformat(purchase_date),
        cost=cost,
        useful_life_months=life,
        organization_id=int(org_id),
        created_by=user.id,
    )
    db.add(asset)
    db.commit()
    return RedirectResponse(f"/assets/?org_id={org_id}", status_code=303)


@router.post("/{asset_id}/delete")
def delete_asset(
    asset_id: int,
    request: Request,
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role == "staff":
        return RedirectResponse("/", status_code=302)
    asset = db.query(Asset).get(asset_id)
    if asset:
        asset.deleted_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(f"/assets/?org_id={org_id}", status_code=303)
