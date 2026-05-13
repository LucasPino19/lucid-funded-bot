"""
Filtro de noticias de alto impacto.
Consulta Finnhub API automaticamente. Si falla, usa calendario de respaldo.
Solo bloquea si el evento cae dentro de la ventana ORB (8am-1:30pm ET).
FOMC (2pm ET) no se filtra — el bot ya cerro su ventana a la 1:30pm.
"""

import os
import requests
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

FINNHUB_TOKEN = os.environ.get('FINNHUB_TOKEN', '')

# Palabras clave de eventos que nos importan
_KEYWORDS = ('nonfarm', 'payroll', 'cpi', 'consumer price', 'ppi', 'producer price')

# Ventana ORB: 8:00am – 1:30pm ET
_FILTRO_INICIO = 8 * 60        # 480 min
_FILTRO_FIN    = 13 * 60 + 30  # 810 min


def check_noticia(fecha: date) -> tuple:
    """
    Devuelve (True, 'NFP') si hay evento de alto impacto dentro de la ventana ORB.
    Intenta Finnhub primero, cae al calendario hardcodeado si falla.
    """
    if FINNHUB_TOKEN:
        try:
            return _check_finnhub(fecha)
        except Exception as e:
            print('Finnhub error: %s — usando calendario de respaldo' % e)
    return _check_hardcoded(fecha)


def _check_finnhub(fecha: date) -> tuple:
    fecha_str = fecha.strftime('%Y-%m-%d')
    resp = requests.get(
        'https://finnhub.io/api/v1/calendar/economic',
        params={'from': fecha_str, 'to': fecha_str, 'token': FINNHUB_TOKEN},
        timeout=5,
    )
    resp.raise_for_status()

    for ev in resp.json().get('economicCalendar', []):
        if ev.get('country', '').upper() != 'US':
            continue
        if ev.get('impact', '').lower() != 'high':
            continue

        nombre = ev.get('event', '')
        if not any(k in nombre.lower() for k in _KEYWORDS):
            continue

        tiempo = ev.get('time', '')
        if not tiempo:
            continue

        # Finnhub devuelve la hora en UTC — convertir a ET
        ev_utc = datetime.strptime(tiempo, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        ev_et  = ev_utc.astimezone(ET)
        ev_min = ev_et.hour * 60 + ev_et.minute

        if _FILTRO_INICIO <= ev_min <= _FILTRO_FIN:
            return True, nombre

    return False, None


# ── Calendario de respaldo (por si Finnhub falla) ────────────────────────────
_CALENDARIO = {
    # NFP
    date(2026,  5,  1), date(2026,  6,  5), date(2026,  7,  2),
    date(2026,  8,  7), date(2026,  9,  4), date(2026, 10,  2),
    date(2026, 11,  6), date(2026, 12,  4),
    # CPI
    date(2026,  5, 13), date(2026,  6, 10), date(2026,  7, 15),
    date(2026,  8, 12), date(2026,  9,  9), date(2026, 10, 14),
    date(2026, 11, 12), date(2026, 12,  9),
    # PPI
    date(2026,  5, 14), date(2026,  6, 11), date(2026,  7, 16),
    date(2026,  8, 13), date(2026,  9, 10), date(2026, 10, 15),
    date(2026, 11, 13), date(2026, 12, 10),
}


def _check_hardcoded(fecha: date) -> tuple:
    if fecha in _CALENDARIO:
        return True, 'evento alto impacto (respaldo)'
    return False, None
