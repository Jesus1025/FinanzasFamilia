"""Webhook de Telegram: recibe mensajes del bot y responde con el asistente IA.

El puente WhatsApp está en desuso (reemplazado por Telegram).
Se conserva el endpoint /bridge por retrocompatibilidad mientras migran los usuarios.
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import config
from ..db import get_db
from ..logger import get_logger
from ..models import Household, PendingUser, User, WaMessage
from ..services import asistente, finanzas, telegram as tg

log = get_logger("webhook")
router = APIRouter(prefix="/webhook", tags=["webhook"])


# ---------------------------------------------------------------------------
# Telegram webhook (canal principal)
# ---------------------------------------------------------------------------
@router.post("/telegram")
async def telegram_webhook(
    request: Request,
    payload: dict,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Webhook oficial de la Bot API de Telegram.

    Verifica el secret_token configurado en setWebhook, deduplica por update_id
    y procesa en background para responder rápido (Telegram exige <10 s o retry).
    """
    # Verificar token secreto (viene como header X-Telegram-Bot-Api-Secret-Token)
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not config.TELEGRAM_ENABLED or secret != config.telegram_webhook_secret:
        return JSONResponse({"ok": False}, status_code=403)

    update = payload
    msg = update.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    text = (msg.get("text") or "").strip()
    update_id = update.get("update_id")

    if not chat_id or not text:
        return JSONResponse({"ok": True})

    # Deduplicación: Telegram puede reenviar el mismo update.
    from ..db import engine
    existente = db.scalar(
        select(WaMessage).where(WaMessage.wa_message_id == f"tg-{update_id}")
    )
    if existente:
        return JSONResponse({"ok": True})
    db.add(WaMessage(wa_message_id=f"tg-{update_id}", direction="in",
                     phone=chat_id, body=text))
    db.commit()

    # Despachar en background para responder dentro de los 10 s de Telegram.
    background.add_task(_procesar_telegram, chat_id, text, msg, update_id)
    return JSONResponse({"ok": True})


async def _procesar_telegram(chat_id: str, text: str, msg: dict, update_id: int | None) -> None:
    """Procesa el mensaje: vincula /start, o responde con el asistente IA."""
    from ..db import SessionLocal

    db = SessionLocal()
    try:
        user = db.scalars(select(User).where(User.telegram_chat_id == chat_id)).first()

        # --- Comando /start con código de invitación ---
        if text.startswith("/start"):
            codigo = text.removeprefix("/start").strip()
            if user:
                # Ya vinculado: solo saludar.
                nombre_bot = (user.household.assistant_name or "").strip() or "Fin"
                await tg.enviar_texto(chat_id,
                    f"👋 Hola de nuevo {user.name.split(' ')[0]}, soy *{nombre_bot}*.\n"
                    f"Ya estás conectado a *{user.household.name}*. "
                    f"Escribime natural: \"gasté 5 lucas en café\".")
            elif codigo:
                # Buscar la familia por código de invitación.
                hh = db.scalars(select(Household).where(Household.invite_code == codigo)).first()
                if hh:
                    user = _vincular_o_crear(db, chat_id, msg, hh)
                    nombre_bot = (hh.assistant_name or "").strip() or "Fin"
                    await tg.enviar_texto(chat_id,
                        f"👋 ¡Te di de alta en *{hh.name}*!\n\n"
                        f"Soy *{nombre_bot}*, tu asistente de finanzas. "
                        f"Escribime normal:\n"
                        f"• \"gasté 15 lucas en bencina\"\n"
                        f"• \"pagué 38.000 en el super y 6 lucas de café\"\n"
                        f"• \"¿cuánto llevo gastado este mes?\"\n\n"
                        f"¡Registra tus gastos y yo te ayudo! 🪙")
                else:
                    await tg.enviar_texto(chat_id,
                        "🤔 Ese código de invitación no es válido o ya expiró. "
                        "Pedile al administrador que te comparta un link nuevo.")
            else:
                await tg.enviar_texto(chat_id,
                    "👋 ¡Hola! Soy el asistente de *Finanzas Familia*.\n\n"
                    "Para conectarte a tu familia, necesitás un código de invitación. "
                    "Pedile al administrador que te comparta el link.")
            db.commit()
            return

        # --- Usuario no vinculado aún ---
        if not user:
            await tg.enviar_texto(chat_id,
                "👋 Todavía no estás vinculado a ninguna familia.\n"
                "Pedile al administrador que te comparta el link de invitación "
                "y abrilo desde Telegram.")
            return

        # --- Mensaje normal: responde el asistente IA ---
        db.add(WaMessage(wa_message_id=None, direction="in", phone=chat_id, body=text))
        db.commit()

        log.info("← Telegram %s (%s): %s", user.name, chat_id, text)
        today = _dt.date.today()
        reply = await asistente.responder(db, user, text, today, canal="telegram")
        db.add(WaMessage(wa_message_id=None, direction="out", phone=chat_id, body=reply))
        db.commit()
        log.info("→ Telegram %s: %s", user.name, (reply or "")[:60])

        await tg.enviar_texto(chat_id, reply)
    except Exception as e:
        log.warning("Error procesando mensaje de Telegram (%s): %s", chat_id, e)
        try:
            await tg.enviar_texto(chat_id, "😬 Tuvimos un error. ¿Probás de nuevo?")
        except Exception:
            pass
    finally:
        db.close()


