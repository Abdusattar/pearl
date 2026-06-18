from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)):
    users = db.query(User).filter(User.deleted_at.is_(None)).order_by(User.id).all()
    return templates.TemplateResponse("auth/login.html", {
        "request": request,
        "users": users,
    })


@router.post("/login")
def login(request: Request, user_id: int = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/login", status_code=302)
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=302)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
