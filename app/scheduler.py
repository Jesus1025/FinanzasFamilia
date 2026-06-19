"""Planificador de recordatorios: cada hora revisa cuentas por vencer y avisa
por Telegram (y por WhatsApp si el puente sigue activo). Marca lo enviado para no repetir."""
from __future__ import annotations

import asyncio
import datetime as _dt

from sqlalchemy import select

from .config import config
from .db import SessionLocal
from .logger import get_logger
from .models import Bill, User, utcnow
from .money import format_clp

log = get_logger("scheduler")


async def _enviar_whatsapp(to: str, message: str) -> bool:
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{config.WHATSAPP_BRIDGE_URL}/send",
                json={"to": to, "message": message},
                headers={"X-Bridge-Token": config.WHATSAPP_BRIDGE_TOKEN},
            )
            return r.status_code == 200 and bool(r.json().get("ok"))
    except Exception:
        return False


async def revisar_recordatorios() -> None:
    today = _dt.date.today()
    db = SessionLocal()
    try:
        bills = db.scalars(select(Bill).where(
            Bill.status == "pending", Bill.notified_at.is_(None))).all()

        for b in bills:
            if today < b.due_date - _dt.timedelta(days=b.notify_days_before):
                continue

            monto = f" por {format_clp(b.amount)}" if b.amount else ""
            msg = (f"⏰ Recordatorio: el {b.due_date.strftime('%d/%m')} "
                   f"vence *{b.label}*{monto}.")

            users = db.scalars(select(User).where(
                User.household_id == b.household_id, User.is_active.is_(True))).all()

            enviado = False

            # 1. Telegram (canal principal)
            from .services import telegram as tg
            for u in users:
                if u.telegram_chat_id:
                    if await tg.enviar_texto(u.telegram_chat_id, msg):
                        enviado = True

            # 2. WhatsApp (fallback, si aún hay puente)
            if not enviado:
                for u in users:
                    if u.phone:
                        if await _enviar_whatsapp(u.phone, msg):
                            enviado = True

            if enviado:
                b.notified_at = utcnow()
                log.info("Recordatorio enviado: %s", b.label)

        db.commit()
    finally:
        db.close()


async def loop() -> None:
    await asyncio.sleep(15)
    while True:
        try:
            await revisar_recordatorios()
        except Exception as e:
            log.warning("Error en el scheduler: %s", e)
        await asyncio.sleep(3600)
