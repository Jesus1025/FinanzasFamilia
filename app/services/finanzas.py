"""Logica de negocio: registrar movimientos y calcular resumenes."""
from __future__ import annotations

import calendar
import datetime as _dt
import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..defaults import DEFAULT_CATEGORIES
from ..models import Bill, Budget, Category, Household, Transaction, User, utcnow

MESES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


# ---------------------------------------------------------------------------
# Helpers de entidades
# ---------------------------------------------------------------------------
def normaliza_phone(raw: str | None) -> str:
    """Canonicaliza un teléfono a dígitos E.164 (Chile) para que SIEMPRE se
    guarde y compare igual, sin importar cómo lo escriba la persona.

    '+56 9 1234 5678' / '912345678' / '56912345678'  ->  '56912345678'
    Devuelve '' si no es un número usable (muy corto/largo), para no guardar
    basura que luego capture mensajes ajenos por coincidencia de sufijo.
    """
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 9 and d.startswith("9"):  # celular chileno sin prefijo país
        d = "56" + d
    return d if 10 <= len(d) <= 15 else ""


def household_activo(db: Session) -> Household | None:
    """Para el MVP operamos sobre la primera familia (multi-familia ya soportado en el esquema)."""
    return db.scalars(select(Household).order_by(Household.id)).first()


def households(db: Session) -> list[Household]:
    return list(db.scalars(select(Household).order_by(Household.id)))


def crear_household(db: Session, name: str) -> Household:
    """Crea una familia nueva con su set de categorías por defecto."""
    hh = Household(name=name.strip()[:120] or "Familia")
    db.add(hh)
    db.commit()
    db.refresh(hh)
    for nombre, kind, ant, emoji in DEFAULT_CATEGORIES:
        db.add(Category(household_id=hh.id, name=nombre, kind=kind, is_ant=ant, emoji=emoji))
    db.commit()
    return hh


def usuario_por_nombre(db: Session, household_id: int, nombre: str | None) -> User | None:
    """Busca un miembro de la familia por nombre (aproximado)."""
    if not nombre:
        return None
    n = nombre.strip().lower()
    miembros = list(db.scalars(select(User).where(User.household_id == household_id)))
    for u in miembros:
        if u.name.lower() == n:
            return u
    for u in miembros:
        if n in u.name.lower() or u.name.lower().split()[0] in n:
            return u
    return None


def usuario_por_telefono(db: Session, phone: str) -> User | None:
    p = normaliza_phone(phone)
    if not p:
        return None
    # Compara por los ultimos digitos para tolerar prefijos (+56 / 56 / 9...)
    for u in db.scalars(select(User).where(User.phone.isnot(None))):
        up = normaliza_phone(u.phone)
        if up and (up == p or up.endswith(p[-9:]) or p.endswith(up[-9:])):
            return u
    return None


def buscar_categoria(db: Session, household_id: int, nombre: str | None, kind: str) -> Category:
    """Encuentra una categoria por nombre (aprox.) o cae a 'Otros' / 'Otros ingresos'."""
    cond = or_(Category.household_id == household_id, Category.household_id.is_(None))
    cats = list(db.scalars(select(Category).where(cond, Category.kind == kind)))
    if nombre:
        n = nombre.strip().lower()
        for c in cats:
            if c.name.lower() == n:
                return c
        for c in cats:
            if n in c.name.lower() or c.name.lower() in n:
                return c
    fallback = "otros ingresos" if kind == "income" else "otros"
    for c in cats:
        if c.name.lower() == fallback:
            return c
    return cats[0] if cats else _crear_otros(db, household_id, kind)


