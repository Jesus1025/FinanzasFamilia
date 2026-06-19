"""Aplicacion FastAPI: webhook de Telegram + dashboard web."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import scheduler
from .config import config
from .db import init_db
from .logger import get_logger
from .routers import admin, api, auth_google, chat, dashboard, webhook

log = get_logger("main")
_BASE = os.path.dirname(os.path.dirname(__file__))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    await _configurar_telegram()
    log.info("Base de datos lista. DeepSeek=%s. Telegram=%s. App en %s",
             "ON" if config.DEEPSEEK_ENABLED else "OFF (fallback)",
             "ON" if config.TELEGRAM_ENABLED else "OFF",
             config.APP_URL)
    tarea = asyncio.create_task(scheduler.loop())
    try:
        yield
    finally:
        tarea.cancel()


async def _configurar_telegram() -> None:
    if not config.TELEGRAM_ENABLED:
        return
    from .services import telegram as tg
    try:
        me = await tg.get_me()
        if not me:
            log.warning("Telegram: el token no respondió a getMe.")
            return
        if config.APP_URL.startswith("https://") and "localhost" not in config.APP_URL:
            await tg.configurar_webhook()
        else:
            log.info("Telegram: APP_URL no es https pública (%s); no registro webhook.", config.APP_URL)
    except Exception as e:
        log.warning("No pude configurar Telegram al arrancar: %s", e)


app = FastAPI(title="Finanzas Familia", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY)

app.include_router(webhook.router)
app.include_router(dashboard.router)
app.include_router(api.router)
app.include_router(admin.router)
app.include_router(chat.router)
app.include_router(auth_google.router)

_static_dir = os.path.join(_BASE, "static")
os.makedirs(_static_dir, exist_ok=True)


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # Servido desde la raíz para que el scope del Service Worker cubra todo el sitio.
    return FileResponse(os.path.join(_static_dir, "sw.js"), media_type="application/javascript")


app.mount("/static", StaticFiles(directory=_static_dir), name="static")
