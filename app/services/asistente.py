"""Orquestador del asistente de WhatsApp.

Con API key de DeepSeek: agente conversacional completo (ia.py) con memoria
multi-turno por teléfono — entiende contexto, registra varios gastos de una,
consulta datos reales con herramientas, etc.

Sin API key (o si la IA se cae): parser heurístico local que igual permite
anotar gastos, ingresos y recordatorios.
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import nlu
from ..config import config
from ..logger import get_logger
from ..models import User
from ..money import format_clp
from . import finanzas, ia

log = get_logger("asistente")

def _ayuda(user: User) -> str:
    hh = user.household
    nombre_bot = (hh.assistant_name or "").strip() or "Fin"
    return (
        f"👋 Hola {user.name.split(' ')[0]}, soy *{nombre_bot}*, el asistente de finanzas de la *{hh.name}*. "
        "Escríbeme natural:\n"
        "• \"gasté 15 lucas en bencina\"\n"
        "• \"pagué 38.000 en el super y 6 lucas de café\"\n"
        "• \"me llegó un bono de 50 lucas\"\n"
        "• \"el 20 pago el arriendo, 350 lucas\"\n"
        "• \"¿cuánto llevo gastado este mes?\" / \"¿cuánto me queda?\"\n"
        "• \"¿en qué he gastado más?\" / \"gastos hormiga\"\n"
        "• \"ponle un presupuesto de 100 lucas al delivery\""
    )


async def responder(db: Session, user: User, message: str, today: _dt.date | None = None,
                    canal: str = "whatsapp") -> str:
    today = today or _dt.date.today()

    if config.DEEPSEEK_ENABLED:
        try:
            clave = user.telegram_chat_id or user.phone or f"user-{user.id}"
            historial = ia.memoria_de(clave)
            historial.append({"role": "user", "content": message})
            out = await ia.conversar(db, user, historial, canal=canal, today=today)
            ia.recordar(clave, "user", message)
            ia.recordar(clave, "assistant", out["reply"])
            return out["reply"]
        except ia.IAError as e:
            log.warning("Agente IA caído, uso heurístico: %s", e)

    # En un thread: el heurístico registra movimientos (DB) y puede enviar
    # alertas por WhatsApp (I/O), que no deben bloquear el event loop.
    return await run_in_threadpool(_responder_heuristico, db, user, message, today)


# ---------------------------------------------------------------------------
# Camino heurístico (sin IA)
# ---------------------------------------------------------------------------
def _fecha(s: str | None, today: _dt.date) -> _dt.date:
    if not s:
        return today
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return today


def _responder_heuristico(db: Session, user: User, message: str, today: _dt.date) -> str:
    p = nlu.interpretar_local(message, today)
    intent = p["intent"]

    if intent == "add_expense":
        return _add_expense(db, user, p, today)
    if intent == "add_income":
        return _add_income(db, user, p, today)
    if intent == "set_income":
        return _set_income(db, user, p)
    if intent == "add_bill":
        return _add_bill(db, user, p, today)
    if intent == "query":
        return _query(db, user.household, p, today)
    return _ayuda(user)


def _add_expense(db: Session, user: User, p: dict, today: _dt.date) -> str:
    if not p.get("amount"):
        return "🤔 ¿De cuánto fue el gasto? Escríbelo así: \"gasté 8 lucas en almuerzo\"."
    fecha = _fecha(p.get("date"), today)
    tx = finanzas.registrar_movimiento(
        db, user, "expense", p["amount"], p.get("category"), p.get("description"),
        fecha, raw_text=p.get("description"), ai_confidence=p.get("confidence"),
    )
    cat = tx.category
    acum = finanzas.total_categoria_mes(db, user.household_id, cat.id, fecha.year, fecha.month)
    extra = "  ⚠️ confírmame si está bien" if tx.needs_review else ""
    return (f"✅ Anotado: {format_clp(tx.amount)} en {cat.name} {cat.emoji or ''}{extra}\n"
            f"Llevas {format_clp(acum)} en {cat.name} este mes.")


def _add_income(db: Session, user: User, p: dict, today: _dt.date) -> str:
    if not p.get("amount"):
        return "🤔 ¿De cuánto fue el ingreso?"
    fecha = _fecha(p.get("date"), today)
    tx = finanzas.registrar_movimiento(
        db, user, "income", p["amount"], p.get("category") or "Otros ingresos",
        p.get("description"), fecha, raw_text=p.get("description"),
    )
    return f"💰 Ingreso anotado: {format_clp(tx.amount)} ({tx.category.name})."


def _set_income(db: Session, user: User, p: dict) -> str:
    if not p.get("amount"):
        return "🤔 ¿Cuál es tu sueldo mensual? Escríbelo así: \"gano 900 lucas\"."
    finanzas.set_sueldo(db, user, p["amount"])
    return f"👌 Listo {user.name}, tu sueldo mensual quedó en {format_clp(p['amount'])}."


def _add_bill(db: Session, user: User, p: dict, today: _dt.date) -> str:
    due = _fecha(p.get("due_date") or p.get("date"), today)
    n = p.get("notify_days_before") or 3
    b = finanzas.crear_bill(db, user, p.get("label") or p.get("description") or "cuenta",
                            p.get("amount"), due, n)
    monto = f" por {format_clp(b.amount)}" if b.amount else ""
    extra = " y lo agendé en tu Google Calendar 📅" if b.gcal_event_id else ""
    return (f"⏰ Anotado: te recordaré pagar *{b.label}*{monto} el "
            f"{due.strftime('%d/%m')} (te aviso {n} día{'s' if n != 1 else ''} antes){extra}.")


def _query(db: Session, household, p: dict, today: _dt.date) -> str:
    r = finanzas.resumen_mes(db, household, today.year, today.month)
    qk = p.get("query_kind") or "month"

    if qk == "remaining":
        return (f"💵 Este mes ({r['mes_nombre']}):\n"
                f"Ingresos {format_clp(r['ingresos'])} − gastos {format_clp(r['gastos'])}"
                f"{' − cuentas pendientes ' + format_clp(r['bills_pendientes']) if r['bills_pendientes'] else ''}\n"
                f"➡️ Disponible: *{format_clp(r['disponible'])}*")

    if qk == "ant":
        ant = [c for c in r["por_categoria"] if c["is_ant"]]
        if not ant:
            return "🐜 Aún no tienes gastos hormiga registrados este mes. ¡Bien ahí!"
        lineas = "\n".join(f"  {c['emoji']} {c['nombre']}: {format_clp(c['total'])}" for c in ant)
        return f"🐜 Gastos hormiga de {r['mes_nombre']} ({format_clp(r['hormigas'])}):\n{lineas}"

    top = "\n".join(f"  {c['emoji']} {c['nombre']}: {format_clp(c['total'])}" for c in r["por_categoria"][:5])
    cuerpo = (f"📊 {r['mes_nombre']} {r['year']}\n"
              f"Ingresos: {format_clp(r['ingresos'])}\n"
              f"Gastos: {format_clp(r['gastos'])}\n")
    if r["bills_pendientes"]:
        cuerpo += f"Cuentas por pagar: {format_clp(r['bills_pendientes'])}\n"
    cuerpo += f"➡️ Te queda: *{format_clp(r['disponible'])}*"
    if top:
        cuerpo += f"\n\nTop gastos:\n{top}"
    if r["hormigas"]:
        cuerpo += f"\n\n🐜 Hormiga: {format_clp(r['hormigas'])}"
    return cuerpo
