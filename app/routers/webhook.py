"""Webhook de Telegram: recibe mensajes del bot y responde con el asistente IA.

El puente WhatsApp está en desuso (reemplazado por Telegram).
Se conserva el endpoint /bridge por retrocompatibilidad mientras migran los usuarios.
"""
from __future__ import annotations

import datetime as _dt
import io
import os
import tempfile

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
    document = msg.get("document")
    update_id = update.get("update_id")

    # Sin texto y sin documento: ignorar (podría ser sticker, audio, etc.)
    if not chat_id or (not text and not document):
        return JSONResponse({"ok": True})

    # Deduplicación: Telegram puede reenviar el mismo update.
    existente = db.scalar(
        select(WaMessage).where(WaMessage.wa_message_id == f"tg-{update_id}")
    )
    if existente:
        return JSONResponse({"ok": True})
    caption = (msg.get("caption") or "").strip()
    db.add(WaMessage(wa_message_id=f"tg-{update_id}", direction="in",
                     phone=chat_id, body=text or f"[documento: {caption}]"))
    db.commit()

    # Despachar en background para responder dentro de los 10 s de Telegram.
    if document:
        background.add_task(_procesar_documento, chat_id, document, caption, update_id)
    else:
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
# Procesamiento de documentos (importación masiva vía Telegram)
# ---------------------------------------------------------------------------
async def _procesar_documento(chat_id: str, document: dict, caption: str,
                               update_id: int | None) -> None:
    """Descarga un archivo .txt/.csv de Telegram, lo parsea con IA, registra
    todos los movimientos y devuelve el Excel actualizado."""
    from ..db import SessionLocal

    file_id = document.get("file_id")
    file_name = (document.get("file_name") or "documento.txt").lower()

    if not file_id or not file_name.endswith((".txt", ".csv", ".tsv")):
        await tg.enviar_texto(chat_id,
            "📎 Solo proceso archivos <b>.txt</b> o <b>.csv</b> con tus movimientos.\n"
            "Ejemplo de formato:\n"
            "<code>15/06 Supermercado 45.990\n16/06 Copec 38.500\n17/06 Farmacia 12.990</code>")
        return

    await tg.enviar_texto(chat_id, "⏳ Descargando y analizando tu archivo…")

    contenido = await tg.descargar_archivo(file_id)
    if not contenido:
        await tg.enviar_texto(chat_id, "😕 No pude descargar el archivo. ¿Probás de nuevo?")
        return

    try:
        texto = contenido.decode("utf-8")
    except UnicodeDecodeError:
        try:
            texto = contenido.decode("latin-1")
        except UnicodeDecodeError:
            await tg.enviar_texto(chat_id, "😕 No pude leer el archivo. Asegurate de que esté en formato texto (UTF-8).")
            return

    db = SessionLocal()
    try:
        user = db.scalars(select(User).where(User.telegram_chat_id == chat_id)).first()
        if not user:
            await tg.enviar_texto(chat_id, "👋 Primero vinculá tu cuenta. Pedile al admin el link de invitación.")
            return

        await tg.enviar_texto(chat_id, f"📋 Leí {len(texto.splitlines())} líneas. Analizando con IA…")

        # Llamar al agente IA con un prompt de importación masiva
        resultado = await _importar_masivo(db, user, texto)

        # Generar Excel y enviarlo
        excel_path = await _generar_excel_para_envio(user.household_id)
        if excel_path:
            await tg.enviar_documento(chat_id, excel_path,
                caption=f"📊 Excel actualizado — {resultado['resumen']}",
                filename=f"finanzas-{_dt.date.today().isoformat()}.xlsx")
            os.unlink(excel_path)

        await tg.enviar_texto(chat_id, resultado["reply"])
    except Exception as e:
        log.warning("Error procesando documento (%s): %s", chat_id, e)
        await tg.enviar_texto(chat_id, "😕 Tuvimos un error procesando el archivo. ¿Probás de nuevo?")
    finally:
        db.close()


