"""Бот: вебхук Telegram, тик расписания, страница привязки людей (блок 7)."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.dependencies import get_current_user
from app.models import BotMessage, User
from app.routers.new_buy import _base_ctx, _site, templates
from app.services import bot as svc

router = APIRouter(tags=["bot"])


@router.post("/bot/webhook/{secret}")
async def webhook(secret: str, request: Request, db: Session = Depends(get_db)):
    if secret != svc.webhook_secret():
        return JSONResponse({"ok": False}, status_code=403)
    try:
        update = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True})
    try:
        svc.handle_update(db, update)
        db.commit()
    except Exception as e:  # noqa: BLE001 — Telegram будет повторять, лучше ответить 200 и записать
        db.rollback()
        db.add(BotMessage(kind="error", direction="in", status="failed", text=str(e)[:500]))
        db.commit()
    return JSONResponse({"ok": True})


@router.post("/bot/tick/{secret}")
def tick(secret: str, db: Session = Depends(get_db)):
    """Внешний будильник (на случай, если фоновый цикл в контейнере не жив)."""
    if secret != svc.webhook_secret():
        return JSONResponse({"ok": False}, status_code=403)
    sent = svc.run_scheduled(db)
    db.commit()
    return JSONResponse({"ok": True, "sent": sent})


async def scheduler_loop():
    """Раз в минуту проверяет расписание. Запускается при старте приложения."""
    while True:
        try:
            db = SessionLocal()
            try:
                svc.run_scheduled(db, datetime.now())
                db.commit()
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — цикл не должен умирать от одной ошибки
            pass
        await asyncio.sleep(60)


@router.get("/new/settings/bot", response_class=HTMLResponse)
def bot_settings(request: Request, sent: int = 0, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role != "owner":
        return HTMLResponse("Только владелец", status_code=403)
    site = _site(user, db)
    ctx = _base_ctx(request, user, site, db, "settings")
    users = db.query(User).filter(User.deleted_at.is_(None)).order_by(User.id).all()
    last = db.query(BotMessage).order_by(BotMessage.id.desc()).limit(15).all()
    ctx.update({"users": users, "has_token": bool(svc.token()), "group_id": svc.group_chat_id(),
                "secret": svc.webhook_secret(), "last": last, "sent": sent,
                "preview_group": svc.group_signals_text(db, site.id) if site else None,
                "preview_summary": svc.founders_summary_text(db, site.id) if site else None,
                "base_url": os.getenv("PUBLIC_BASE_URL", str(request.base_url).rstrip("/"))})
    return templates.TemplateResponse("new/settings_bot.html", ctx)


@router.post("/new/settings/bot")
async def bot_settings_save(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role != "owner":
        return HTMLResponse("Только владелец", status_code=403)
    form = await request.form()
    action = form.get("action") or "save"
    if action == "save":
        for u in db.query(User).filter(User.deleted_at.is_(None)).all():
            raw = (form.get(f"tg_{u.id}") or "").strip()
            u.tg_id = int(raw) if raw.lstrip("-").isdigit() else None
        db.commit()
        return RedirectResponse("/new/settings/bot", status_code=303)
    site = _site(user, db)
    if action == "test_group":
        svc.send(db, svc.group_chat_id(), "Проверка связи: бот «Жемчужина» подключён.", "test")
    elif action == "test_me":
        svc.send(db, user.tg_id, "Проверка связи: это ваша личка с ботом «Жемчужина».", "test", user_id=user.id)
    elif action == "send_group_now" and site:
        text = svc.group_signals_text(db, site.id) or "Сигналов нет."
        svc.send(db, svc.group_chat_id(), text, "group_signals")
    elif action == "webhook":
        base = os.getenv("PUBLIC_BASE_URL", str(request.base_url).rstrip("/"))
        res = svc.set_webhook(base)
        db.add(BotMessage(kind="webhook", status="sent" if res.get("ok") else "failed", text=str(res)[:500]))
    elif action == "run_now":
        svc.run_scheduled(db, datetime.now())
    db.commit()
    return RedirectResponse("/new/settings/bot?sent=1", status_code=303)
