"""
Filtro de noticias de alto impacto — calendario hardcodeado.
Solo bloquea si el evento cae dentro de la ventana ORB (8am-1:30pm ET).
FOMC (2pm ET) no se filtra — el bot ya cerro su ventana a la 1:30pm.

Para actualizar: mandame las fechas del nuevo año y lo actualizo en minutos.
Fuentes: bls.gov/schedule (NFP/CPI/PPI) y federalreserve.gov (FOMC).
"""

from datetime import date


_CALENDARIO = {
    # NFP — primer viernes del mes, 8:30am ET
    date(2025,  9,  5): 'NFP', date(2025, 10,  3): 'NFP',
    date(2025, 11,  7): 'NFP', date(2025, 12,  5): 'NFP',
    date(2026,  1,  9): 'NFP', date(2026,  2,  6): 'NFP',
    date(2026,  3,  6): 'NFP', date(2026,  4,  3): 'NFP',
    date(2026,  5,  1): 'NFP', date(2026,  6,  5): 'NFP',
    date(2026,  7,  2): 'NFP', date(2026,  8,  7): 'NFP',
    date(2026,  9,  4): 'NFP', date(2026, 10,  2): 'NFP',
    date(2026, 11,  6): 'NFP', date(2026, 12,  4): 'NFP',
    # CPI — 8:30am ET
    date(2025,  9, 10): 'CPI', date(2025, 10, 15): 'CPI',
    date(2025, 11, 12): 'CPI', date(2025, 12, 10): 'CPI',
    date(2026,  1, 15): 'CPI', date(2026,  2, 12): 'CPI',
    date(2026,  3, 12): 'CPI', date(2026,  4, 10): 'CPI',
    date(2026,  5, 13): 'CPI', date(2026,  6, 10): 'CPI',
    date(2026,  7, 15): 'CPI', date(2026,  8, 12): 'CPI',
    date(2026,  9,  9): 'CPI', date(2026, 10, 14): 'CPI',
    date(2026, 11, 12): 'CPI', date(2026, 12,  9): 'CPI',
    # PPI — 8:30am ET
    date(2025,  9, 11): 'PPI', date(2025, 10, 16): 'PPI',
    date(2025, 11, 13): 'PPI', date(2025, 12, 11): 'PPI',
    date(2026,  1, 16): 'PPI', date(2026,  2, 13): 'PPI',
    date(2026,  3, 13): 'PPI', date(2026,  4, 11): 'PPI',
    date(2026,  5, 14): 'PPI', date(2026,  6, 11): 'PPI',
    date(2026,  7, 16): 'PPI', date(2026,  8, 13): 'PPI',
    date(2026,  9, 10): 'PPI', date(2026, 10, 15): 'PPI',
    date(2026, 11, 13): 'PPI', date(2026, 12, 10): 'PPI',
}


def check_noticia(fecha: date) -> tuple:
    """
    Devuelve (True, 'NFP') si hay evento de alto impacto dentro de la ventana ORB.
    FOMC a las 2pm devuelve (False, None) — fuera de la ventana de ejecucion.
    """
    nombre = _CALENDARIO.get(fecha)
    if nombre:
        return True, nombre
    return False, None
