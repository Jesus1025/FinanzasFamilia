"""Datos por defecto: categorias iniciales y palabras clave para clasificar
gastos sin IA (fallback heuristico)."""
from __future__ import annotations

# (nombre, kind, is_ant, emoji)
DEFAULT_CATEGORIES = [
    ("Supermercado", "expense", False, "🛒"),
    ("Comida/Restaurant", "expense", False, "🍔"),
    ("Delivery", "expense", True, "🛵"),
    ("Café", "expense", True, "☕"),
    ("Transporte", "expense", False, "🚌"),
    ("Bencina", "expense", False, "⛽"),
    ("Arriendo", "expense", False, "🏠"),
    ("Cuentas/Servicios", "expense", False, "💡"),
    ("Salud", "expense", False, "💊"),
    ("Entretención", "expense", False, "🎬"),
    ("Compras", "expense", False, "🛍️"),
    ("Suscripciones", "expense", True, "📺"),
    ("Mascotas", "expense", False, "🐾"),
    ("Educación", "expense", False, "📚"),
    ("Otros", "expense", False, "📦"),
    ("Sueldo", "income", False, "💰"),
    ("Bono/Extra", "income", False, "🎁"),
    ("Otros ingresos", "income", False, "➕"),
]

# El primer match gana. Solo se usa cuando no hay IA (o como respaldo).
CATEGORY_KEYWORDS = {
    "Bencina": ["bencina", "combustible", "copec", "shell", "petrobras", "gasolina", "petroleo", "petróleo"],
    "Transporte": ["uber", "taxi", "micro", "metro", "bip", "pasaje", "didi", "cabify", "bus",
                   "locomocion", "locomoción", "estacionamiento", "peaje", "tag"],
    "Delivery": ["delivery", "pedidosya", "pedidos ya", "rappi", "uber eats", "ubereats", "justo"],
    "Café": ["café", "cafe", "starbucks", "juan valdez", "cafetería", "cafeteria"],
    "Supermercado": ["super", "supermercado", "jumbo", "lider", "líder", "tottus", "unimarc",
                     "santa isabel", "feria", "verduras", "almacen", "almacén"],
    "Comida/Restaurant": ["almuerzo", "once", "comida", "restaurant", "restorán", "restoran", "cena",
                          "completos", "sushi", "pizza", "mcdonald", "burger", "empanada"],
    "Arriendo": ["arriendo", "renta", "dividendo"],
    "Cuentas/Servicios": ["luz", "agua", "gas", "internet", "electricidad", "enel", "aguas andinas",
                          "gtd", "wifi", "cuenta de luz", "cuentas"],
    "Salud": ["farmacia", "remedio", "médico", "medico", "doctor", "isapre", "clínica", "clinica",
              "dentista", "salud", "ahumada", "cruz verde", "salcobrand"],
    "Suscripciones": ["suscripción", "suscripcion", "mensualidad", "spotify", "disney", "prime", "hbo", "max"],
    "Entretención": ["cine", "netflix", "juego", "concierto", "entrada", "panorama", "carrete"],
    "Compras": ["ropa", "zapatos", "falabella", "ripley", "paris", "compré", "compras", "amazon",
                "aliexpress", "mercadolibre", "mercado libre"],
    "Mascotas": ["perro", "gato", "mascota", "veterinario", "vet "],
    "Educación": ["curso", "colegio", "universidad", "matrícula", "matricula", "libro", "útiles", "utiles"],
}


def detectar_categoria(texto: str, kind: str = "expense") -> str:
    """Devuelve el nombre de categoria mas probable segun palabras clave."""
    if kind == "income":
        t = texto.lower()
        if any(k in t for k in ["sueldo", "salario", "remuneración", "remuneracion"]):
            return "Sueldo"
        if any(k in t for k in ["bono", "aguinaldo", "extra", "propina"]):
            return "Bono/Extra"
        return "Otros ingresos"
    t = texto.lower()
    for nombre, claves in CATEGORY_KEYWORDS.items():
        if any(k in t for k in claves):
            return nombre
    return "Otros"
