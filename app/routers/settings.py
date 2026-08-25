from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_accessible_orgs, get_current_user, resolve_org
from app.models import Organization, User

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# Первая настройка здесь — кто принимает наличные за разовые услуги
# (25.08, app/routers/services.py). Отдельная страница, а не довесок на
# «Услуги» — по прямому запросу: настройки объекта должны жить своим местом,
# не смешиваться со страницей, где их не ждут (тот же урок, что и с
# разделением форм подотчёта). Видимость — те же роли, что «Подотчёт»
# (podotchet.ALLOWED_ROLES), т.к. эта настройка напрямую определяет, кто там
# становится подотчётным.
ALLOWED_ROLES = ("owner", "founder", "staff", "manager")


def _guard(request: Request, db: Session):
    user = get_current_user(request, db)
    if not user:
        return None, RedirectResponse("/login", status_code=302)
    if user.role not in ALLOWED_ROLES:
        return None, RedirectResponse("/", status_code=302)
    return user, None


@router.get("/", response_class=HTMLResponse)
def settings_page(request: Request, org_id: str | None = None, db: Session = Depends(get_db)):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect

    accessible = get_accessible_orgs(user, db)
    current_org = resolve_org(int(org_id) if org_id and org_id.isdigit() else None, user, db)
    if not current_org:
        return RedirectResponse("/", status_code=302)

    org_users = db.query(User).filter(
        User.organization_id == current_org.id, User.deleted_at.is_(None), User.role != "founder",
    ).order_by(User.name).all()

    return templates.TemplateResponse("settings/index.html", {
        "request": request,
        "current_user": user,
        "accessible_orgs": accessible,
        "current_org_id": current_org.id,
        "current_org": current_org,
        "active_page": "settings",
        "org_users": org_users,
    })


@router.post("/cash-recipient")
def update_cash_recipient(
    request: Request,
    cash_recipient_user_id: str = Form(default=""),
    org_id: str = Form(...),
    db: Session = Depends(get_db),
):
    user, redirect = _guard(request, db)
    if redirect:
        return redirect
    redirect_url = f"/settings/?org_id={org_id}"

    org = db.query(Organization).filter(Organization.id == int(org_id)).first() if org_id.isdigit() else None
    if org:
        org.cash_recipient_user_id = int(cash_recipient_user_id) if cash_recipient_user_id.isdigit() else None
        db.commit()
    return RedirectResponse(redirect_url, status_code=303)
