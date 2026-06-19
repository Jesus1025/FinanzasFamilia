"""Login con Google (OAuth 2.0 / OIDC) + sala de espera de aprobación.

Flujo:
  /login                → página con el botón "Entrar con Google"
  /auth/google          → redirige a Google
  /auth/google/callback → vuelta de Google: valida correo, abre sesión y enruta:
      · super admin (SUPER_ADMIN_EMAIL)      → /
      · perfil ya aprobado (User con email)  → /
      · cualquier otro (cuenta válida nueva) → crea PendingUser → /pendiente
  /pendiente            → el usuario elige a qué familia quiere unirse (queda
                          a la espera de que el super admin lo apruebe)
  /logout               → cierra la sesión

Sin GOOGLE_CLIENT_ID/SECRET el botón no aparece y /auth/google da 404
(la app sigue funcionando en modo local abierto).
"""
from __future__ import annotations

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import config
from ..db import get_db
from ..logger import get_logger
from ..models import Household, PendingUser, User
from ..services import finanzas
from ..templating import templates
from .helpers import pending_logueado, usuario_logueado

log = get_logger("auth")
router = APIRouter(tags=["auth"])

# Cliente OAuth registrado una sola vez (resuelve los endpoints de Google solo).
_oauth = OAuth()
if config.GOOGLE_OAUTH_ENABLED:
    _oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=config.GOOGLE_CLIENT_ID,
        client_secret=config.GOOGLE_CLIENT_SECRET,
        client_kwargs={"scope": config.GOOGLE_SCOPES},
    )


def _enabled_or_404() -> None:
    if not config.GOOGLE_OAUTH_ENABLED:
        raise HTTPException(status_code=404, detail="Login con Google no configurado")


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db), error: str | None = None):
    # Ya logueado: directo a donde corresponda.
    if usuario_logueado(request, db) or request.session.get("email") == config.SUPER_ADMIN_EMAIL:
        return RedirectResponse("/", status_code=303)
    if pending_logueado(request, db):
        return RedirectResponse("/pendiente", status_code=303)
    return templates.TemplateResponse(request, "login.html", {
        "oauth": config.GOOGLE_OAUTH_ENABLED,
        "dominio": config.GOOGLE_HOSTED_DOMAIN,
        "error": error,
    })


@router.get("/auth/google")
async def google_login(request: Request):
    _enabled_or_404()
    redirect_uri = f"{config.APP_URL}/auth/google/callback"
    kwargs = {}
    if config.GOOGLE_HOSTED_DOMAIN:
        kwargs["hd"] = config.GOOGLE_HOSTED_DOMAIN
    if config.GOOGLE_CALENDAR:
        # offline + consent => Google entrega un refresh_token para agendar
        # eventos aunque la persona no tenga sesión abierta (ej. desde WhatsApp).
        kwargs["access_type"] = "offline"
        kwargs["prompt"] = "consent"
    return await _oauth.google.authorize_redirect(request, redirect_uri, **kwargs)


@router.get("/auth/google/callback", name="google_callback")
async def google_callback(request: Request, db: Session = Depends(get_db)):
    _enabled_or_404()
    try:
        token = await _oauth.google.authorize_access_token(request)
    except OAuthError as e:
        log.warning("Error OAuth Google: %s", e)
        return RedirectResponse("/login?error=oauth", status_code=303)

    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower().strip()
    verificado = bool(info.get("email_verified"))
    nombre = (info.get("name") or info.get("given_name") or "").strip()
    google_id = info.get("sub")
    dominio = email.split("@")[-1] if "@" in email else ""

    if not email or not verificado:
        return RedirectResponse("/login?error=no_verificado", status_code=303)
    if config.GOOGLE_HOSTED_DOMAIN and dominio != config.GOOGLE_HOSTED_DOMAIN:
        log.info("Dominio no permitido en login: %s", email)
        return RedirectResponse("/login?error=dominio", status_code=303)

    # refresh_token de Google Calendar (solo viene con access_type=offline).
    refresh = token.get("refresh_token")

    request.session.clear()
    request.session["email"] = email
    request.session["nombre"] = nombre or email.split("@")[0]
    log.info("Login Google: %s (calendar=%s)", email, "sí" if refresh else "no")

    # Si ya hay perfil con este correo, guardamos su token de Calendar.
    u = db.scalars(select(User).where(User.email == email)).first()
    if u and refresh:
        u.gcal_refresh_token = refresh
        u.gcal_access_token = None  # forzar refresco con el nuevo refresh_token
        u.gcal_token_expiry = None
        db.commit()

    # Super admin: entra siempre, aunque no tenga perfil en ninguna familia.
    if config.SUPER_ADMIN_EMAIL and email == config.SUPER_ADMIN_EMAIL:
        return RedirectResponse("/", status_code=303)

    # Perfil ya aprobado.
    if u and u.is_active:
        return RedirectResponse("/", status_code=303)
    if u and not u.is_active:
        request.session.clear()
        return RedirectResponse("/login?error=desactivado", status_code=303)

    # Cuenta nueva válida: queda pendiente de aprobación (conservamos su token).
    p = db.scalars(select(PendingUser).where(PendingUser.email == email)).first()
    if not p:
        p = PendingUser(email=email, google_id=google_id, name=nombre or email.split("@")[0])
        db.add(p)
    if google_id and not p.google_id:
        p.google_id = google_id
    if refresh:
        p.gcal_refresh_token = refresh
    db.commit()
    return RedirectResponse("/pendiente", status_code=303)


@router.get("/pendiente", response_class=HTMLResponse)
def pendiente_page(request: Request, db: Session = Depends(get_db)):
    # Si ya fue aprobado (o es admin), no tiene nada que esperar.
    if usuario_logueado(request, db) or request.session.get("email") == config.SUPER_ADMIN_EMAIL:
        return RedirectResponse("/", status_code=303)
    p = pending_logueado(request, db)
    if not p:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "pendiente.html", {
        "pending": p, "households": finanzas.households(db),
    })


@router.post("/pendiente")
def pendiente_guardar(request: Request, db: Session = Depends(get_db),
                      nombre: str = Form(...), household_id: str = Form(""),
                      phone: str = Form("")):
    p = pending_logueado(request, db)
    if not p:
        return RedirectResponse("/login", status_code=303)
    p.name = nombre.strip()[:150] or p.name
    p.phone = finanzas.normaliza_phone(phone) or None
    try:
        p.requested_household_id = int(household_id) if household_id else None
    except ValueError:
        p.requested_household_id = None
    db.commit()
    return RedirectResponse("/pendiente", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login" if config.GOOGLE_OAUTH_ENABLED else "/", status_code=303)
