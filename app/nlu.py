"""Entendimiento del mensaje (NLU).

Convierte texto libre de WhatsApp en una intencion estructurada. Usa DeepSeek
si hay API key; si no (o si falla), cae a un parser heuristico local que igual
permite anotar gastos, ingresos y recordatorios.

Devuelve siempre un dict con esta forma:
    {
      "intent": add_expense | add_income | add_bill | set_income | query | help | unknown,
      "amount": int | None,            # CLP
      "category": str | None,          # nombre de categoria
      "description": str | None,
      "date": "YYYY-MM-DD" | None,     # fecha del movimiento
      "label": str | None,             # nombre de la cuenta (add_bill)
      "due_date": "YYYY-MM-DD" | None, # vencimiento (add_bill)
      "notify_days_before": int | None,
      "query_kind": month | remaining | ant | category | None,
      "confidence": float,
    }
"""
from __future__ import annotations

import datetime as _dt
import json
import re

import httpx

from .config import config
from .defaults import detectar_categoria
from .logger import get_logger
from .money import parse_amount

log = get_logger("nlu")

_INTENTS = {"add_expense", "add_income", "add_bill", "set_income", "query", "help", "unknown"}


# ---------------------------------------------------------------------------
# Entrada principal
# ---------------------------------------------------------------------------
async def interpretar(message: str, categorias: list[str], today: _dt.date) -> dict:
    obj = await _con_deepseek(message, categorias, today)
    if obj and obj.get("intent") in _INTENTS and obj["intent"] != "unknown":
        return obj
    return _fallback(message, today)


def interpretar_local(message: str, today: _dt.date) -> dict:
    """Parser heurístico puro (sin IA). Para cuando no hay API key o la IA falló."""
    return _fallback(message, today)


# ---------------------------------------------------------------------------
# DeepSeek (API compatible con OpenAI)
# ---------------------------------------------------------------------------
def _system_prompt(categorias: list[str], today: _dt.date) -> str:
    cats = ", ".join(categorias)
    return f"""Eres un asistente de finanzas familiares en Chile. Conviertes el mensaje del usuario en JSON.

Hoy es {today.isoformat()} (zona America/Santiago). La moneda es el peso chileno (CLP).
Normaliza la plata coloquial: "luca"/"lucas"/"k" = 1.000, "palo"/"palos"/"millon" = 1.000.000.
Ejemplos: "15 lucas" -> 15000 ; "2 palos" -> 2000000 ; "1.500" -> 1500.

Categorias disponibles (elige la que mejor calce): {cats}.

Devuelve SOLO un objeto JSON con estos campos (usa null si no aplica):
- intent: uno de [add_expense, add_income, add_bill, set_income, query, help, unknown]
- amount: entero en pesos (sin puntos ni simbolos)
- category: nombre exacto de una categoria de la lista
- description: descripcion corta del movimiento
- date: fecha del movimiento en formato YYYY-MM-DD (resuelve "ayer", "el viernes", etc.)
- label: nombre de la cuenta a pagar (solo para add_bill, ej "arriendo")
- due_date: vencimiento YYYY-MM-DD (solo add_bill)
- notify_days_before: cuantos dias antes avisar (entero, default 3)
- query_kind: para intent=query, uno de [month, remaining, ant, category]
- confidence: tu confianza de 0 a 1

Reglas de intencion:
- add_expense: registra un gasto ("gaste 15 lucas en bencina").
- add_income: registra un ingreso puntual ("me llego un bono de 50 lucas").
- set_income: el usuario define su sueldo mensual ("gano 900 lucas", "mi sueldo es 1.2 palos").
- add_bill: agenda una cuenta/recordatorio ("el 20 pago el arriendo 350 lucas", "recuerdame pagar la luz").
- query: pregunta por sus numeros ("cuanto llevo gastado", "cuanto me queda", "gastos hormiga").
- help/unknown: saludo o algo no relacionado."""


