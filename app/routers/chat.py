"""Chat con el agente IA + API JSON del dashboard (insights, datos en vivo, borrado)."""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import nlu
from ..config import config
from ..db import get_db
from ..logger import get_logger
from ..models import User
from ..services import asistente, finanzas, ia
from .helpers import build_dashboard_data, household_actual

log = get_logger("chat")
router = APIRouter(tags=["chat"])

_INTENTS_MUTANTES = {"add_expense", "add_income", "add_bill", "set_income"}


@router.post("/chat")
async def chat(request: Request, db: Session = Depends(get_db), payload: dict = Body(...)):
    household = household_actual(request, db)
    if not household:
        return JSONResponse({"ok": False, "error": "No hay ninguna familia creada todavía."}, status_code=400)

    mensaje = str(payload.get("message") or "").strip()[:2000]
    if not mensaje:
        return JSONResponse({"ok": False, "error": "Mensaje vacío."}, status_code=400)

    user_id = payload.get("user_id")
    user = db.get(User, int(user_id)) if user_id else None
    if not user or user.household_id != household.id:
        user = next((u for u in sorted(household.users, key=lambda x: x.id) if u.is_active), None)
    if not user:
        return JSONResponse({"ok": False, "error": "La familia no tiene miembros activos."}, status_code=400)

    historial = [
        {"role": m.get("role"), "content": str(m.get("content") or "")[:2000]}
        for m in (payload.get("history") or [])[-12:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ]
    historial.append({"role": "user", "content": mensaje})

    if config.DEEPSEEK_ENABLED:
        try:
            out = await ia.conversar(db, user, historial, canal="web")
            return JSONResponse({"ok": True, "reply": out["reply"], "actions": out["actions"],
                                 "refresh": out["refresh"], "ia": True})
        except ia.IAError as e:
            log.warning("IA caída en /chat, uso heurístico: %s", e)

    # Sin API key (o IA caída): camino heurístico (en thread: registra en DB y
    # puede enviar WhatsApp, no debe bloquear el event loop).
    today = _dt.date.today()
    p = nlu.interpretar_local(mensaje, today)
    reply = await run_in_threadpool(asistente._responder_heuristico, db, user, mensaje, today)  # noqa: SLF001
    return JSONResponse({"ok": True, "reply": reply, "actions": [],
                         "refresh": p["intent"] in _INTENTS_MUTANTES, "ia": False})


@router.get("/api/ia/status")
def ia_status():
    return {"enabled": config.DEEPSEEK_ENABLED, "model": config.DEEPSEEK_MODEL if config.DEEPSEEK_ENABLED else None}


@router.get("/api/insights")
async def insights(request: Request, db: Session = Depends(get_db),
                   year: int | None = None, month: int | None = None,
                   force: int = 0):
    household = household_actual(request, db)
    if not household:
        return JSONResponse({"ok": False, "error": "sin_familia"}, status_code=400)
    today = _dt.date.today()
    month = min(max(month or today.month, 1), 12)
    out = await ia.generar_insights(db, household, year or today.year, month,
                                    force=bool(force))
    status = 200 if out.get("ok") else 502
    if out.get("error") == "sin_api_key":
        status = 200  # la UI muestra el aviso, no es un error del servidor
    return JSONResponse(out, status_code=status)


@router.get("/api/dashboard-data")
def dashboard_data(request: Request, db: Session = Depends(get_db),
                   year: int | None = None, month: int | None = None,
                   u: int | None = None):
    household = household_actual(request, db)
    if not household:
        return JSONResponse({"ok": False, "error": "sin_familia"}, status_code=400)
    today = _dt.date.today()
    month = min(max(month or today.month, 1), 12)
    user_id = u if u and any(usr.id == u for usr in household.users) else None
    data = build_dashboard_data(db, household, year or today.year, month, user_id)
    data["ok"] = True
    return JSONResponse(data)


@router.post("/api/tx/{tx_id}/delete")
def borrar_tx(tx_id: int, request: Request, db: Session = Depends(get_db)):
    household = household_actual(request, db)
    if not household:
        return JSONResponse({"ok": False}, status_code=400)
    tx = finanzas.eliminar_movimiento(db, household.id, tx_id)
    return JSONResponse({"ok": bool(tx)})


@router.post("/api/tx/{tx_id}/edit")
def editar_tx(tx_id: int, request: Request, db: Session = Depends(get_db), payload: dict = Body(...)):
    household = household_actual(request, db)
    if not household:
        return JSONResponse({"ok": False, "error": "sin_familia"}, status_code=400)
    from ..money import parse_amount
    monto = payload.get("monto")
    intento_monto = monto not in (None, "")
    amount = parse_amount(str(monto)) if intento_monto else None
    if intento_monto and not amount:  # escribió algo, pero no es un monto válido
        return JSONResponse({"ok": False, "error": "monto_invalido"}, status_code=400)
    fecha = None
    if payload.get("fecha"):
        try:
            fecha = _dt.date.fromisoformat(str(payload["fecha"])[:10])
        except ValueError:
            pass
    kind = payload.get("kind")
    categoria = payload.get("categoria")
    categoria = str(categoria) if categoria not in (None, "") else None
    desc = payload.get("descripcion")
    desc = str(desc) if desc is not None else None
    tx = finanzas.editar_movimiento(
        db, household.id, tx_id, amount=amount,
        categoria_nombre=categoria, descripcion=desc, fecha=fecha,
        kind=kind if kind in ("expense", "income") else None)
    return JSONResponse({"ok": bool(tx)})
