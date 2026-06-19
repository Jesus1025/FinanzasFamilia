"""Crea la familia de ejemplo con dos personas, categorias y movimientos demo.

Uso:  python scripts/seed.py
Es idempotente: si ya hay una familia, no hace nada.
EDITA los telefonos (formato 569XXXXXXXX) por los reales de cada persona.
"""
import datetime as _dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal, init_db  # noqa: E402
from app.defaults import DEFAULT_CATEGORIES  # noqa: E402
from app.models import Bill, Category, Household, Transaction, User  # noqa: E402


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        if db.query(Household).first():
            print("Ya existe una familia. Nada que hacer.")
            return

        hh = Household(name="Familia", currency="CLP", timezone="America/Santiago")
        db.add(hh)
        db.commit()
        db.refresh(hh)

        cats: dict[str, Category] = {}
        for nombre, kind, ant, emoji in DEFAULT_CATEGORIES:
            c = Category(household_id=hh.id, name=nombre, kind=kind, is_ant=ant, emoji=emoji)
            db.add(c)
            cats[nombre] = c
        db.commit()

        # >>> EDITA estos telefonos por los reales (formato 569XXXXXXXX, sin +) <<<
        u1 = User(household_id=hh.id, name="Persona 1", phone="56911111111",
                  monthly_income=900_000, role="owner")
        u2 = User(household_id=hh.id, name="Persona 2", phone="56922222222",
                  monthly_income=750_000)
        db.add_all([u1, u2])
        db.commit()

        hoy = _dt.date.today()

        def tx(user, kind, monto, categoria, desc, dia_offset=0):
            db.add(Transaction(
                household_id=hh.id, user_id=user.id, kind=kind, amount=monto,
                category_id=cats[categoria].id, description=desc,
                occurred_at=hoy - _dt.timedelta(days=dia_offset), source="manual",
            ))

        tx(u1, "expense", 42_000, "Supermercado", "Compra semanal", 1)
        tx(u1, "expense", 15_000, "Bencina", "Bencina auto", 2)
        tx(u2, "expense", 8_500, "Café", "Café con la amiga", 2)
        tx(u2, "expense", 12_900, "Delivery", "PedidosYa", 3)
        tx(u1, "expense", 350_000, "Arriendo", "Arriendo depto", 5)
        tx(u2, "expense", 23_000, "Cuentas/Servicios", "Cuenta de luz", 6)
        tx(u1, "expense", 6_990, "Suscripciones", "Spotify", 7)
        tx(u2, "expense", 18_000, "Salud", "Farmacia", 8)
        tx(u2, "income", 50_000, "Bono/Extra", "Bono puntualidad", 4)
        db.commit()

        # Un recordatorio de ejemplo (dia 20 de este mes)
        try:
            venc = hoy.replace(day=20)
        except ValueError:
            venc = hoy
        if venc < hoy:
            venc = (hoy.replace(day=1) + _dt.timedelta(days=32)).replace(day=20)
        db.add(Bill(household_id=hh.id, created_by=u1.id, label="Tarjeta de crédito",
                    amount=120_000, due_date=venc, notify_days_before=3))
        db.commit()

        print("OK Seed listo: familia 'Familia' con 2 personas, categorias y movimientos demo.")
        print("   Recuerda editar los telefonos reales en el dashboard o en este script.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
