from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.models import Organization, User


def get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id, User.deleted_at.is_(None)).first()


def require_user(request: Request, db: Session) -> User | RedirectResponse:
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    return user


def get_accessible_orgs(user: User, db: Session) -> list[Organization]:
    all_orgs = db.query(Organization).all()
    if user.role in ("owner", "director"):
        return all_orgs
    if user.role == "manager":
        kinder_ids = {3}
        kinder_ids |= {o.id for o in all_orgs if o.parent_id == 3}
        return [o for o in all_orgs if o.id in kinder_ids]
    return [o for o in all_orgs if o.id == user.organization_id]


def resolve_org(org_id: int | None, user: User, db: Session) -> Organization:
    accessible = get_accessible_orgs(user, db)
    if org_id:
        org = next((o for o in accessible if o.id == org_id), None)
        if org:
            return org
    return accessible[0] if accessible else None
