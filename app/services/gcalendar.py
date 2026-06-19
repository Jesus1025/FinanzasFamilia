"""Integración con Google Calendar: agenda recordatorios en el calendario del
usuario usando su refresh_token (obtenido al entrar con Google y autorizar Calendar).

Todo es best-effort: si el usuario no conectó Calendar o la API falla, no se
rompe el flujo (el recordatorio por WhatsApp sigue funcionando igual).
"""
from __future__ import annotations

import datetime as _dt

import httpx
from sqlalchemy.orm import Session

from ..config import config
from ..logger import get_logger
from ..models import User

log = get_logger("gcal")

TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def _utcnow() -> _dt.datetime:
    """UTC naive (para comparar con lo guardado en SQLite sin líos de zona)."""
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


def conectado(user: User | None) -> bool:
    return bool(user and getattr(user, "gcal_refresh_token", None))


def _access_token(db: Session, user: User) -> str | None:
    """Devuelve un access_token válido, refrescándolo si hace falta."""
    if (user.gcal_access_token and user.gcal_token_expiry
            and user.gcal_token_expiry > _utcnow() + _dt.timedelta(seconds=60)):
        return user.gcal_access_token
    if not user.gcal_refresh_token:
        return None
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(TOKEN_URL, data={
                "client_id": config.GOOGLE_CLIENT_ID,
                "client_secret": config.GOOGLE_CLIENT_SECRET,
                "refresh_token": user.gcal_refresh_token,
                "grant_type": "refresh_token",
            })
            r.raise_for_status()
            j = r.json()
        user.gcal_access_token = j["access_token"]
        user.gcal_token_expiry = _utcnow() + _dt.timedelta(seconds=int(j.get("expires_in", 3600)))
        db.commit()
        return user.gcal_access_token
    except (httpx.HTTPError, KeyError, ValueError) as e:
        detalle = e.response.text[:200] if isinstance(e, httpx.HTTPStatusError) else str(e)
        log.warning("No pude refrescar el token de Calendar de %s: %s", user.email, detalle)
        # Si el refresh_token fue revocado, lo limpiamos para no reintentar en vano.
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 400:
            user.gcal_refresh_token = None
            db.commit()
        return None


def crear_evento(db: Session, user: User, titulo: str, fecha: _dt.date,
                 descripcion: str | None = None, notify_days_before: int = 3) -> str | None:
    """Crea un evento de día completo con recordatorios. Devuelve el id o None."""
    if not config.GOOGLE_CALENDAR_ENABLED:
        return None
    tok = _access_token(db, user)
    if not tok:
        return None
    overrides = [{"method": "popup", "minutes": 9 * 60}]  # aviso a las 9:00 del día
    if notify_days_before:
        overrides.append({"method": "popup", "minutes": notify_days_before * 24 * 60})
    body = {
        "summary": titulo[:250],
        "description": descripcion or "Recordatorio creado por Finanzas Familia 🪙",
        "start": {"date": fecha.isoformat()},
        "end": {"date": (fecha + _dt.timedelta(days=1)).isoformat()},
        "reminders": {"useDefault": False, "overrides": overrides},
    }
    try:
        with httpx.Client(timeout=15) as c:
            r = c.post(EVENTS_URL, headers={"Authorization": f"Bearer {tok}"}, json=body)
            r.raise_for_status()
            ev = r.json().get("id")
            log.info("Evento Calendar creado para %s: %s", user.email, ev)
            return ev
    except (httpx.HTTPError, ValueError) as e:
        detalle = e.response.text[:200] if isinstance(e, httpx.HTTPStatusError) else str(e)
        log.warning("No pude crear el evento en Calendar de %s: %s", user.email, detalle)
        return None


def borrar_evento(db: Session, user: User, event_id: str | None) -> bool:
    if not event_id:
        return False
    tok = _access_token(db, user)
    if not tok:
        return False
    try:
        with httpx.Client(timeout=15) as c:
            r = c.delete(f"{EVENTS_URL}/{event_id}", headers={"Authorization": f"Bearer {tok}"})
            return r.status_code in (200, 204, 410)  # 410 = ya no existe
    except httpx.HTTPError:
        return False
