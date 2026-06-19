"""Panel de super admin: gestionar familias (households) y sus perfiles.

Protegido por ADMIN_PASSWORD (.env). Si no hay password definida, el panel
queda abierto (modo desarrollo) y se muestra una advertencia.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import config
from ..db import get_db
from ..logger import get_logger
from ..models import (AiInsight, Bill, Budget, Category, Household, Income,
                      PendingUser, Transaction, User)
from ..services import finanzas, telegram as tg, whatsapp
from ..templating import templates
from .helpers import es_super_admin

log = get_logger("admin")
router = APIRouter(prefix="/admin", tags=["admin"])


def es_admin(request: Request) -> bool:
    """Alias estable: el panel /admin lo controla el super admin."""
    return es_super_admin(request)


def _requiere_admin(request: Request) -> RedirectResponse | None:
    if not es_admin(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if es_admin(request):
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(request, "admin_login.html", {"error": None})


@router.post("/login")
def login(request: Request, password: str = Form(...)):
    if config.ADMIN_PASSWORD and secrets.compare_digest(password, config.ADMIN_PASSWORD):
        request.session["is_admin"] = True
        return RedirectResponse("/admin", status_code=303)
    log.warning("Intento de login admin fallido")
    return templates.TemplateResponse(request, "admin_login.html",
                                      {"error": "Contraseña incorrecta"}, status_code=401)


@router.post("/logout")
def logout(request: Request):
    request.session.pop("is_admin", None)
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
def panel(request: Request, db: Session = Depends(get_db)):
    if (r := _requiere_admin(request)):
        return r
    import datetime as _dt
    today = _dt.date.today()
    hogares = finanzas.households(db)
    nombres_hh = {h.id: h.name for h in hogares}
    filas = []
    for hh in hogares:
        resumen = finanzas.resumen_mes(db, hh, today.year, today.month)
        filas.append({"hh": hh, "users": sorted(hh.users, key=lambda u: u.id),
                      "gastos_mes": resumen["gastos"], "disponible": resumen["disponible"]})
    pendientes = [
        {"p": p, "familia_solicitada": nombres_hh.get(p.requested_household_id)}
        for p in db.scalars(select(PendingUser).order_by(PendingUser.created_at))
    ]
    return templates.TemplateResponse(request, "admin.html", {
        "filas": filas, "households": hogares, "pendientes": pendientes,
        "sin_password": not config.ADMIN_PASSWORD and not config.GOOGLE_OAUTH_ENABLED,
        "oauth": config.GOOGLE_OAUTH_ENABLED,
        "deepseek": config.DEEPSEEK_ENABLED, "modelo": config.DEEPSEEK_MODEL,
        "telegram": config.TELEGRAM_ENABLED,
        "telegram_bot_username": tg.bot_username(),
    })


# ---------------------------------------------------------------------------
# Familias
# ---------------------------------------------------------------------------
@router.post("/household")
def crear_household(request: Request, db: Session = Depends(get_db), name: str = Form(...)):
    if (r := _requiere_admin(request)):
        return r
    hh = finanzas.crear_household(db, name)
    log.info("Familia creada: %s (id=%s)", hh.name, hh.id)
    return RedirectResponse("/admin", status_code=303)


@router.post("/household/{hh_id}/rename")
def renombrar_household(hh_id: int, request: Request, db: Session = Depends(get_db),
                        name: str = Form(...), assistant_name: str = Form("")):
    if (r := _requiere_admin(request)):
        return r
    hh = db.get(Household, hh_id)
    if hh and name.strip():
        hh.name = name.strip()[:120]
        hh.assistant_name = assistant_name.strip()[:60] or None
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/household/{hh_id}/delete")
def eliminar_household(hh_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := _requiere_admin(request)):
        return r
    hh = db.get(Household, hh_id)
    if hh:
        # SQLite no aplica ON DELETE CASCADE por defecto: limpiamos a mano.
        db.execute(delete(Transaction).where(Transaction.household_id == hh_id))
        db.execute(delete(Bill).where(Bill.household_id == hh_id))
        db.execute(delete(Budget).where(Budget.household_id == hh_id))
        db.execute(delete(Income).where(Income.household_id == hh_id))
        db.execute(delete(AiInsight).where(AiInsight.household_id == hh_id))
        db.execute(delete(User).where(User.household_id == hh_id))
        db.execute(delete(Category).where(Category.household_id == hh_id))
        db.delete(hh)
        db.commit()
        log.info("Familia eliminada: %s (id=%s)", hh.name, hh_id)
        if request.session.get("hh_id") == hh_id:
            request.session.pop("hh_id", None)
    return RedirectResponse("/admin", status_code=303)


# ---------------------------------------------------------------------------
# Perfiles (miembros)
# ---------------------------------------------------------------------------
@router.post("/user")
def crear_usuario(request: Request, db: Session = Depends(get_db),
                  household_id: int = Form(...), name: str = Form(...),
                  phone: str = Form(""), monthly_income: str = Form("0"),
                  role: str = Form("member")):
    if (r := _requiere_admin(request)):
        return r
    phone_norm = finanzas.normaliza_phone(phone) or None
    if phone_norm and db.scalar(select(func.count(User.id)).where(User.phone == phone_norm)):
        log.warning("Teléfono duplicado al crear usuario: %s", phone_norm)
        return RedirectResponse("/admin?error=telefono_duplicado", status_code=303)
    from ..money import parse_amount
    u = User(household_id=household_id, name=name.strip()[:120] or "Sin nombre",
             phone=phone_norm, monthly_income=parse_amount(monthly_income) or 0,
             role=role if role in ("owner", "member") else "member")
    db.add(u)
    db.commit()
    log.info("Perfil creado: %s (familia %s)", u.name, household_id)
    return RedirectResponse("/admin", status_code=303)


@router.post("/user/{user_id}/update")
def actualizar_usuario(user_id: int, request: Request, db: Session = Depends(get_db),
                       name: str = Form(...), phone: str = Form(""),
                       monthly_income: str = Form("0"), role: str = Form("member")):
    if (r := _requiere_admin(request)):
        return r
    u = db.get(User, user_id)
    if u:
        phone_norm = finanzas.normaliza_phone(phone) or None
        if phone_norm:
            dup = db.scalars(select(User).where(User.phone == phone_norm, User.id != user_id)).first()
            if dup:
                return RedirectResponse("/admin?error=telefono_duplicado", status_code=303)
        from ..money import parse_amount
        u.name = name.strip()[:120] or u.name
        u.phone = phone_norm
        u.monthly_income = parse_amount(monthly_income) or 0
        u.role = role if role in ("owner", "member") else u.role
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/user/{user_id}/toggle")
def toggle_usuario(user_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := _requiere_admin(request)):
        return r
    u = db.get(User, user_id)
    if u:
        u.is_active = not u.is_active
        db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/user/{user_id}/delete")
def eliminar_usuario(user_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := _requiere_admin(request)):
        return r
    u = db.get(User, user_id)
    if u:
        tiene_movs = db.scalar(select(func.count(Transaction.id)).where(Transaction.user_id == user_id))
        if tiene_movs:
            # Conserva el historial: solo lo desactivamos.
            u.is_active = False
            db.commit()
            return RedirectResponse("/admin?error=usuario_con_movimientos", status_code=303)
        db.execute(delete(Bill).where(Bill.created_by == user_id))
        db.delete(u)
        db.commit()
    return RedirectResponse("/admin", status_code=303)


# ---------------------------------------------------------------------------
# Solicitudes de acceso (usuarios que entraron con Google, pendientes)
# ---------------------------------------------------------------------------
@router.post("/pending/{pending_id}/aprobar")
def aprobar_pendiente(pending_id: int, request: Request, background: BackgroundTasks,
                      db: Session = Depends(get_db),
                      household_id: int = Form(...), role: str = Form("member")):
    """Aprueba una solicitud: crea el perfil en la familia, lo conecta a su
    teléfono y lo saluda por WhatsApp (si dejó número)."""
    if (r := _requiere_admin(request)):
        return r
    p = db.get(PendingUser, pending_id)
    hh = db.get(Household, household_id)
    if not p or not hh:
        return RedirectResponse("/admin?error=solicitud_invalida", status_code=303)

    nombre_persona = (p.name or p.email.split("@")[0])[:120]
    correo = p.email
    # El teléfono solo se asigna si no lo usa ya OTRO perfil (User.phone es único).
    telefono = p.phone or None
    if telefono and db.scalars(select(User).where(User.phone == telefono, User.email != correo)).first():
        telefono = None

    existente = db.scalars(select(User).where(User.email == correo)).first()
    if existente:
        existente.household_id = hh.id
        existente.is_active = True
        existente.google_id = existente.google_id or p.google_id
        if telefono and not existente.phone:
            existente.phone = telefono
        telefono_saludo = existente.phone  # el que realmente queda en la cuenta
    else:
        db.add(User(household_id=hh.id, name=nombre_persona, email=correo,
                    google_id=p.google_id, phone=telefono,
                    role=role if role in ("owner", "member") else "member",
                    is_active=True))
        telefono_saludo = telefono
    db.delete(p)
    try:
        db.commit()
    except IntegrityError:
        # Choque con otro perfil (email/teléfono/google_id ya en uso): no romper.
        db.rollback()
        log.warning("Conflicto al aprobar acceso de %s", correo)
        return RedirectResponse("/admin?error=conflicto_acceso", status_code=303)
    log.info("Acceso aprobado: %s -> familia %s (%s)", correo, hh.name, hh.id)

    # Saludo de bienvenida por WhatsApp: solo al número que quedó vinculado.
    if telefono_saludo:
        nombre_bot = (hh.assistant_name or "").strip() or "Fin"
        msg = (f"¡Hola {nombre_persona.split(' ')[0]}! 👋 Soy *{nombre_bot}*, el asistente de "
               f"finanzas de *{hh.name}*.\n\nYa tienes acceso ✅. Anota tus gastos hablándome "
               f"normal, por ejemplo:\n• \"gasté 5 lucas en café\"\n• \"¿cuánto llevo este mes?\"\n\n"
               f"¡Pruébame cuando quieras! 🪙")
        background.add_task(whatsapp.enviar, telefono_saludo, msg)
    return RedirectResponse("/admin", status_code=303)


@router.post("/pending/{pending_id}/rechazar")
def rechazar_pendiente(pending_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := _requiere_admin(request)):
        return r
    p = db.get(PendingUser, pending_id)
    if p:
        db.delete(p)
        db.commit()
        log.info("Solicitud de acceso rechazada: %s", p.email)
    return RedirectResponse("/admin", status_code=303)


# ---------------------------------------------------------------------------
# Telegram: código de invitación por familia
# ---------------------------------------------------------------------------
@router.post("/household/{hh_id}/invite")
def generar_invite(hh_id: int, request: Request, db: Session = Depends(get_db)):
    if (r := _requiere_admin(request)):
        return r
    hh = db.get(Household, hh_id)
    if not hh:
        return RedirectResponse("/admin", status_code=303)
    from ..services import telegram as tg
    hh.invite_code = tg.generar_codigo_invitacion()
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.get("/telegram/estado")
async def telegram_estado(request: Request):
    if (r := _requiere_admin(request)):
        return r
    from ..services import telegram as tg
    return JSONResponse(await tg.estado())


# Endpoint público para que el JS del panel admin pueda chequear el estado
# sin necesidad de sesión de admin (usa el mismo secreto de sesión como token).
@router.get("/api/telegram/estado")
async def telegram_estado_publico(request: Request):
    from ..services import telegram as tg
    return JSONResponse(await tg.estado())
