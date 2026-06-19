"""Normalizacion de plata chilena: entiende 'lucas', 'palos', 'k', 'mil' y
formatea montos en CLP (separador de miles con punto)."""
from __future__ import annotations

import re


def parse_amount(text: str) -> int | None:
    """Extrae un monto en pesos desde texto coloquial chileno.

    Ejemplos:
        '15 lucas'  -> 15000      '2 palos'    -> 2000000
        '15k'       -> 15000      '1.5 palos'  -> 1500000
        '15 mil'    -> 15000      '$15.000'    -> 15000
        '20000'     -> 20000      '1.500'      -> 1500
    """
    if not text:
        return None
    t = text.lower()

    # 'lucas' / 'k'  (mil)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(lucas?|lukas?|k)(?![a-z])", t)
    if m:
        return _mult(m.group(1), 1000)

    # 'palos' / 'millones'  (millon)
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(palos?|millon(?:es)?|millón)", t)
    if m:
        return _mult(m.group(1), 1_000_000)

    # 'X mil'
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*mil\b", t)
    if m:
        return _mult(m.group(1), 1000)

    # Numero plano, con o sin separador de miles: '$15.000', '15.000', '20000'
    best: int | None = None
    for mm in re.finditer(r"(\d{1,3}(?:\.\d{3})+|\d+)", t):
        n = int(mm.group(1).replace(".", ""))
        if best is None or n > best:
            best = n
    return best


def _mult(num_str: str, factor: int) -> int:
    """Multiplica un numero (que puede traer coma decimal) por un factor."""
    # En CLP el '.' suele ser separador de miles y la ',' decimal.
    val = float(num_str.replace(".", "").replace(",", ".")) if "," in num_str else float(num_str)
    return int(round(val * factor))


def format_clp(amount: int | float | None) -> str:
    """15000 -> '$15.000'."""
    if amount is None:
        return "$0"
    return "$" + f"{int(round(amount)):,}".replace(",", ".")
