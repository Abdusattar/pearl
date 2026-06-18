import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

app = FastAPI(title="Жемчужина ИС")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "pearl-dev-secret"))

MEDIA_DIR = Path(__file__).parent.parent / "media"
MEDIA_DIR.mkdir(exist_ok=True)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

from app.routers import expenses, students, optima, auth, income
app.include_router(auth.router)
app.include_router(expenses.router)
app.include_router(students.router)
app.include_router(income.router)
app.include_router(optima.router)


@app.get("/")
def root():
    return RedirectResponse("/expenses/")
