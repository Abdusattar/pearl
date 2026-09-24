"""Бот: вебхук Telegram, тик расписания, страница привязки людей (блок 7)."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.dependencies import get_current_user
from app.models import BotMessage, User
from app.routers.new_buy import _base_ctx, _site, templates
from app.services import bot as svc

router = APIRouter(tags=["bot"])


@router.post("/bot/webhook/{secret}")
async def webhook(secret: str, request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    if secret != svc.webhook_secret():
        return JSONResponse({"ok": False}, status_code=403)
    try:
        update = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True})
    try:
        # разбор фото моделью идёт секунды — не держим цикл событий
        await run_in_threadpool(svc.handle_update, db, update)
        db.commit()
        if (update.get("message") or {}).get("photo") or (update.get("message") or {}).get("document"):
            # строки листа кухни — сразу, фоном, после ответа Telegram (черновик готов к открытию)
            from app.services.drafts import prepare_pending
            background.add_task(prepare_pending)
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
        try:
            # листы кухни, что пришли без разбора (модель была занята, сервер перезапускался)
            from app.services.drafts import prepare_pending
            await run_in_threadpool(prepare_pending)
        except Exception:  # noqa: BLE001
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
    ctx.update({"users": users, "unknown": svc.unknown_senders(db),
                "has_token": bool(svc.token()), "group_id": svc.group_chat_id(),
                "secret": svc.webhook_secret(), "last": last, "sent": sent,
                "preview_group": svc.group_signals_text(db, site.id) if site else None,
                "preview_summary": svc.founders_summary_text(db, site.id) if site else None,
                "base_url": os.getenv("PUBLIC_BASE_URL", str(request.base_url).rstrip("/"))})
    return templates.TemplateResponse("new/settings_bot.html", ctx)


@router.get("/new/settings/bot/chat", response_class=HTMLResponse)
def bot_chat(request: Request, who: str | None = None, db: Session = Depends(get_db)):
    """Переписка бота как чат — только владельцу (24.09: «плохо, что я не вижу общения
    с ботом в личке»). Личка каждого человека и группа; входящие слева, бот справа."""
    user = get_current_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=302)
    if user.role != "owner":
        return HTMLResponse("Только владелец", status_code=403)
    site = _site(user, db)
    ctx = _base_ctx(request, user, site, db, "settings")
    users = db.query(User).filter(User.deleted_at.is_(None)).order_by(User.id).all()
    names = {u.id: u.name for u in users}
    group = svc.group_chat_id()
    SKIP = ("webhook", "test", "group_threshold", "group_error")
    rows = (db.query(BotMessage).filter(BotMessage.kind.notin_(SKIP), BotMessage.status != "skipped")
            .order_by(BotMessage.id.desc()).limit(600).all())
    items, counts = [], {}
    for m in reversed(rows):
        is_group = group is not None and m.chat_id == group
        # «inbound» дублирует разбор того же сообщения (group_text, stock_text…) — показываем один раз
        if m.direction == "in" and m.kind != "inbound" and m.kind != "voice":
            continue
        key = "group" if is_group else m.user_id
        counts[key] = counts.get(key, 0) + 1
        if who and str(key) != who:
            continue
        p = m.payload or {}
        text = m.text or ""
        if not text and m.direction == "in":
            text = {"photo": "📷 фото", "document": "📎 файл", "voice": "🎤 голосовое", "file": "📎 файл"}.get(p.get("media") or "", "…")
            if m.kind == "voice":
                text = "🎤 " + (m.text or "")
        note = ""
        if m.direction == "in" and (p.get("reply") or p.get("what")):
            note = "бот понял: " + (p.get("reply") or p.get("what"))[:160]
        elif m.direction == "out" and m.kind == "group_reply_owner":
            note = "копия вам"
        items.append({"dir": m.direction, "text": text, "status": m.status, "group": is_group,
                      "who": names.get(m.user_id) or p.get("from_name") or ("группа" if is_group else "?"),
                      "day": m.created_at.strftime("%d.%m.%Y") if m.created_at else "",
                      "time": m.created_at.strftime("%H:%M") if m.created_at else "", "note": note})
    ctx.update({"items": items, "users": [u for u in users if u.tg_id], "counts": counts, "sel": who})
    return templates.TemplateResponse("new/bot_chat.html", ctx)


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
    if action == "link":
        # Непривязанный отправитель из группы → человек в системе (23.09): одна кнопка,
        # без «напишите боту свой номер»
        u = db.get(User, int(form.get("user_id") or 0))
        raw = (form.get("from_id") or "").strip()
        if u is not None and raw.lstrip("-").isdigit():
            u.tg_id = int(raw)
            db.query(BotMessage).filter(BotMessage.user_id.is_(None), BotMessage.direction == "in",
                                        BotMessage.payload["from_id"].as_string() == raw).update(
                {BotMessage.user_id: u.id}, synchronize_session=False)
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