async def _importar_masivo(db: Session, user: User, texto: str) -> dict:
    """Envía el texto del archivo al agente IA para registro masivo."""
    today = _dt.date.today()
    data = finanzas.resumen_mes(db, user.household, today.year, today.month)
    cats = finanzas.categorias(db, user.household_id)
    cats_gasto = ", ".join(c.name for c in cats if c.kind == "expense")

    prompt = f"""Eres el asistente de finanzas de {user.household.name}. Recibiste un archivo de texto con movimientos bancarios o de gastos. Tu tarea es EXTRAER y REGISTRAR CADA movimiento.

REGLAS:
1. Por cada línea del archivo que sea un gasto o ingreso, llama a registrar_movimiento.
2. Si una línea no es un movimiento (encabezado, saldo, resumen), ignórala.
3. Las fechas pueden estar en formato DD/MM, DD/MM/AAAA o YYYY-MM-DD. Si no hay año, asumí {today.year}.
4. Si el monto está en negativo (-) o entre paréntesis, es un gasto.
5. Si no podés identificar la categoría exacta, usá la que mejor calce.
6. Cada llamado a registrar_movimiento debe incluir: kind, amount, category, description, date.
7. Si hay descripciones largas, resumilas a máximo 80 caracteres.

HOY: {today.isoformat()}
FAMILIA: {user.household.name}
CATEGORÍAS: {cats_gasto}
MONEDA: peso chileno (CLP). Montos en formato chileno: 1.000 = mil, 45.990 = cuarenta y cinco mil novecientos noventa.

DATOS DEL ARCHIVO:
{texto[:8000]}

Después de registrar todo, responde con un RESUMEN así:
✅ Importación completada: X movimientos registrados.
💰 Total gastos: $X
📂 Categorías: X en supermercado, X en bencina, etc.
⚠️ Dudas: (si algo no quedó claro)"""

    from ..services import ia as ia_service
    try:
        historial = [{"role": "user", "content": prompt}]
        out = await ia_service.conversar(db, user, historial, canal="telegram")
        return {"reply": out.get("reply", "✅ Importación procesada."),
                "resumen": f"{len(out.get('actions', []))} movimientos"}
    except Exception:
        return {"reply": "✅ Archivo procesado. Revisá tu dashboard o el Excel adjunto.", "resumen": "completado"}


async def _generar_excel_para_envio(household_id: int) -> str | None:
    """Genera un archivo Excel temporal con los datos de la familia."""
    from ..db import SessionLocal
    from openpyxl import Workbook
    from ..money import format_clp

    db = SessionLocal()
    try:
        from ..models import Household
        hh = db.get(Household, household_id)
        if not hh:
            return None

        today = _dt.date.today()
        r = finanzas.resumen_mes(db, hh, today.year, today.month)
        txs = finanzas.transacciones_mes(db, hh, today.year, today.month)

        wb = Workbook()
        ws = wb.active
        ws.title = "Movimientos"
        ws.append(["Fecha", "Tipo", "Categoría", "Descripción", "Persona", "Monto"])
        total_gasto = 0
        total_ingreso = 0
        for t in txs:
            es_gasto = t.kind == "expense"
            if es_gasto:
                total_gasto += t.amount
            else:
                total_ingreso += t.amount
            ws.append([
                t.occurred_at.isoformat(),
                "Gasto" if es_gasto else "Ingreso",
                t.category.name if t.category else "",
                t.description or "",
                t.user.name if t.user else "",
                t.amount,
            ])
        ws.append([])
        ws.append(["", "", "", "", "Total Ingresos", total_ingreso])
        ws.append(["", "", "", "", "Total Gastos", total_gasto])
        ws.append(["", "", "", "", "Disponible", r["disponible"]])

        fd, path = tempfile.mkstemp(suffix=".xlsx", prefix="finanzas_")
        os.close(fd)
        wb.save(path)
        return path
    finally:
        db.close()


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
