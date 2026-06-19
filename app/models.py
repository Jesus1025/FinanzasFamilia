"""Modelos de datos (SQLAlchemy 2.0).

Diseño multi-familia desde el dia 1: casi todo cuelga de `household_id`, asi
agregar otra familia es crear un registro, no tocar codigo.

Montos en CLP enteros (sin decimales). En transacciones, `kind` distingue
gasto de ingreso; `amount` siempre es positivo.
"""
from __future__ import annotations

import datetime as _dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


class Household(Base):
    __tablename__ = "households"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="CLP")
    timezone: Mapped[str] = mapped_column(String(64), default="America/Santiago")
    # Nombre del asistente de WhatsApp para esta familia (vacío = "Fin").
    assistant_name: Mapped[str | None] = mapped_column(String(60))
    # Código de invitación para Telegram (deep link: t.me/<bot>?start=CODE).
    invite_code: Mapped[str | None] = mapped_column(String(20), unique=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=utcnow)

    users: Mapped[list["User"]] = relationship(back_populates="household", cascade="all, delete-orphan")
    categories: Mapped[list["Category"]] = relationship(back_populates="household", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(180), unique=True)
    google_id: Mapped[str | None] = mapped_column(String(120), unique=True)
    # Numero en formato E.164 sin "+": ej 56912345678. Clave para identificar en WhatsApp.
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True)
    # Chat ID de Telegram (canal oficial). El bot solo responde a quien le escribió primero.
    telegram_chat_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    monthly_income: Mapped[int] = mapped_column(BigInteger, default=0)
    role: Mapped[str] = mapped_column(String(20), default="member")  # owner | member
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)  # allowlist
    # Google Calendar: token para crear recordatorios en el calendario del usuario.
    gcal_refresh_token: Mapped[str | None] = mapped_column(Text)
    gcal_access_token: Mapped[str | None] = mapped_column(Text)
    gcal_token_expiry: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=utcnow)

    household: Mapped[Household] = relationship(back_populates="users")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int | None] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(12), default="expense")  # expense | income
    is_ant: Mapped[bool] = mapped_column(Boolean, default=False)  # gasto hormiga (cafe, delivery...)
    emoji: Mapped[str | None] = mapped_column(String(8))

    household: Mapped[Household | None] = relationship(back_populates="categories")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(12), default="expense")  # expense | income
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)  # CLP, positivo
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    description: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[_dt.date] = mapped_column(Date, default=lambda: _dt.date.today(), index=True)
    source: Mapped[str] = mapped_column(String(16), default="whatsapp")  # whatsapp | manual | statement
    raw_text: Mapped[str | None] = mapped_column(Text)
    ai_confidence: Mapped[float | None] = mapped_column(Float)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship()
    category: Mapped[Category | None] = relationship()


class Income(Base):
    """Ingresos recurrentes extra al sueldo base (que vive en users.monthly_income)."""
    __tablename__ = "incomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    label: Mapped[str] = mapped_column(String(120))
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    day_of_month: Mapped[int | None] = mapped_column(Integer)
    recurrence: Mapped[str] = mapped_column(String(12), default="monthly")


class Bill(Base):
    """Cuentas por pagar / recordatorios."""
    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[int | None] = mapped_column(BigInteger)
    due_date: Mapped[_dt.date] = mapped_column(Date, nullable=False, index=True)
    recurrence: Mapped[str] = mapped_column(String(12), default="none")  # none | monthly
    notify_days_before: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | paid
    paid_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)
    notified_at: Mapped[_dt.datetime | None] = mapped_column(DateTime)  # evita avisar dos veces
    gcal_event_id: Mapped[str | None] = mapped_column(String(255))  # evento en Google Calendar
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=utcnow)


class Budget(Base):
    """Limite de gasto (para alertas de sobregasto). category_id NULL = limite total."""
    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    limit_amount: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PendingUser(Base):
    """Solicitud de acceso: alguien entró con Google pero el super admin aún no
    lo aprobó ni lo asignó a una familia. Se borra al aprobar (pasa a `users`)."""
    __tablename__ = "pending_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(190), unique=True, nullable=False, index=True)
    google_id: Mapped[str | None] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    # Teléfono de WhatsApp que la persona declara (para conectarla al bot y saludarla).
    phone: Mapped[str | None] = mapped_column(String(20))
    # Familia que la persona pidió unirse (la elige en /pendiente). El admin la confirma.
    requested_household_id: Mapped[int | None] = mapped_column(ForeignKey("households.id", ondelete="SET NULL"))
    gcal_refresh_token: Mapped[str | None] = mapped_column(Text)  # se conserva hasta aprobar
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=utcnow)


class AiInsight(Base):
    """Cache del análisis IA del mes (se regenera cuando cambian los movimientos)."""
    __tablename__ = "ai_insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("households.id", ondelete="CASCADE"), index=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    tx_count: Mapped[int] = mapped_column(Integer, default=0)  # invalida el cache al cambiar
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=utcnow)


class WaMessage(Base):
    """Log de mensajes de WhatsApp; el id de Meta da idempotencia (no doble-procesar)."""
    __tablename__ = "wa_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wa_message_id: Mapped[str | None] = mapped_column(String(120), unique=True, index=True)
    direction: Mapped[str] = mapped_column(String(4))  # in | out
    phone: Mapped[str] = mapped_column(String(20))
    body: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=utcnow)
