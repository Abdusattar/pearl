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


# Пилот только на Садике Сокулук (решено 08.07, реально закреплено в коде 12.07 —
# до этого owner/founder технически видели все объекты, дыра в решении).
# Ограничены конкретные тестировщики по user_id, не роль целиком — иначе
# реальный владелец (Талас/Абдусаттар после снятия пилота) тоже терял бы
# доступ к Школе/Кожомкулу на проде. Убрать запись из PILOT_USER_IDS, когда
# конкретный человек больше не участвует в пилоте (13.07).
PILOT_ORG_IDS = {4}
PILOT_USER_IDS = {1, 3, 61, 64}  # Абдусаттар, Мунара, Айдай, Талас


def get_accessible_orgs(user: User, db: Session) -> list[Organization]:
    all_orgs = db.query(Organization).all()
    if user.role == "director":
        # Айжан — директор школы, видит только свой объект (Школа), не садики
        return [o for o in all_orgs if o.id == user.organization_id]
    if user.id in PILOT_USER_IDS:
        return [o for o in all_orgs if o.id in PILOT_ORG_IDS]
    if user.role in ("owner", "founder", "manager"):
        return all_orgs
    return [o for o in all_orgs if o.id == user.organization_id]


def resolve_org(org_id: int | None, user: User, db: Session) -> Organization:
    accessible = get_accessible_orgs(user, db)
    if org_id:
        org = next((o for o in accessible if o.id == org_id), None)
        if org:
            return org
    # Без явного org_id в ссылке (большинство пунктов меню) — сначала пробуем
    # "родной" объект пользователя (user.organization_id), а не первый
    # попавшийся в списке. Раньше падало на accessible[0] всегда — для
    # owner/founder/director это буквально первая организация в таблице
    # (Жемчужина, не листовая), что молча расходилось с тем, что реально
    # назначено пользователю. Баг найден 12.07, когда organization_id
    # тестировщиков перевели на Садик Сокулук, а навигация продолжала
    # молча показывать другое.
    own = next((o for o in accessible if o.id == user.organization_id), None)
    if own:
        return own
    return accessible[0] if accessible else None
