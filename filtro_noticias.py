"""
Filtro de noticias de alto impacto — calendario hardcodeado.
Bloquea el dia entero si el evento esta en el calendario.

VENTANA DE ENTRADA ACTUAL: 9:30am - 3:00pm ET (ORB_VENTANA_H=15 en config.py).
=> FOMC (anuncio 2pm ET) SI cae dentro de la ventana — deberia agregarse al
   calendario abajo para evitar entradas justo antes/durante el anuncio.
   TODO: agregar fechas FOMC 2025-2026 (federalreserve.gov/monetarypolicy/fomccalendars.htm).

Si el calendario expira (fecha > _ULTIMA_FECHA), por defecto se bloquea el dia
para evitar operar sin filtro de noticias.

Fuentes: bls.gov/schedule (NFP/CPI/PPI) y federalreserve.gov (FOMC).
"""

from datetime import date, timedelta


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


_ULTIMA_FECHA = max(_CALENDARIO.keys())


def check_noticia(fecha: date) -> tuple:
    if fecha > _ULTIMA_FECHA:
        # Fail-safe: si el calendario expiro, bloqueamos por defecto para no operar
        # sin filtro de noticias. Hay que actualizar _CALENDARIO con fechas nuevas.
        print('[FILTRO_NOTICIAS] ALERTA: calendario expiro el %s — bloqueando entradas por seguridad.' % _ULTIMA_FECHA)
        return True, 'CALENDARIO_EXPIRADO'
    if fecha > _ULTIMA_FECHA - timedelta(days=30):
        print('[FILTRO_NOTICIAS] WARNING: calendario expira el %s — actualizar pronto.' % _ULTIMA_FECHA)
    nombre = _CALENDARIO.get(fecha)
    if nombre:
        return True, nombre
    return False, None