def _crear_otros(db: Session, household_id: int, kind: str) -> Category:
    c = Category(household_id=household_id, name=("Otros ingresos" if kind == "income" else "Otros"),
                 kind=kind, emoji="📦")
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ---------------------------------------------------------------------------
# Registrar movimientos
# ---------------------------------------------------------------------------
def registrar_movimiento(db: Session, user: User, kind: str, amount: int, categoria_nombre: str | None,
                         descripcion: str | None, fecha: _dt.date, source: str = "whatsapp",
                         raw_text: str | None = None, ai_confidence: float | None = None) -> Transaction:
    cat = buscar_categoria(db, user.household_id, categoria_nombre, kind)
    tx = Transaction(
        household_id=user.household_id, user_id=user.id, kind=kind, amount=abs(int(amount)),
        category_id=cat.id, description=descripcion, occurred_at=fecha, source=source,
        raw_text=raw_text, ai_confidence=ai_confidence,
        needs_review=bool(ai_confidence is not None and ai_confidence < 0.6),
    )
    db.add(tx)
    db.commit()
    db.refresh(tx)
    if kind == "expense":
        _alerta_sobregasto(db, user, tx)
    return tx


def _alerta_sobregasto(db: Session, user: User, tx: Transaction) -> None:
    """Si este gasto hace CRUZAR un presupuesto del mes, avisa por WhatsApp.

    Solo alerta en el gasto que cruza el límite (no en cada uno por encima),
    así no llega spam. Best-effort: nunca rompe el registro.
    """
    try:
        if not user.phone:
            return
        from ..money import format_clp
        from . import whatsapp
        y, m = tx.occurred_at.year, tx.occurred_at.month
        chequeos: list[tuple[int, int, str]] = []
        bcat = db.scalars(select(Budget).where(
            Budget.household_id == user.household_id, Budget.category_id == tx.category_id)).first()
        if bcat:
            nuevo = total_categoria_mes(db, user.household_id, tx.category_id, y, m)
            chequeos.append((bcat.limit_amount, nuevo,
                             tx.category.name if tx.category else "esa categoría"))
        btot = db.scalars(select(Budget).where(
            Budget.household_id == user.household_id, Budget.category_id.is_(None))).first()
        if btot:
            inicio, fin = _rango_mes(y, m)
            chequeos.append((btot.limit_amount, _suma(db, user.household_id, "expense", inicio, fin),
                             "el total del mes"))
        for limite, nuevo, etiqueta in chequeos:
            anterior = nuevo - tx.amount
            if limite and anterior < limite <= nuevo:
                exceso = nuevo - limite
                whatsapp.enviar(user.phone,
                    f"🔔 *Alerta de presupuesto*\n"
                    f"Con este gasto pasaste el tope de *{etiqueta}*:\n"
                    f"Llevas {format_clp(nuevo)} de {format_clp(limite)} "
                    f"({format_clp(exceso)} sobre el límite) 😬")
    except Exception as e:  # noqa: BLE001
        from ..logger import get_logger
        get_logger("finanzas").warning("Alerta sobregasto: %s", e)


def set_sueldo(db: Session, user: User, amount: int) -> None:
    user.monthly_income = abs(int(amount))
    db.commit()


def crear_bill(db: Session, user: User, label: str, amount: int | None, due_date: _dt.date,
               notify_days_before: int = 3) -> Bill:
    b = Bill(household_id=user.household_id, created_by=user.id, label=label or "cuenta",
             amount=amount, due_date=due_date, notify_days_before=notify_days_before)
    db.add(b)
    db.commit()
    db.refresh(b)
    # Agenda el recordatorio en el Google Calendar del usuario (best-effort).
    try:
        from . import gcalendar
        if gcalendar.conectado(user):
            from ..money import format_clp
            monto = f" ({format_clp(amount)})" if amount else ""
            ev = gcalendar.crear_evento(db, user, f"💸 Pagar {b.label}{monto}", due_date,
                                        notify_days_before=notify_days_before)
            if ev:
                b.gcal_event_id = ev
                db.commit()
    except Exception as e:  # noqa: BLE001  (Calendar nunca debe romper el registro)
        from ..logger import get_logger
        get_logger("finanzas").warning("Calendar al crear bill: %s", e)
    return b


# ---------------------------------------------------------------------------
# Consultas / resumenes
# ---------------------------------------------------------------------------
def _rango_mes(year: int, month: int) -> tuple[_dt.date, _dt.date]:
    inicio = _dt.date(year, month, 1)
    fin = _dt.date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return inicio, fin


# Categorías de ingreso que NO se suman como "extra": el sueldo ya vive en
# users.monthly_income, así que contarlo también como transacción lo duplicaría.
_CATS_SUELDO = ["Sueldo"]


