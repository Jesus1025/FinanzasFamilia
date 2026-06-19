"""Canal Telegram (Bot API oficial de @BotFather).

Un solo bot atiende a TODAS las familias mediante deep linking:
  t.me/BotFinanzas?start=CODIGO_FAMILIA

Cuando una persona abre ese link y pulsa "Start", el webhook recibe
/start CODIGO_FAMILIA y asocia su telegram_chat_id a la familia correcta.

La Bot API es gratuita, no usa navegador ni sesión: no hay riesgo de baneo,
no consume RAM de Chromium y es infalible para responder solo a quien te
escribió primero (un bot NO puede iniciar conversaciones).
"""
from __future__ import annotations

import secrets

import httpx

from ..config import config
from ..logger import get_logger

log = get_logger("telegram")

# 4096 caracteres por mensaje (límite de la Bot API).
_LIMITE_TEXTO = 4096

_bot_info: dict | None = None


def _api(metodo: str) -> str:
    return f"{config.TELEGRAM_API}/bot{config.TELEGRAM_BOT_TOKEN}/{metodo}"


def _trocear(texto: str, limite: int = _LIMITE_TEXTO) -> list[str]:
    texto = texto or ""
    if len(texto) <= limite:
        return [texto]
    partes, resto = [], texto
    while len(resto) > limite:
        corte = resto.rfind("\n", 0, limite)
        if corte < limite // 2:
            corte = limite
        partes.append(resto[:corte])
        resto = resto[corte:].lstrip("\n")
    if resto:
        partes.append(resto)
    return partes


# ---------------------------------------------------------------------------
# Envío de mensajes (usa la app para recordatorios, bienvenidas, alertas)
# ---------------------------------------------------------------------------
async def enviar_texto(chat_id: str | int, mensaje: str) -> bool:
    if not config.TELEGRAM_ENABLED or not (mensaje or "").strip():
        return False
    ok = True
    for parte in _trocear(mensaje):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(_api("sendMessage"), json={
                    "chat_id": chat_id, "text": parte,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                })
                r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("No pude enviar texto a %s: %s", chat_id, e)
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Administracion del bot (estado, webhook, getMe)
# ---------------------------------------------------------------------------
async def get_me() -> dict | None:
    global _bot_info
    if not config.TELEGRAM_ENABLED:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(_api("getMe"))
            r.raise_for_status()
            _bot_info = r.json().get("result") or None
            return _bot_info
    except httpx.HTTPError as e:
        log.info("getMe de Telegram falló: %s", e)
        return None


def bot_username() -> str | None:
    if config.TELEGRAM_BOT_USERNAME:
        return config.TELEGRAM_BOT_USERNAME
    return (_bot_info or {}).get("username")


async def configurar_webhook() -> bool:
    if not config.TELEGRAM_ENABLED:
        return False
    url = f"{config.APP_URL}/webhook/telegram"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(_api("setWebhook"), json={
                "url": url,
                "secret_token": config.telegram_webhook_secret,
                "allowed_updates": ["message"],
                "drop_pending_updates": True,
            })
            r.raise_for_status()
            ok = bool(r.json().get("ok"))
            log.info("setWebhook de Telegram -> %s (%s)", url, ok)
            return ok
    except httpx.HTTPError as e:
        log.warning("No pude configurar el webhook de Telegram: %s", e)
        return False


async def estado() -> dict:
    if not config.TELEGRAM_ENABLED:
        return {"enabled": False, "ok": False}
    me = await get_me()
    webhook_url = None
    pendientes = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(_api("getWebhookInfo"))
            r.raise_for_status()
            info = r.json().get("result") or {}
            webhook_url = info.get("url") or None
            pendientes = info.get("pending_update_count")
    except httpx.HTTPError:
        pass
    return {
        "enabled": True,
        "ok": bool(me),
        "username": (me or {}).get("username"),
        "name": (me or {}).get("first_name"),
        "webhook": webhook_url,
        "pending": pendientes,
    }


# ---------------------------------------------------------------------------
# Códigos de invitación (deep linking por familia)
# ---------------------------------------------------------------------------
def generar_codigo_invitacion() -> str:
    """Código alfanumérico de 12 caracteres, único, para el link t.me/<bot>?start=CODE."""
    return secrets.token_hex(6)


def link_invitacion(codigo: str) -> str | None:
    """Devuelve el deep link t.me/<bot>?start=CODE o None si el bot no está configurado."""
    username = bot_username()
    if not username:
        return None
    return f"https://t.me/{username}?start={codigo}"
