"""Configuracion central, cargada desde variables de entorno (.env)."""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on", "si", "sí"}


class Config:
    ENV = os.getenv("ENV", "development")
    DEBUG = ENV == "development"

    DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./finanzas.db")
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    # Puertos distintos a los del 99520 (8080/8090) para que ambos convivan.
    APP_PORT = int(os.getenv("APP_PORT", "8088"))
    APP_URL = os.getenv("APP_URL", "http://localhost:8088").rstrip("/")

    CURRENCY = os.getenv("CURRENCY", "CLP")
    TIMEZONE = os.getenv("TIMEZONE", "America/Santiago")

    # --- DeepSeek (API compatible con OpenAI) ---
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")

    # --- Puente WhatsApp (Node, whatsapp-web.js) ---
    WHATSAPP_BRIDGE_URL = os.getenv("WHATSAPP_BRIDGE_URL", "http://localhost:8099").rstrip("/")
    WHATSAPP_BRIDGE_TOKEN = os.getenv("WHATSAPP_BRIDGE_TOKEN", "bridge-dev-token")

    # --- Login Google (OAuth 2.0 / OIDC, opcional) ---
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    # Si se define, solo se aceptan correos de ese dominio (ej: "miempresa.cl").
    # Vacío = cualquier cuenta de Google (lo normal para familias con Gmail).
    GOOGLE_HOSTED_DOMAIN = os.getenv("GOOGLE_HOSTED_DOMAIN", "").strip().lower()
    # Pedir permiso de Google Calendar al entrar (para agendar recordatorios).
    GOOGLE_CALENDAR = _bool(os.getenv("GOOGLE_CALENDAR"), True)

    # --- Telegram (bot oficial, reemplaza al puente WhatsApp) ---
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").strip()
    TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()

    # --- Super admin (panel /admin: crear familias, perfiles, aprobar accesos) ---
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
    # Correo que, al entrar con Google, es super admin automático (sin password).
    SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL", "").strip().lower()

    @property
    def DEEPSEEK_ENABLED(self) -> bool:
        return bool(self.DEEPSEEK_API_KEY)

    @property
    def GOOGLE_OAUTH_ENABLED(self) -> bool:
        return bool(self.GOOGLE_CLIENT_ID and self.GOOGLE_CLIENT_SECRET)

    @property
    def TELEGRAM_ENABLED(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN)

    TELEGRAM_API = "https://api.telegram.org"

    @property
    def telegram_webhook_secret(self) -> str:
        return self.TELEGRAM_WEBHOOK_SECRET or self.SECRET_KEY

    @property
    def GOOGLE_CALENDAR_ENABLED(self) -> bool:
        return self.GOOGLE_OAUTH_ENABLED and self.GOOGLE_CALENDAR

    @property
    def GOOGLE_SCOPES(self) -> str:
        s = "openid email profile"
        if self.GOOGLE_CALENDAR:
            s += " https://www.googleapis.com/auth/calendar.events"
        return s


config = Config()
