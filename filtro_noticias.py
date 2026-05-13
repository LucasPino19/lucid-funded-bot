"""
Filtro de noticias de alto impacto — solo bloquea si el evento cae
dentro de la ventana de ejecución ORB (8:00am–1:30pm ET).
FOMC (2:00pm ET) no se filtra — el bot ya cerró su ventana a la 1:30pm.
"""

from datetime import date

# ── Calendario de eventos de alto impacto con horario ET ─────────────────
# Formato: (fecha, hora_ET, minuto_ET, nombre)
EVENTOS = [
    # NFP — primer viernes del mes, 8:30am ET
    (date(2025,  9,  5),  8, 30, 'NFP'),
    (date(2025, 10,  3),  8, 30, 'NFP'),
    (date(2025, 11,  7),  8, 30, 'NFP'),
    (date(2025, 12,  5),  8, 30, 'NFP'),
    (date(2026,  1,  9),  8, 30, 'NFP'),
    (date(2026,  2,  6),  8, 30, 'NFP'),
    (date(2026,  3,  6),  8, 30, 'NFP'),
    (date(2026,  4,  3),  8, 30, 'NFP'),
    (date(2026,  5,  1),  8, 30, 'NFP'),
    (date(2026,  6,  5),  8, 30, 'NFP'),
    (date(2026,  7,  2),  8, 30, 'NFP'),
    (date(2026,  8,  7),  8, 30, 'NFP'),
    (date(2026,  9,  4),  8, 30, 'NFP'),
    (date(2026, 10,  2),  8, 30, 'NFP'),
    (date(2026, 11,  6),  8, 30, 'NFP'),
    (date(2026, 12,  4),  8, 30, 'NFP'),
    # CPI — 8:30am ET
    (date(2025,  9, 10),  8, 30, 'CPI'),
    (date(2025, 10, 15),  8, 30, 'CPI'),
    (date(2025, 11, 12),  8, 30, 'CPI'),
    (date(2025, 12, 10),  8, 30, 'CPI'),
    (date(2026,  1, 15),  8, 30, 'CPI'),
    (date(2026,  2, 12),  8, 30, 'CPI'),
    (date(2026,  3, 12),  8, 30, 'CPI'),
    (date(2026,  4, 10),  8, 30, 'CPI'),
    (date(2026,  5, 13),  8, 30, 'CPI'),
    (date(2026,  6, 10),  8, 30, 'CPI'),
    (date(2026,  7, 15),  8, 30, 'CPI'),
    (date(2026,  8, 12),  8, 30, 'CPI'),
    (date(2026,  9,  9),  8, 30, 'CPI'),
    (date(2026, 10, 14),  8, 30, 'CPI'),
    (date(2026, 11, 12),  8, 30, 'CPI'),
    (date(2026, 12,  9),  8, 30, 'CPI'),
    # PPI — 8:30am ET (día siguiente al CPI)
    (date(2025,  9, 11),  8, 30, 'PPI'),
    (date(2025, 10, 16),  8, 30, 'PPI'),
    (date(2025, 11, 13),  8, 30, 'PPI'),
    (date(2025, 12, 11),  8, 30, 'PPI'),
    (date(2026,  1, 16),  8, 30, 'PPI'),
    (date(2026,  2, 13),  8, 30, 'PPI'),
    (date(2026,  3, 13),  8, 30, 'PPI'),
    (date(2026,  4, 11),  8, 30, 'PPI'),
    (date(2026,  5, 14),  8, 30, 'PPI'),
    (date(2026,  6, 11),  8, 30, 'PPI'),
    (date(2026,  7, 16),  8, 30, 'PPI'),
    (date(2026,  8, 13),  8, 30, 'PPI'),
    (date(2026,  9, 10),  8, 30, 'PPI'),
    (date(2026, 10, 15),  8, 30, 'PPI'),
    (date(2026, 11, 13),  8, 30, 'PPI'),
    (date(2026, 12, 10),  8, 30, 'PPI'),
    # FOMC decisión — 2:00pm ET → fuera de ventana ORB, NO se filtra
    (date(2025,  9, 17), 14,  0, 'FOMC'),
    (date(2025, 10, 29), 14,  0, 'FOMC'),
    (date(2025, 12, 10), 14,  0, 'FOMC'),
    (date(2026,  1, 28), 14,  0, 'FOMC'),
    (date(2026,  3, 19), 14,  0, 'FOMC'),
    (date(2026,  5,  7), 14,  0, 'FOMC'),
    (date(2026,  6, 17), 14,  0, 'FOMC'),
    (date(2026,  7, 29), 14,  0, 'FOMC'),
    (date(2026,  9, 16), 14,  0, 'FOMC'),
    (date(2026, 10, 28), 14,  0, 'FOMC'),
    (date(2026, 12,  9), 14,  0, 'FOMC'),
]

# Ventana ORB: 8:00am – 1:30pm ET
# Eventos dentro de este rango → saltear el día
_FILTRO_INICIO = 8 * 60        # 480 min
_FILTRO_FIN    = 13 * 60 + 30  # 810 min


def check_noticia(fecha: date) -> tuple:
    """
    Devuelve (True, 'NFP') si hay evento de alto impacto dentro de la ventana
    de ejecucion ORB (8am-1:30pm ET). FOMC a las 2pm devuelve (False, None).
    """
    for ev_fecha, ev_h, ev_m, ev_nombre in EVENTOS:
        if ev_fecha == fecha:
            ev_mins = ev_h * 60 + ev_m
            if _FILTRO_INICIO <= ev_mins <= _FILTRO_FIN:
                return True, ev_nombre
    return False, None