def _vincular_o_crear(db: Session, chat_id: str, msg: dict, hh: Household) -> User:
    """Vincula el chat_id al usuario existente con ese Telegram, o crea uno nuevo."""
    tg_name = (msg.get("chat") or {}).get("first_name") or ""
    tg_username = (msg.get("chat") or {}).get("username") or ""
    nombre = (tg_name or tg_username or f"Usuario-{chat_id[-6:]}")[:120]

    # Ya hay un usuario sin Telegram vinculado con ese nombre en la familia? Lo conectamos.
    existente = finanzas.usuario_por_nombre(db, hh.id, nombre)
    if existente and not existente.telegram_chat_id:
        existente.telegram_chat_id = chat_id
        return existente

    # Crear nuevo perfil en la familia.
    u = User(
        household_id=hh.id, name=nombre,
        telegram_chat_id=chat_id, role="member", is_active=True,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


# ---------------------------------------------------------------------------
# WhatsApp bridge (legacy — en desuso, se conserva para no romper)
# ---------------------------------------------------------------------------
def verificar_whatsapp_token(x_bridge_token: str | None = Header(default=None)) -> None:
    if x_bridge_token != config.WHATSAPP_BRIDGE_TOKEN:
        raise HTTPException(status_code=403, detail="token invalido")


@router.post("/bridge", dependencies=[Depends(verificar_whatsapp_token)])
async def recibir_whatsapp(payload: dict, db: Session = Depends(get_db)):
    telefono = (payload.get("from") or "").strip()
    mensaje = (payload.get("message") or "").strip()
    wa_id = payload.get("wa_message_id")
    if not telefono or not mensaje:
        return {"reply": None}

    if wa_id and db.scalar(select(WaMessage).where(WaMessage.wa_message_id == wa_id)):
        return {"reply": None}
    db.add(WaMessage(wa_message_id=wa_id, direction="in", phone=telefono, body=mensaje))
    db.commit()

    user = finanzas.usuario_por_telefono(db, telefono)
    if not user or not user.is_active:
        log.info("Mensaje de numero no autorizado: %s", telefono)
        return {"reply": "👋 Tu número no está registrado en ninguna familia. Pídele al administrador que te agregue."}

    log.info("← WhatsApp %s (%s): %s", user.name, telefono, mensaje)
    reply = await asistente.responder(db, user, mensaje, canal="whatsapp")
    db.add(WaMessage(wa_message_id=None, direction="out", phone=telefono, body=reply))
    db.commit()
    log.info("→ WhatsApp %s: %s", user.name, (reply or "")[:60])
    return {"reply": reply}


@router.post("/bridge/estado", dependencies=[Depends(verificar_whatsapp_token)])
async def estado_whatsapp(payload: dict):
    log.info("Estado del puente WhatsApp: conectado=%s numero=%s",
             payload.get("conectado"), payload.get("numero"))
    return {"ok": True}
