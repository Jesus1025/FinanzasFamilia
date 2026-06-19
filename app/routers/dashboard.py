"""Dashboard web: resumen mensual, alta manual de movimientos y conexión QR."""
from __future__ import annotations

import datetime as _dt

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..config import config
from ..db import get_db
from ..models import User
from ..money import parse_amount
from ..services import finanzas
from ..templating import templates
from .admin import es_admin
from .helpers import (build_dashboard_data, household_actual, pending_logueado,
                      usuario_logueado, user_de_familia)

router = APIRouter(tags=["dashboard"])


async def _bridge_get(path: str, params: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{config.WHATSAPP_BRIDGE_URL}{path}", params=params or {},
                            headers={"X-Bridge-Token": config.WHATSAPP_BRIDGE_TOKEN})
            return r.json()
    except (httpx.HTTPError, ValueError):
        return {}


async def _bridge_post(path: str, payload: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{config.WHATSAPP_BRIDGE_URL}{path}", json=payload or {},
                             headers={"X-Bridge-Token": config.WHATSAPP_BRIDGE_TOKEN})
            return r.json()
    except (httpx.HTTPError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Vista principal
# ---------------------------------------------------------------------------
@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db),
         year: int | None = None, month: int | None = None, hh: int | None = None):
    # Con login Google activo, el dashboard requiere sesión (salvo super admin).
    if config.GOOGLE_OAUTH_ENABLED and not es_admin(request):
        if not usuario_logueado(request, db):
            destino = "/pendiente" if pending_logueado(request, db) else "/login"
            return RedirectResponse(destino, status_code=303)

    # Cambio de familia (solo admin): /?hh=2
    if hh is not None and es_admin(request):
        request.session["hh_id"] = hh

    household = household_actual(request, db)
    if not household:
        # Super admin sin familias aún: pantalla de setup. Usuario aprobado cuya
        # familia fue borrada: a login.
        if es_admin(request) or not config.GOOGLE_OAUTH_ENABLED:
            return templates.TemplateResponse(request, "setup.html")
        return RedirectResponse("/login", status_code=303)

    today = _dt.date.today()
    year = year or today.year
    month = min(max(month or today.month, 1), 12)

    data = build_dashboard_data(db, household, year, month)
    users = sorted([u for u in household.users], key=lambda u: u.id)
    cats = finanzas.categorias(db, household.id)

    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    u_log = usuario_logueado(request, db)
    gcal_conectado = bool(u_log and u_log.gcal_refresh_token)

    return templates.TemplateResponse(request, "dashboard.html", {
        "household": household, "households": finanzas.households(db),
        "es_admin": es_admin(request),
        "data": data, "users": users, "cats": cats,
        "year": year, "month": month, "today": today,
        "mes_nombre": data["mes_nombre"],
        "prev_y": prev_y, "prev_m": prev_m, "next_y": next_y, "next_m": next_m,
        "deepseek": config.DEEPSEEK_ENABLED, "modelo": config.DEEPSEEK_MODEL,
        "gcal_on": config.GOOGLE_CALENDAR_ENABLED, "gcal_conectado": gcal_conectado,
        "logueado": bool(u_log),
        "telegram_on": config.TELEGRAM_ENABLED,
    })


# ---------------------------------------------------------------------------
# Setup inicial (solo cuando no existe ninguna familia)
# ---------------------------------------------------------------------------
@router.post("/setup")
def setup(request: Request, db: Session = Depends(get_db),
          familia: str = Form(...), nombre: str = Form(...),
          phone: str = Form(""), sueldo: str = Form("")):
    if finanzas.households(db):
        return RedirectResponse("/", status_code=303)
    hh = finanzas.crear_household(db, familia)
    db.add(User(household_id=hh.id, name=nombre.strip()[:120] or "Admin",
                phone=finanzas.normaliza_phone(phone) or None,
                monthly_income=parse_amount(sueldo) or 0, role="owner"))
    db.commit()
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Acciones (alta manual desde la web)
# ---------------------------------------------------------------------------
@router.post("/sueldo")
def set_sueldo(request: Request, db: Session = Depends(get_db),
               user_id: int = Form(...), monto: str = Form(...)):
    user = user_de_familia(request, db, user_id)
    if user:
        finanzas.set_sueldo(db, user, parse_amount(monto) or 0)
    return RedirectResponse("/", status_code=303)


@router.post("/tx")
def add_tx(request: Request, db: Session = Depends(get_db), user_id: int = Form(...),
           kind: str = Form("expense"), monto: str = Form(...), categoria: str = Form(""),
           descripcion: str = Form(""), fecha: str = Form("")):
    user = user_de_familia(request, db, user_id)
    amount = parse_amount(monto)
    if user and amount:
        try:
            f = _dt.date.fromisoformat(fecha) if fecha else _dt.date.today()
        except ValueError:
            f = _dt.date.today()
        kind = kind if kind in ("expense", "income") else "expense"
        finanzas.registrar_movimiento(db, user, kind, amount, categoria or None,
                                      descripcion or None, f, source="manual")
    return RedirectResponse("/", status_code=303)


@router.post("/bill")
def add_bill(request: Request, db: Session = Depends(get_db), user_id: int = Form(...),
             label: str = Form(...), monto: str = Form(""), due_date: str = Form(...),
             notify_days_before: int = Form(3)):
    user = user_de_familia(request, db, user_id)
    if user:
        try:
            due = _dt.date.fromisoformat(due_date)
        except ValueError:
            due = _dt.date.today()
        finanzas.crear_bill(db, user, label, parse_amount(monto), due, max(0, notify_days_before))
    return RedirectResponse("/", status_code=303)


@router.post("/bill/{bill_id}/pagar")
def pagar_bill(bill_id: int, request: Request, db: Session = Depends(get_db)):
    household = household_actual(request, db)
    if household:
        finanzas.pagar_bill(db, household.id, bill_id=bill_id)
    return RedirectResponse("/", status_code=303)


@router.post("/presupuesto")
def set_presupuesto(request: Request, db: Session = Depends(get_db),
                    categoria: str = Form(""), limite: str = Form(...)):
    household = household_actual(request, db)
    monto = parse_amount(limite)
    if household and monto:
        finanzas.set_presupuesto(db, household.id, categoria or None, monto)
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Conexión de WhatsApp por QR (puente Node)
# ---------------------------------------------------------------------------
@router.get("/whatsapp", response_class=HTMLResponse)
def whatsapp_page(request: Request):
    return templates.TemplateResponse(request, "whatsapp.html")


@router.post("/whatsapp/qr/start")
async def qr_start():
    data = await _bridge_post("/start", {})
    if not data:
        return JSONResponse({"ok": False, "error": "El puente de WhatsApp no está corriendo."}, status_code=503)
    return JSONResponse({"ok": True, "estado": data.get("estado", "iniciando")})


@router.get("/whatsapp/qr/estado")
async def qr_estado():
    data = await _bridge_get("/estado")
    if not data:
        return JSONResponse({"estado": "sin_puente", "qr": None, "numero": ""})
    return JSONResponse({"estado": data.get("estado", "desconectado"),
                         "qr": data.get("qr"), "numero": data.get("numero", "")})


@router.post("/whatsapp/qr/logout")
async def qr_logout():
    await _bridge_post("/logout", {})
    return JSONResponse({"ok": True})
