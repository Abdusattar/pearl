from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_accessible_orgs
from app.models import Supplier

router = APIRouter(prefix="/suppliers", tags=["suppliers"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/", response_class=HTMLResponse)
def supplier_list(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    accessible = get_accessible_orgs(user, db)
    current_org_id = request.query_params.get("org_id")

    suppliers = db.query(Supplier).order_by(Supplier.name).all()
    return templates.TemplateResponse("suppliers/list.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": int(current_org_id) if current_org_id else (accessible[0].id if accessible else None),
        "suppliers": suppliers,
    })


@router.post("/", response_class=HTMLResponse)
def create_supplier(
    request: Request,
    name: str = Form(...),
    phone: str = Form(default=""),
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    name = name.strip()
    phone_val = phone.strip() or None

    if name:
        existing = db.query(Supplier).filter(Supplier.name == name).first()
        if not existing:
            s = Supplier(name=name, phone=phone_val)
            db.add(s)
            db.commit()

    redirect_url = f"/suppliers/?org_id={org_id}" if org_id else "/suppliers/"
    return RedirectResponse(redirect_url, status_code=303)


@router.post("/{supplier_id}/delete")
def delete_supplier(
    supplier_id: int,
    request: Request,
    org_id: str = Form(default=""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    s = db.query(Supplier).get(supplier_id)
    if s:
        db.delete(s)
        db.commit()
    redirect_url = f"/suppliers/?org_id={org_id}" if org_id else "/suppliers/"
    return RedirectResponse(redirect_url, status_code=303)
