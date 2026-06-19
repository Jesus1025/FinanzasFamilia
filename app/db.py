"""Conexion a la base de datos y sesion de SQLAlchemy."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import config

_connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


def get_db():
    """Dependencia de FastAPI: entrega una sesion y la cierra al terminar."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Crea las tablas si no existen y aplica migraciones ligeras idempotentes."""
    from . import models  # noqa: F401  (registra los modelos en Base.metadata)

    Base.metadata.create_all(bind=engine)
    _migraciones_ligeras()


def _migraciones_ligeras() -> None:
    """ALTERs no destructivos para BD que ya existían (SQLite no añade columnas
    nuevas con create_all). Cada paso es idempotente."""
    if not config.DATABASE_URL.startswith("sqlite"):
        return
    def _cols(tabla: str) -> set[str]:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({tabla})")}

    with engine.begin() as conn:
        hcols = _cols("households")
        if "assistant_name" not in hcols:
            conn.exec_driver_sql("ALTER TABLE households ADD COLUMN assistant_name VARCHAR(60)")
        if "invite_code" not in hcols:
            conn.exec_driver_sql("ALTER TABLE households ADD COLUMN invite_code VARCHAR(20)")
        pcols = _cols("pending_users")
        if pcols and "phone" not in pcols:
            conn.exec_driver_sql("ALTER TABLE pending_users ADD COLUMN phone VARCHAR(20)")
        if pcols and "gcal_refresh_token" not in pcols:
            conn.exec_driver_sql("ALTER TABLE pending_users ADD COLUMN gcal_refresh_token TEXT")
        ucols = _cols("users")
        for col, ddl in (("gcal_refresh_token", "TEXT"), ("gcal_access_token", "TEXT"),
                         ("gcal_token_expiry", "DATETIME")):
            if col not in ucols:
                conn.exec_driver_sql(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        bcols = _cols("bills")
        if "gcal_event_id" not in bcols:
            conn.exec_driver_sql("ALTER TABLE bills ADD COLUMN gcal_event_id VARCHAR(255)")
        if "telegram_chat_id" not in ucols:
            conn.exec_driver_sql("ALTER TABLE users ADD COLUMN telegram_chat_id VARCHAR(40)")
