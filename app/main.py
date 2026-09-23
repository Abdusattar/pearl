import os
from pathlib import Path
from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.database import get_db
from app.dependencies import get_current_user

app = FastAPI(title="Жемчужина ИС")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "pearl-dev-secret"))


@app.middleware("http")
async def _remember_target(request: Request, call_next):
    # Ссылку, присланную в чат (23.09: «Дети» Школы для Айжан), после входа
    # открываем там, куда она вела, а не на «Сегодня». Роутов с переходом на
    # /login больше сотни — запоминаем здесь, в одном месте.
    response = await call_next(request)
    if (request.method == "GET" and response.status_code in (302, 307)
            and response.headers.get("location") == "/login" and request.url.path != "/"):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        response.set_cookie("next", target, max_age=900, httponly=True, samesite="lax")
    return response

MEDIA_DIR = Path(__file__).parent.parent / "media"
MEDIA_DIR.mkdir(exist_ok=True)
app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

from app.routers import expenses, students, optima, auth, income, warehouse, suppliers, services, reports, assets, attendance, menu, employees, dashboard, podotchet, settings, stock_count, new_buy, new_kitchen, new_today, new_expenses, new_cash, new_children, new_overview, new_bot, new_salary, new_stock, new_settings
app.include_router(auth.router)
app.include_router(new_overview.router)
app.include_router(new_bot.router)


@app.on_event("startup")
async def _start_bot_scheduler():
    # Расписание бота (блок 7): раз в минуту смотрит, что пора отправить.
    # Без токена всё пишется только в журнал. Выключается BOT_SCHEDULER=0.
    import asyncio
    if os.getenv("BOT_SCHEDULER", "1") == "1":
        asyncio.create_task(new_bot.scheduler_loop())
# Новый вход (16.09): та же база, другая поверхность, по другой ссылке.
app.include_router(new_buy.router)
app.include_router(new_kitchen.router)
app.include_router(new_today.router)
app.include_router(new_expenses.router)
app.include_router(new_cash.router)
app.include_router(new_children.router)
app.include_router(new_salary.router)
app.include_router(new_stock.router)
app.include_router(new_settings.router)
app.include_router(expenses.router)
app.include_router(students.router)
app.include_router(income.router)
# Раньше warehouse.router — у него prefix="/warehouse", и без этого порядка
# /warehouse/count/ мог бы перехватиться его же маршрутами.
app.include_router(stock_count.router)
app.include_router(warehouse.router)
app.include_router(optima.router)
app.include_router(suppliers.router)
app.include_router(services.router)
app.include_router(reports.router)
app.include_router(assets.router)
app.include_router(attendance.router)
app.include_router(menu.router)
app.include_router(employees.router)
app.include_router(dashboard.router)
app.include_router(podotchet.router)
app.include_router(settings.router)


@app.get("/")
def root(request: Request, db: Session = Depends(get_db)):
    # Вход — новая версия (решение владельца 17.09, переключено 18.09):
    # собственникам Обзор, остальным «Сегодня». Старая — только по ссылке.
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login")
    return RedirectResponse("/new/overview" if user.role in ("owner", "founder") else "/new/today")
