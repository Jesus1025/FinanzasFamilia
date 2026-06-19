"""Envío proactivo de mensajes por WhatsApp a través del puente Node.

Lo usa la app cuando ELLA inicia la conversación (saludo de bienvenida al
aprobar un acceso, recordatorios, etc.), no para responder mensajes entrantes.
Si el puente no está conectado, el envío falla en silencio (no rompe el flujo).
"""
from __future__ import annotations

import httpx

from ..config import config
from ..logger import get_logger
from . import finanzas

log = get_logger("whatsapp")


def normaliza_destino(phone: str | None) -> str:
    """Mismo canónico que el almacenamiento (finanzas.normaliza_phone), para que
    el destino del envío coincida exactamente con el teléfono guardado."""
    return finanzas.normaliza_phone(phone)


def enviar(to: str | None, message: str) -> bool:
    """Envía un mensaje por el puente. Devuelve True si el puente lo aceptó."""
    destino = normaliza_destino(to)
    if not destino or not message:
        return False
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(
                f"{config.WHATSAPP_BRIDGE_URL}/send",
                json={"to": destino, "message": message},
                headers={"X-Bridge-Token": config.WHATSAPP_BRIDGE_TOKEN},
            )
            ok = r.status_code == 200 and bool(r.json().get("ok"))
            if not ok:
                log.info("Puente no envió a %s: %s", destino, r.text[:120])
            return ok
    except (httpx.HTTPError, ValueError) as e:
        log.info("No se pudo enviar WhatsApp a %s (¿puente apagado?): %s", destino, e)
        return False