def _suma(db: Session, household_id: int, kind: str, inicio: _dt.date, fin: _dt.date,
          user_id: int | None = None, excluir_categorias: list[str] | None = None) -> int:
    q = select(func.coalesce(func.sum(Transaction.amount), 0)).where(
        Transaction.household_id == household_id, Transaction.kind == kind,
        Transaction.occurred_at >= inicio, Transaction.occurred_at < fin,
    )
    if user_id:
        q = q.where(Transaction.user_id == user_id)
    if excluir_categorias:
        sub = select(Category.id).where(Category.name.in_(excluir_categorias))
        q = q.where(or_(Transaction.category_id.is_(None), Transaction.category_id.notin_(sub)))
    return int(db.scalar(q) or 0)


def _sueldos(db: Session, household_id: int, fin: _dt.date, user_id: int | None = None) -> int:
    """Suma de sueldos mensuales de usuarios activos creados antes de `fin`.

    El filtro por `created_at` evita imputar el sueldo actual a meses en que la
    persona aún no estaba en el sistema (no inventa ingresos históricos).
    """
    q = select(func.coalesce(func.sum(User.monthly_income), 0)).where(
        User.household_id == household_id, User.is_active.is_(True),
        User.created_at < _dt.datetime(fin.year, fin.month, fin.day))
    if user_id:
        q = q.where(User.id == user_id)
    return int(db.scalar(q) or 0)


def resumen_mes(db: Session, household: Household, year: int, month: int,
                user_id: int | None = None) -> dict:
    """Resumen del mes. Con `user_id` filtra los números a esa persona."""
    inicio, fin = _rango_mes(year, month)

    sueldos = _sueldos(db, household.id, fin, user_id)
    ingresos_extra = _suma(db, household.id, "income", inicio, fin, user_id,
                           excluir_categorias=_CATS_SUELDO)
    gastos = _suma(db, household.id, "expense", inicio, fin, user_id)

    # Las cuentas por pagar son a nivel familia (no tienen dueño), así que al
    # filtrar por persona no se descuentan. Incluye pendientes vencidas de meses
    # anteriores (due_date < fin), para que el disponible no se infle con deuda
    # atrasada que la lista de "cuentas por pagar" sí muestra.
    if user_id:
        bills_pend = 0
    else:
        bills_pend = int(db.scalar(
            select(func.coalesce(func.sum(Bill.amount), 0)).where(
                Bill.household_id == household.id, Bill.status == "pending",
                Bill.due_date < fin)
        ) or 0)

    ingresos_total = sueldos + ingresos_extra
    disponible = ingresos_total - gastos - bills_pend

    # Gastos por categoria
    q_cat = (select(Category.name, Category.emoji, Category.is_ant, func.sum(Transaction.amount))
             .join(Category, Category.id == Transaction.category_id)
             .where(Transaction.household_id == household.id, Transaction.kind == "expense",
                    Transaction.occurred_at >= inicio, Transaction.occurred_at < fin))
    if user_id:
        q_cat = q_cat.where(Transaction.user_id == user_id)
    filas = db.execute(q_cat.group_by(Category.id).order_by(func.sum(Transaction.amount).desc())).all()
    por_categoria = [{"nombre": n, "emoji": e or "", "is_ant": bool(a), "total": int(s or 0)} for n, e, a, s in filas]
    hormigas = sum(c["total"] for c in por_categoria if c["is_ant"])

    # Gasto por persona
    filas_p = db.execute(
        select(User.name, func.sum(Transaction.amount))
        .join(User, User.id == Transaction.user_id)
        .where(Transaction.household_id == household.id, Transaction.kind == "expense",
               Transaction.occurred_at >= inicio, Transaction.occurred_at < fin)
        .group_by(User.id).order_by(func.sum(Transaction.amount).desc())
    ).all()
    por_persona = [{"nombre": n, "total": int(s or 0)} for n, s in filas_p]

    return {
        "year": year, "month": month, "mes_nombre": MESES[month].capitalize(),
        "ingresos": ingresos_total, "sueldos": sueldos, "ingresos_extra": ingresos_extra,
        "gastos": gastos, "bills_pendientes": bills_pend, "disponible": disponible,
        "por_categoria": por_categoria, "hormigas": hormigas, "por_persona": por_persona,
    }


