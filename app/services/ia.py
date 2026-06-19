"""Agente IA sobre DeepSeek (function calling).

Es el cerebro de la app cuando hay API key: recibe la conversación (web o
WhatsApp), decide qué herramientas llamar (registrar gastos, consultar
resúmenes, agendar cuentas...) y redacta la respuesta final. También genera
el análisis mensual ("insights") que se muestra en el dashboard.

Sin API key nada de esto se usa: el asistente cae al parser heurístico local.
"""
from __future__ import annotations

import datetime as _dt
import json
import time

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..config import config
from ..logger import get_logger
from ..models import AiInsight, Household, Transaction, User
from ..money import format_clp
from . import finanzas

log = get_logger("ia")

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


class IAError(Exception):
    """La API de DeepSeek falló o respondió algo inusable."""


# ---------------------------------------------------------------------------
# Definición de herramientas (function calling)
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "registrar_movimiento",
            "description": "Registra un gasto o un ingreso EXTRA puntual (bono, regalo, venta, devolución). NO la uses para el sueldo mensual recurrente: para eso usa definir_sueldo. Si el usuario menciona varios movimientos, llama esta herramienta una vez por cada uno.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["expense", "income"],
                             "description": "expense = gasto, income = ingreso"},
                    "amount": {"type": "integer", "description": "Monto en pesos chilenos (entero positivo)"},
                    "category": {"type": "string", "description": "Categoría de la lista disponible que mejor calce"},
                    "description": {"type": "string", "description": "Descripción corta (ej: 'bencina copec')"},
                    "date": {"type": "string", "description": "Fecha YYYY-MM-DD del movimiento (hoy si no se indica)"},
                    "persona": {"type": "string", "description": "Nombre del miembro al que pertenece el movimiento. Por defecto, quien habla."},
                },
                "required": ["kind", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "eliminar_movimiento",
            "description": "Elimina un movimiento por su id (usa listar_movimientos primero para encontrarlo).",
            "parameters": {
                "type": "object",
                "properties": {"tx_id": {"type": "integer", "description": "Id del movimiento a borrar"}},
                "required": ["tx_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crear_recordatorio",
            "description": "Agenda una cuenta por pagar / recordatorio con fecha de vencimiento. Avisa por WhatsApp antes y, si la persona conectó Google Calendar, también crea un evento con alarmas (la respuesta trae agendado_en_google_calendar).",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Nombre de la cuenta (ej: 'arriendo', 'luz')"},
                    "amount": {"type": "integer", "description": "Monto en CLP si se conoce"},
                    "due_date": {"type": "string", "description": "Vencimiento YYYY-MM-DD"},
                    "notify_days_before": {"type": "integer", "description": "Días de anticipación del aviso (default 3)"},
                },
                "required": ["label", "due_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "marcar_cuenta_pagada",
            "description": "Marca una cuenta pendiente como pagada, buscándola por nombre.",
            "parameters": {
                "type": "object",
                "properties": {"label": {"type": "string", "description": "Nombre (aproximado) de la cuenta"}},
                "required": ["label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_cuentas_pendientes",
            "description": "Lista las cuentas por pagar pendientes de la familia.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "definir_sueldo",
            "description": "Define el sueldo mensual de un miembro de la familia.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "integer", "description": "Sueldo mensual en CLP"},
                    "persona": {"type": "string", "description": "Nombre del miembro. Por defecto, quien habla."},
                },
                "required": ["amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fijar_presupuesto",
            "description": "Fija o actualiza un límite de gasto mensual para una categoría (o total si no se indica categoría).",
            "parameters": {
                "type": "object",
                "properties": {
                    "monto_limite": {"type": "integer", "description": "Límite mensual en CLP"},
                    "category": {"type": "string", "description": "Categoría de gasto; omite para límite total del mes"},
                },
                "required": ["monto_limite"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_presupuestos",
            "description": "Muestra los presupuestos definidos y cuánto se ha gastado de cada uno este mes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resumen_financiero",
            "description": "Resumen del mes: ingresos, gastos, disponible, gastos por categoría, gasto hormiga y por persona. Úsalo para responder '¿cuánto llevo?', '¿cuánto me queda?', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer", "description": "Año (default: actual)"},
                    "month": {"type": "integer", "description": "Mes 1-12 (default: actual)"},
                    "persona": {"type": "string", "description": "Filtra los números a un miembro (ej: quien habla, si pregunta por 'mi' gasto)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_movimientos",
            "description": "Lista movimientos del mes con sus ids (filtrable por persona y categoría).",
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {"type": "integer"},
                    "month": {"type": "integer", "description": "Mes 1-12"},
                    "persona": {"type": "string", "description": "Nombre de un miembro para filtrar"},
                    "category": {"type": "string", "description": "Categoría para filtrar"},
                    "limit": {"type": "integer", "description": "Máximo de filas (default 20)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "comparar_meses",
            "description": "Ingresos vs gastos de los últimos N meses, para comparar tendencias.",
            "parameters": {
                "type": "object",
                "properties": {"n": {"type": "integer", "description": "Cantidad de meses hacia atrás (default 6)"}},
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Cliente DeepSeek
# ---------------------------------------------------------------------------
async def _chat_api(messages: list[dict], tools: list[dict] | None = None,
                    temperature: float = 0.3, max_tokens: int = 900) -> dict:
    """Una llamada al endpoint /chat/completions. Devuelve el `message` de la respuesta."""
    if not config.DEEPSEEK_ENABLED:
        raise IAError("DEEPSEEK_API_KEY no configurada")
    payload: dict = {
        "model": config.DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{config.DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
                json=payload,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
        detalle = ""
        if isinstance(e, httpx.HTTPStatusError):
            detalle = f" [{e.response.status_code}] {e.response.text[:200]}"
        log.warning("Error DeepSeek: %s%s", e, detalle)
        raise IAError(f"{e}{detalle}") from e


# ---------------------------------------------------------------------------
# Prompt del agente
# ---------------------------------------------------------------------------
def _system_prompt(db: Session, user: User, today: _dt.date, canal: str) -> str:
    hh = user.household
    nombre_bot = (hh.assistant_name or "").strip() or "Fin"
    cats = finanzas.categorias(db, hh.id)
    cats_gasto = ", ".join(f"{c.name}" for c in cats if c.kind == "expense")
    cats_ingreso = ", ".join(f"{c.name}" for c in cats if c.kind == "income")
    miembros = "\n".join(
        f"- {u.name}" + (f" (sueldo {format_clp(u.monthly_income)})" if u.monthly_income else "")
        + ("  ← ES QUIEN TE HABLA" if u.id == user.id else "")
        for u in hh.users if u.is_active
    )
    formato = ("Formato WhatsApp: texto plano, *negritas* con UN asterisco a cada lado (nunca **doble**). PROHIBIDO el markdown: nada de #, **, tablas ni [links]."
               if canal == "whatsapp" else
               "Puedes usar markdown simple: **negritas** y listas con viñetas (•). PROHIBIDO usar tablas markdown (|...|), títulos (#) o bloques de código.")
    return f"""Eres "{nombre_bot}" 🪙, el asistente de finanzas de la familia {hh.name}. Hablas español chileno, cercano y directo.
Cuando te saluden o sea claramente el primer mensaje, preséntate en una línea: "Soy {nombre_bot}, el asistente de {hh.name}".

HOY es {DIAS[today.weekday()]} {today.isoformat()}. Moneda: peso chileno (CLP), montos enteros.
Plata coloquial: "luca(s)"/"k" = 1.000 · "palo(s)"/"millón" = 1.000.000. Ej: "15 lucas" = 15000, "1.2 palos" = 1200000.

MIEMBROS DE LA FAMILIA {hh.name}:
{miembros}

Estás hablando con {user.name}. Cuando dice "yo", "mi", "llevo", "gasté", se refiere a sí mismo/a.

CATEGORÍAS DE GASTO: {cats_gasto}
CATEGORÍAS DE INGRESO (solo extras): {cats_ingreso}

REGLAS:
1. Usa SIEMPRE las herramientas para registrar o consultar. NUNCA inventes cifras: si te preguntan números, llama primero a la herramienta y responde con lo que devuelva. NUNCA digas que registraste, guardaste, actualizaste o eliminaste algo si no llamaste su herramienta EN ESTE turno: hacerlo es mentirle al usuario. Un saludo + un dato ("hola, gano 900 lucas") igual requiere llamar la herramienta.
2. Si un mensaje trae varios gastos, registra cada uno con su propia llamada.
2b. El SUELDO mensual recurrente se fija con definir_sueldo (no es un ingreso puntual). "gano/mi sueldo es/me pagaron el sueldo" → definir_sueldo. Bonos, aguinaldos, regalos o ventas sí son ingresos → registrar_movimiento kind=income.
3. Resuelve fechas relativas ("ayer", "el sábado") a YYYY-MM-DD usando la fecha de hoy.
4. Si falta el monto de un gasto, pregúntalo en vez de adivinar.
5. Confirmaciones breves y con un emoji; incluye el acumulado del mes en esa categoría si la herramienta lo entrega.
6. Respuestas cortas (máximo ~8 líneas). Si notas algo relevante (gasto hormiga alto, presupuesto casi copado), coméntalo en una línea.
6b. Al crear un recordatorio, si la herramienta devuelve agendado_en_google_calendar=true, menciona en una línea que también quedó en su Google Calendar 📅.
7. {formato}
8. Solo temas de finanzas de la familia; si te preguntan otra cosa, responde simpático y breve, y vuelve al tema."""


# ---------------------------------------------------------------------------
# Ejecución de herramientas
# ---------------------------------------------------------------------------
def _persona(db: Session, user: User, nombre: str | None) -> User:
    """Resuelve el miembro aludido; si no se encuentra, es quien habla."""
    if nombre:
        u = finanzas.usuario_por_nombre(db, user.household_id, nombre)
        if u:
            return u
    return user


def _ejecutar_tool(db: Session, user: User, name: str, args: dict,
                   today: _dt.date) -> tuple[dict, dict | None]:
    """Ejecuta una herramienta. Devuelve (resultado_para_el_modelo, accion_para_la_ui|None)."""
    hh = user.household
    try:
        if name == "registrar_movimiento":
            amount = abs(int(args.get("amount") or 0))
            if not amount:
                return {"ok": False, "error": "monto faltante o cero"}, None
            kind = args.get("kind") if args.get("kind") in ("expense", "income") else "expense"
            quien = _persona(db, user, args.get("persona"))
            fecha = _parse_fecha(args.get("date"), today)
            tx = finanzas.registrar_movimiento(
                db, quien, kind, amount, args.get("category"), args.get("description"),
                fecha, source="ia", raw_text=args.get("description"), ai_confidence=0.95)
            # Acumulado del mes EN QUE QUEDÓ el movimiento (puede no ser el mes actual
            # si el usuario dijo "ayer" cruzando el cambio de mes).
            acum = finanzas.total_categoria_mes(db, hh.id, tx.category_id, fecha.year, fecha.month)
            icono = "💸" if kind == "expense" else "💰"
            accion = {"tipo": "movimiento",
                      "texto": f"{icono} {'Gasto' if kind == 'expense' else 'Ingreso'}: "
                               f"{format_clp(amount)} · {tx.category.name} ({quien.name})"}
            return {"ok": True, "id": tx.id, "kind": kind, "monto": amount,
                    "categoria": tx.category.name, "persona": quien.name,
                    "fecha": fecha.isoformat(),
                    "acumulado_mes_categoria": acum,
                    "acumulado_mes_categoria_fmt": format_clp(acum)}, accion

        if name == "eliminar_movimiento":
            tx = finanzas.eliminar_movimiento(db, hh.id, int(args.get("tx_id") or 0))
            if not tx:
                return {"ok": False, "error": "movimiento no encontrado"}, None
            return {"ok": True, "eliminado": {"id": tx.id, "monto": tx.amount,
                                              "descripcion": tx.description}}, \
                {"tipo": "borrado", "texto": f"🗑️ Eliminado: {format_clp(tx.amount)} ({tx.description or 'sin descripción'})"}

        if name == "crear_recordatorio":
            due = _parse_fecha(args.get("due_date"), today, future=True)
            amount = abs(int(args["amount"])) if args.get("amount") else None
            b = finanzas.crear_bill(db, user, str(args.get("label") or "cuenta")[:120], amount,
                                    due, int(args.get("notify_days_before") or 3))
            en_cal = bool(b.gcal_event_id)
            return {"ok": True, "id": b.id, "label": b.label, "vence": due.isoformat(),
                    "monto": b.amount, "agendado_en_google_calendar": en_cal}, \
                {"tipo": "recordatorio",
                 "texto": f"⏰ Recordatorio: {b.label} · {due.strftime('%d/%m')}"
                          + (" · 📅 en Google Calendar" if en_cal else "")}

        if name == "marcar_cuenta_pagada":
            b = finanzas.pagar_bill(db, hh.id, label=args.get("label"))
            if not b:
                return {"ok": False, "error": "no encontré esa cuenta pendiente"}, None
            return {"ok": True, "pagada": b.label}, \
                {"tipo": "pago", "texto": f"✅ Pagada: {b.label}"}

        if name == "listar_cuentas_pendientes":
            bills = finanzas.bills_pendientes(db, hh)
            return {"ok": True, "cuentas": [
                {"id": b.id, "label": b.label, "monto": b.amount,
                 "vence": b.due_date.isoformat()} for b in bills]}, None

        if name == "definir_sueldo":
            amount = abs(int(args.get("amount") or 0))
            if not amount:
                return {"ok": False, "error": "monto faltante"}, None
            quien = _persona(db, user, args.get("persona"))
            finanzas.set_sueldo(db, quien, amount)
            return {"ok": True, "persona": quien.name, "sueldo": amount}, \
                {"tipo": "sueldo", "texto": f"💼 Sueldo de {quien.name}: {format_clp(amount)}"}

        if name == "fijar_presupuesto":
            limite = abs(int(args.get("monto_limite") or 0))
            if not limite:
                return {"ok": False, "error": "límite faltante"}, None
            finanzas.set_presupuesto(db, hh.id, args.get("category"), limite)
            etiqueta = args.get("category") or "Total del mes"
            return {"ok": True, "categoria": etiqueta, "limite": limite}, \
                {"tipo": "presupuesto", "texto": f"🎯 Presupuesto {etiqueta}: {format_clp(limite)}"}

        if name == "listar_presupuestos":
            estado = finanzas.presupuestos_estado(db, hh, today.year, today.month)
            return {"ok": True, "presupuestos": estado}, None

        if name == "resumen_financiero":
            y = int(args.get("year") or today.year)
            m = min(max(int(args.get("month") or today.month), 1), 12)
            quien = finanzas.usuario_por_nombre(db, hh.id, args.get("persona"))
            r = finanzas.resumen_mes(db, hh, y, m, user_id=quien.id if quien else None)
            r["filtrado_a_persona"] = quien.name if quien else None
            return {"ok": True, "resumen": r}, None

        if name == "listar_movimientos":
            y = int(args.get("year") or today.year)
            m = min(max(int(args.get("month") or today.month), 1), 12)
            quien = finanzas.usuario_por_nombre(db, hh.id, args.get("persona"))
            limit = min(int(args.get("limit") or 20), 60)
            txs = finanzas.transacciones_mes(db, hh, y, m, limite=limit,
                                             user_id=quien.id if quien else None,
                                             categoria_nombre=args.get("category"))
            return {"ok": True, "movimientos": [
                {"id": t.id, "fecha": t.occurred_at.isoformat(),
                 "tipo": "gasto" if t.kind == "expense" else "ingreso",
                 "monto": t.amount, "categoria": t.category.name if t.category else "",
                 "persona": t.user.name if t.user else "",
                 "descripcion": t.description or ""} for t in txs]}, None

        if name == "comparar_meses":
            n = min(max(int(args.get("n") or 6), 2), 12)
            serie = finanzas.serie_meses(db, hh, today.year, today.month, n)
            return {"ok": True, "meses": serie}, None

        return {"ok": False, "error": f"herramienta desconocida: {name}"}, None
    except (ValueError, TypeError) as e:
        return {"ok": False, "error": f"argumentos inválidos: {e}"}, None


def _parse_fecha(s: str | None, today: _dt.date, future: bool = False) -> _dt.date:
    if s:
        try:
            return _dt.date.fromisoformat(str(s)[:10])
        except ValueError:
            pass
    return today if not future else today + _dt.timedelta(days=7)


# ---------------------------------------------------------------------------
# Conversación (loop del agente)
# ---------------------------------------------------------------------------
MAX_RONDAS = 8


async def conversar(db: Session, user: User, mensajes: list[dict],
                    canal: str = "web", today: _dt.date | None = None) -> dict:
    """Corre el loop del agente sobre la conversación.

    `mensajes`: historial [{'role': 'user'|'assistant', 'content': str}, ...]
    terminando con el último mensaje del usuario.
    Devuelve {'reply': str, 'actions': [{'tipo', 'texto'}], 'refresh': bool}.
    """
    today = today or _dt.date.today()
    msgs: list[dict] = [{"role": "system", "content": _system_prompt(db, user, today, canal)}]
    msgs += [{"role": m["role"], "content": m["content"]}
             for m in mensajes[-14:] if m.get("content") and m.get("role") in ("user", "assistant")]

    actions: list[dict] = []
    for _ in range(MAX_RONDAS):
        try:
            msg = await _chat_api(msgs, tools=TOOLS)
        except IAError:
            # Si ya ejecutamos herramientas (que COMMITEAN en la BD), NO podemos
            # dejar que el error suba al fallback heurístico: re-procesaría el
            # mensaje y duplicaría los movimientos. Respondemos con lo hecho.
            if actions:
                return {"reply": _resumen_acciones(actions, canal), "actions": actions, "refresh": True}
            raise  # nada ejecutado todavía: el caller puede usar el heurístico
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            reply = (msg.get("content") or "").strip() or "🤔 No te entendí, ¿me lo dices de otra forma?"
            if canal == "whatsapp":  # WhatsApp usa *negrita*, no **negrita**
                reply = reply.replace("**", "*")
            return {"reply": reply, "actions": actions, "refresh": bool(actions)}

        msgs.append(msg)
        for tc in tool_calls:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            # En un thread: _ejecutar_tool hace I/O bloqueante (DB, WhatsApp,
            # Calendar) y no debe congelar el event loop async.
            resultado, accion = await run_in_threadpool(
                _ejecutar_tool, db, user, fn.get("name", ""), args, today)
            if accion:
                actions.append(accion)
            log.info("tool %s(%s) -> ok=%s", fn.get("name"), args, resultado.get("ok"))
            msgs.append({"role": "tool", "tool_call_id": tc.get("id"),
                         "content": json.dumps(resultado, ensure_ascii=False, default=str)})

    return {"reply": "Hice varias operaciones pero me enredé con la respuesta final 😅. "
                     "Revisa el dashboard para confirmar.",
            "actions": actions, "refresh": bool(actions)}


def _resumen_acciones(actions: list[dict], canal: str) -> str:
    """Respuesta de respaldo cuando la IA cae tras ejecutar acciones (ya commiteadas)."""
    lineas = "\n".join(a["texto"] for a in actions)
    txt = f"✅ Listo, lo registré:\n{lineas}\n\n_(la conexión con la IA se cortó al redactar, pero quedó guardado)_"
    return txt.replace("**", "*") if canal == "whatsapp" else txt


# ---------------------------------------------------------------------------
# Memoria conversacional de WhatsApp (en memoria del proceso)
# ---------------------------------------------------------------------------
_MEMORIA: dict[str, dict] = {}  # phone -> {"ts": epoch, "msgs": [...]}
MEMORIA_TTL = 30 * 60  # 30 minutos
MEMORIA_MAX = 12


def memoria_de(phone: str) -> list[dict]:
    item = _MEMORIA.get(phone)
    if not item or time.time() - item["ts"] > MEMORIA_TTL:
        return []
    return list(item["msgs"])


def recordar(phone: str, role: str, content: str) -> None:
    item = _MEMORIA.setdefault(phone, {"ts": time.time(), "msgs": []})
    item["ts"] = time.time()
    item["msgs"].append({"role": role, "content": content[:2000]})
    item["msgs"] = item["msgs"][-MEMORIA_MAX:]


# ---------------------------------------------------------------------------
# Insights del dashboard (análisis mensual, cacheado)
# ---------------------------------------------------------------------------
INSIGHTS_PROMPT = """Eres un analista financiero familiar chileno. Con los datos JSON de abajo, escribe un análisis breve y útil del mes para mostrar en el dashboard.

FORMATO: 4 a 6 viñetas que partan con "• " y un emoji. Sin títulos, sin tablas, sin texto introductorio.
Cada viñeta: una observación concreta con cifras en CLP formateadas ($1.234.567). Cierra con UNA recomendación accionable (viñeta que parta con "💡").
Cosas que valen oro: comparación con el mes anterior, categorías que se disparan, gasto hormiga, presupuestos al límite, ritmo de gasto vs días que quedan, quién gasta más.
Si hay pocos datos, dilo amable y sugiere registrar más por WhatsApp.

DATOS:
{datos}"""


def _tx_count(db: Session, household_id: int, year: int, month: int) -> int:
    inicio = _dt.date(year, month, 1)
    fin = _dt.date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return int(db.scalar(
        select(func.count(Transaction.id)).where(
            Transaction.household_id == household_id,
            Transaction.occurred_at >= inicio, Transaction.occurred_at < fin)
    ) or 0)


async def generar_insights(db: Session, household: Household, year: int, month: int,
                           force: bool = False) -> dict:
    """Análisis IA del mes. Cachea por (familia, mes, nº de movimientos)."""
    if not config.DEEPSEEK_ENABLED:
        return {"ok": False, "error": "sin_api_key"}

    n = _tx_count(db, household.id, year, month)
    cache = db.scalars(select(AiInsight).where(
        AiInsight.household_id == household.id, AiInsight.year == year,
        AiInsight.month == month).order_by(AiInsight.id.desc())).first()
    if cache and cache.tx_count == n and not force:
        return {"ok": True, "content": cache.content, "cached": True,
                "generated_at": cache.created_at.isoformat()}

    today = _dt.date.today()
    datos = {
        "mes_analizado": f"{year}-{month:02d}",
        "hoy": today.isoformat(),
        "resumen": finanzas.resumen_mes(db, household, year, month),
        "mes_anterior": finanzas.resumen_mes(db, household,
                                             year - 1 if month == 1 else year,
                                             12 if month == 1 else month - 1),
        "ultimos_meses": finanzas.serie_meses(db, household, year, month, 6),
        "presupuestos": finanzas.presupuestos_estado(db, household, year, month),
    }
    try:
        msg = await _chat_api(
            [{"role": "user", "content": INSIGHTS_PROMPT.format(
                datos=json.dumps(datos, ensure_ascii=False, default=str))}],
            temperature=0.6, max_tokens=700)
    except IAError as e:
        return {"ok": False, "error": str(e)}

    content = (msg.get("content") or "").strip()
    if not content:
        return {"ok": False, "error": "respuesta vacía"}

    db.execute(delete(AiInsight).where(
        AiInsight.household_id == household.id, AiInsight.year == year, AiInsight.month == month))
    db.add(AiInsight(household_id=household.id, year=year, month=month, tx_count=n, content=content))
    db.commit()
    return {"ok": True, "content": content, "cached": False,
            "generated_at": _dt.datetime.now().isoformat()}