async def _con_deepseek(message: str, categorias: list[str], today: _dt.date) -> dict | None:
    if not config.DEEPSEEK_ENABLED:
        return None
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": _system_prompt(categorias, today)},
            {"role": "user", "content": message},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 400,
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{config.DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
                json=payload,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            obj = json.loads(content)
    except (httpx.HTTPError, ValueError, KeyError, json.JSONDecodeError) as e:
        log.warning("DeepSeek no disponible, uso fallback: %s", e)
        return None
    return _normalizar(obj, message)


def _normalizar(obj: dict, message: str) -> dict:
    """Asegura tipos correctos en la respuesta de la IA."""
    out: dict = {
        "intent": obj.get("intent") if obj.get("intent") in _INTENTS else "unknown",
        "amount": _as_int(obj.get("amount")),
        "category": obj.get("category") or None,
        "description": (obj.get("description") or None),
        "date": _as_date_str(obj.get("date")),
        "label": obj.get("label") or None,
        "due_date": _as_date_str(obj.get("due_date")),
        "notify_days_before": _as_int(obj.get("notify_days_before")) or 3,
        "query_kind": obj.get("query_kind") or None,
        "confidence": float(obj.get("confidence") or 0.85),
    }
    # Respaldo: si la IA no dio monto pero el texto lo tiene, lo extraemos.
    if out["amount"] is None and out["intent"] in {"add_expense", "add_income", "set_income"}:
        out["amount"] = parse_amount(message)
    return out


def _as_int(v) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(round(float(v)))
    except (ValueError, TypeError):
        return None


def _as_date_str(v) -> str | None:
    if not v or not isinstance(v, str):
        return None
    try:
        _dt.date.fromisoformat(v[:10])
        return v[:10]
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Fallback heuristico (sin IA)
# ---------------------------------------------------------------------------
def _fallback(message: str, today: _dt.date) -> dict:
    t = (message or "").lower().strip()
    amount = parse_amount(t)

    base = {
        "intent": "unknown", "amount": amount, "category": None, "description": message.strip()[:120],
        "date": today.isoformat(), "label": None, "due_date": None,
        "notify_days_before": 3, "query_kind": None, "confidence": 0.45,
    }

    # Consultas
    if "hormiga" in t or any(k in t for k in ["cuanto", "cuánto", "resumen", "como voy", "cómo voy", "balance"]):
        base["intent"] = "query"
        if "hormiga" in t:
            base["query_kind"] = "ant"
        elif any(k in t for k in ["queda", "sobra", "sobrar"]):
            base["query_kind"] = "remaining"
        else:
            base["query_kind"] = "month"
        return base

    # Definir sueldo (el sueldo mensual vive en monthly_income, no como transacción,
    # para no duplicarlo con el ingreso base al sumar ingresos del mes).
    if "sueldo" in t or "salario" in t or re.search(r"\bgano\b", t):
        base["intent"] = "set_income"
        base["category"] = "Sueldo"
        return base

    # Recordatorio / cuenta por pagar
    if any(k in t for k in ["recuerda", "recuérda", "recordar", "acuerda", "acuérda", "vence", "tengo que pagar", "hay que pagar"]) \
            or re.search(r"\bel\s+\d{1,2}\b.*\bpag", t):
        base["intent"] = "add_bill"
        base["due_date"] = _resolver_fecha(t, today, future=True).isoformat()
        base["label"] = _limpiar_label(t)
        return base

    # Ingreso puntual
    if any(k in t for k in ["me llego", "me llegó", "recibi", "recibí", "me pagaron", "bono", "aguinaldo", "ingreso", "gane", "gané"]):
        base["intent"] = "add_income"
        base["category"] = detectar_categoria(t, "income")
        base["date"] = _resolver_fecha(t, today).isoformat()
        return base

    # Gasto (por defecto, si hay monto)
    if amount is not None:
        cat = detectar_categoria(t, "expense")
        base["intent"] = "add_expense"
        base["category"] = cat
        base["date"] = _resolver_fecha(t, today).isoformat()
        # Si reconocimos una categoria concreta, confiamos; si cayo en "Otros", marcamos para revisar.
        base["confidence"] = 0.7 if cat != "Otros" else 0.5
        return base

    base["intent"] = "help"
    return base


def _resolver_fecha(texto: str, today: _dt.date, future: bool = False) -> _dt.date:
    t = texto.lower()
    if "anteayer" in t:
        return today - _dt.timedelta(days=2)
    if "pasado mañana" in t or "pasado manana" in t:
        return today + _dt.timedelta(days=2)
    if "mañana" in t or "manana" in t:
        return today + _dt.timedelta(days=1)
    if "ayer" in t:
        return today - _dt.timedelta(days=1)
    m = re.search(r"\b(?:el|para el|d[ií]a)\s+(\d{1,2})\b", t)
    if m:
        day = int(m.group(1))
        if 1 <= day <= 31:
            return _dia_a_fecha(day, today, future)
    return today


def _dia_a_fecha(day: int, today: _dt.date, future: bool) -> _dt.date:
    import calendar

    year, month = today.year, today.month
    day = min(day, calendar.monthrange(year, month)[1])
    fecha = _dt.date(year, month, day)
    if future and fecha < today:
        # pasa al mes siguiente
        month = 1 if month == 12 else month + 1
        year = year + 1 if month == 1 else year
        day = min(day, calendar.monthrange(year, month)[1])
        fecha = _dt.date(year, month, day)
    return fecha


def _limpiar_label(texto: str) -> str:
    t = re.sub(r"\b(recu[eé]rda(me)?|recordar|acu[eé]rda(me)?|tengo que|hay que|pagar|el d[ií]a|el)\b", " ", texto, flags=re.I)
    t = re.sub(r"\d[\d.,]*\s*(lucas?|palos?|mil|k)?", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,")
    return t[:80] or "cuenta"