def serie_meses(db: Session, household: Household, year: int, month: int, n: int = 6,
                user_id: int | None = None) -> list[dict]:
    """Ingresos vs gastos de los últimos `n` meses (terminando en year/month).

    El sueldo se imputa mes a mes solo desde que la persona existe (ver `_sueldos`),
    y el ingreso "extra" excluye la categoría Sueldo para no duplicarlo. Con
    `user_id` la serie se filtra a esa persona.
    """
    out: list[dict] = []
    y, m = year, month
    for _ in range(n):
        inicio, fin = _rango_mes(y, m)
        ingresos = _sueldos(db, household.id, fin, user_id) + _suma(
            db, household.id, "income", inicio, fin, user_id, excluir_categorias=_CATS_SUELDO)
        out.append({
            "year": y, "month": m, "label": f"{MESES[m][:3].capitalize()} {str(y)[2:]}",
            "ingresos": ingresos,
            "gastos": _suma(db, household.id, "expense", inicio, fin, user_id),
        })
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    out.reverse()
    return out


def gastos_diarios_acumulados(db: Session, household_id: int, year: int, month: int,
                              user_id: int | None = None) -> list[int]:
    """Gasto acumulado por día del mes: lista de largo = días del mes."""
    inicio, fin = _rango_mes(year, month)
    q = (select(Transaction.occurred_at, func.sum(Transaction.amount))
         .where(Transaction.household_id == household_id, Transaction.kind == "expense",
                Transaction.occurred_at >= inicio, Transaction.occurred_at < fin))
    if user_id:
        q = q.where(Transaction.user_id == user_id)
    por_dia = {f.day: int(s or 0) for f, s in db.execute(q.group_by(Transaction.occurred_at)).all()}
    dias = calendar.monthrange(year, month)[1]
    acumulado, out = 0, []
    for d in range(1, dias + 1):
        acumulado += por_dia.get(d, 0)
        out.append(acumulado)
    return out


# ---------------------------------------------------------------------------
# Presupuestos
# ---------------------------------------------------------------------------
def presupuestos_estado(db: Session, household: Household, year: int, month: int) -> list[dict]:
    """Presupuestos con su gasto acumulado del mes y % de uso."""
    inicio, fin = _rango_mes(year, month)
    out: list[dict] = []
    for b in db.scalars(select(Budget).where(Budget.household_id == household.id)):
        if b.category_id:
            cat = db.get(Category, b.category_id)
            gastado = int(db.scalar(
                select(func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.household_id == household.id, Transaction.kind == "expense",
                    Transaction.category_id == b.category_id,
                    Transaction.occurred_at >= inicio, Transaction.occurred_at < fin)
            ) or 0)
            nombre, emoji = (cat.name, cat.emoji or "") if cat else ("?", "")
        else:
            gastado = _suma(db, household.id, "expense", inicio, fin)
            nombre, emoji = "Total del mes", "🎯"
        pct = round(gastado * 100 / b.limit_amount) if b.limit_amount else 0
        out.append({"id": b.id, "categoria": nombre, "emoji": emoji,
                    "limite": b.limit_amount, "gastado": gastado, "pct": pct})
    out.sort(key=lambda x: -x["pct"])
    return out


def set_presupuesto(db: Session, household_id: int, categoria_nombre: str | None, limite: int) -> Budget:
    """Crea o actualiza el presupuesto de una categoría (None = límite total del mes)."""
    cat_id = None
    if categoria_nombre:
        cat = buscar_categoria(db, household_id, categoria_nombre, "expense")
        cat_id = cat.id
    b = db.scalars(select(Budget).where(Budget.household_id == household_id,
                                        Budget.category_id == cat_id)).first()
    if b:
        b.limit_amount = abs(int(limite))
    else:
        b = Budget(household_id=household_id, category_id=cat_id, limit_amount=abs(int(limite)))
        db.add(b)
    db.commit()
    db.refresh(b)
    return b


