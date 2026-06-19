"""Helpers compartidos entre routers: familia activa y armado de datos del dashboard."""
from __future__ import annotations

import datetime as _dt

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import config
from ..models import Household, PendingUser, User
from ..services import finanzas


# ---------------------------------------------------------------------------
# Sesión / autenticación (login con Google)
# ---------------------------------------------------------------------------
def usuario_logueado(request: Request, db: Session) -> User | None:
    """Perfil de usuario ya aprobado, según el correo guardado en la sesión."""
    email = request.session.get("email")
    if not email:
        return None
    return db.scalars(select(User).where(User.email == email, User.is_active.is_(True))).first()


def pending_logueado(request: Request, db: Session) -> PendingUser | None:
    """Solicitud de acceso pendiente, según el correo de la sesión."""
    email = request.session.get("email")
    if not email:
        return None
    return db.scalars(select(PendingUser).where(PendingUser.email == email)).first()


def es_super_admin(request: Request) -> bool:
    """¿La sesión corresponde al super admin (por correo de Google o password)?"""
    email = request.session.get("email")
    if email and config.SUPER_ADMIN_EMAIL and email == config.SUPER_ADMIN_EMAIL:
        return True
    if request.session.get("is_admin"):
        return True
    # Modo local abierto: sin password de panel y sin OAuth configurado.
    return not config.ADMIN_PASSWORD and not config.GOOGLE_OAUTH_ENABLED


def household_actual(request: Request, db: Session) -> Household | None:
    """Familia que la sesión actual puede ver.

    - Super admin: la familia seleccionada (`?hh=`) o la primera.
    - Usuario aprobado: SU familia (no puede ver otras).
    - Sin login y con OAuth activo: ninguna (acceso bloqueado).
    - Sin login y sin OAuth (modo local): la primera familia.
    """
    hhs = finanzas.households(db)
    if not hhs:
        return None
    if es_super_admin(request):
        hh_id = request.session.get("hh_id")
        return next((h for h in hhs if h.id == hh_id), hhs[0])
    u = usuario_logueado(request, db)
    if u:
        return next((h for h in hhs if h.id == u.household_id), None)
    if config.GOOGLE_OAUTH_ENABLED:
        return None
    return hhs[0]


def user_de_familia(request: Request, db: Session, user_id: int) -> User | None:
    """Usuario `user_id` SOLO si pertenece a la familia activa de la sesión.

    Evita que un formulario manipulado escriba en otra familia (el user_id viene
    del cliente; aquí lo anclamos al household que la sesión tiene seleccionado).
    """
    hh = household_actual(request, db)
    if not hh:
        return None
    u = db.get(User, user_id)
    return u if u and u.household_id == hh.id else None


def build_dashboard_data(db: Session, household: Household, year: int, month: int,
                         user_id: int | None = None) -> dict:
    """Snapshot JSON con todo lo que la UI necesita para pintar (y repintar) el dashboard."""
    today = _dt.date.today()
    r = finanzas.resumen_mes(db, household, year, month, user_id=user_id)
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    r_prev = finanzas.resumen_mes(db, household, prev_y, prev_m, user_id=user_id)

    txs = finanzas.transacciones_mes(db, household, year, month, limite=120, user_id=user_id)
    bills = finanzas.bills_pendientes(db, household)

    return {
        "year": year, "month": month, "mes_nombre": r["mes_nombre"],
        "hoy": today.isoformat(),
        "es_mes_actual": (year == today.year and month == today.month),
        "dia_hoy": today.day,
        "resumen": {k: r[k] for k in ("ingresos", "sueldos", "ingresos_extra", "gastos",
                                      "bills_pendientes", "disponible", "hormigas")},
        "anterior": {"gastos": r_prev["gastos"], "ingresos": r_prev["ingresos"],
                     "hormigas": r_prev["hormigas"], "disponible": r_prev["disponible"]},
        "por_categoria": r["por_categoria"],
        "por_persona": r["por_persona"],
        "serie_meses": finanzas.serie_meses(db, household, year, month, 6, user_id=user_id),
        "diario": {
            "actual": finanzas.gastos_diarios_acumulados(db, household.id, year, month, user_id),
            "anterior": finanzas.gastos_diarios_acumulados(db, household.id, prev_y, prev_m, user_id),
        },
        "presupuestos": finanzas.presupuestos_estado(db, household, year, month),
        "bills": [{"id": b.id, "label": b.label, "monto": b.amount,
                   "vence": b.due_date.isoformat(),
                   "dias": (b.due_date - today).days} for b in bills],
        "txs": [{"id": t.id, "fecha": t.occurred_at.isoformat(),
                 "kind": t.kind, "monto": t.amount,
                 "categoria": (t.category.name if t.category else ""),
                 "emoji": (t.category.emoji if t.category and t.category.emoji else ""),
                 "persona": (t.user.name if t.user else ""),
                 "descripcion": t.description or t.raw_text or "",
                 "fuente": t.source} for t in txs],
    }