def transacciones_mes(db: Session, household: Household, year: int, month: int,
                      limite: int | None = None, user_id: int | None = None,
                      categoria_nombre: str | None = None):
    inicio, fin = _rango_mes(year, month)
    q = (select(Transaction).where(
            Transaction.household_id == household.id,
            Transaction.occurred_at >= inicio, Transaction.occurred_at < fin)
         .order_by(Transaction.occurred_at.desc(), Transaction.id.desc()))
    if user_id:
        q = q.where(Transaction.user_id == user_id)
    if categoria_nombre:
        cat = buscar_categoria(db, household.id, categoria_nombre, "expense")
        q = q.where(Transaction.category_id == cat.id)
    if limite:
        q = q.limit(limite)
    return list(db.scalars(q))


def eliminar_movimiento(db: Session, household_id: int, tx_id: int) -> Transaction | None:
    """Borra un movimiento de la familia. Devuelve el borrado o None si no existe."""
    tx = db.get(Transaction, tx_id)
    if not tx or tx.household_id != household_id:
        return None
    db.delete(tx)
    db.commit()
    return tx


def editar_movimiento(db: Session, household_id: int, tx_id: int, *, amount: int | None = None,
                      categoria_nombre: str | None = None, descripcion: str | None = None,
                      fecha: _dt.date | None = None, kind: str | None = None) -> Transaction | None:
    """Edita un movimiento de la familia. Solo cambia lo que se pasa. None si no existe."""
    tx = db.get(Transaction, tx_id)
    if not tx or tx.household_id != household_id:
        return None
    cambio_kind = bool(kind and kind in ("expense", "income") and kind != tx.kind)
    if kind in ("expense", "income"):
        tx.kind = kind
    if amount is not None and amount > 0:
        tx.amount = abs(int(amount))
    if categoria_nombre:
        tx.category_id = buscar_categoria(db, household_id, categoria_nombre, tx.kind).id
    elif cambio_kind:
        # Cambió gasto<->ingreso sin elegir categoría: re-resuelve para que calce el tipo.
        prev = tx.category.name if tx.category else None
        tx.category_id = buscar_categoria(db, household_id, prev, tx.kind).id
    if descripcion is not None:
        tx.description = descripcion.strip()[:255] or None
    if fecha is not None:
        tx.occurred_at = fecha
    db.commit()
    db.refresh(tx)
    return tx


def pagar_bill(db: Session, household_id: int, bill_id: int | None = None,
               label: str | None = None) -> Bill | None:
    """Marca una cuenta como pagada, por id o por nombre aproximado."""
    b = None
    if bill_id:
        b = db.get(Bill, bill_id)
        if b and b.household_id != household_id:
            b = None
    elif label:
        n = label.strip().lower()
        for cand in db.scalars(select(Bill).where(Bill.household_id == household_id,
                                                  Bill.status == "pending")):
            if n in cand.label.lower() or cand.label.lower() in n:
                b = cand
                break
    if b:
        # Quita el evento del Google Calendar de quien lo creó (best-effort).
        if b.gcal_event_id:
            try:
                from . import gcalendar
                creador = db.get(User, b.created_by)
                if creador:
                    gcalendar.borrar_evento(db, creador, b.gcal_event_id)
            except Exception:  # noqa: BLE001
                pass
        b.status = "paid"
        b.paid_at = utcnow()
        db.commit()
    return b


def bills_pendientes(db: Session, household: Household) -> list[Bill]:
    return list(db.scalars(
        select(Bill).where(Bill.household_id == household.id, Bill.status == "pending")
        .order_by(Bill.due_date)
    ))


def categorias(db: Session, household_id: int, kind: str | None = None) -> list[Category]:
    cond = or_(Category.household_id == household_id, Category.household_id.is_(None))
    q = select(Category).where(cond)
    if kind:
        q = q.where(Category.kind == kind)
    return list(db.scalars(q.order_by(Category.kind, Category.name)))


def total_categoria_mes(db: Session, household_id: int, category_id: int, year: int, month: int) -> int:
    inicio, fin = _rango_mes(year, month)
    val = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.household_id == household_id, Transaction.category_id == category_id,
            Transaction.occurred_at >= inicio, Transaction.occurred_at < fin)
    )
    return int(val or 0)
