"""
Backtest 8 meses (Sep 13, 2025 – May 9, 2026) — filtro horario de noticias.
Solo se saltea el día si el evento cae dentro de la ventana de ejecución ORB (8am-1:30pm ET).
Eventos a las 2pm (FOMC) quedan fuera → NO se filtran.
"""

import yfinance as yf
import numpy as np
from datetime import date
from zoneinfo import ZoneInfo
from config import PLANES, CUENTA

from estrategias import signal_orb, signal_ict

ET = ZoneInfo("America/New_York")

# ── Calendario de eventos de alto impacto con horario ET ─────────────────
# Formato: (fecha, hora_ET, minuto_ET, nombre)
EVENTOS = [
    # NFP — primer viernes de cada mes, 8:30am ET
    (date(2025,  9,  5),  8, 30, 'NFP'),
    (date(2025, 10,  3),  8, 30, 'NFP'),
    (date(2025, 11,  7),  8, 30, 'NFP'),
    (date(2025, 12,  5),  8, 30, 'NFP'),
    (date(2026,  1,  9),  8, 30, 'NFP'),
    (date(2026,  2,  6),  8, 30, 'NFP'),
    (date(2026,  3,  6),  8, 30, 'NFP'),
    (date(2026,  4,  3),  8, 30, 'NFP'),
    (date(2026,  5,  1),  8, 30, 'NFP'),
    # CPI — 8:30am ET
    (date(2025,  9, 10),  8, 30, 'CPI'),
    (date(2025, 10, 15),  8, 30, 'CPI'),
    (date(2025, 11, 12),  8, 30, 'CPI'),
    (date(2025, 12, 10),  8, 30, 'CPI'),
    (date(2026,  1, 15),  8, 30, 'CPI'),
    (date(2026,  2, 12),  8, 30, 'CPI'),
    (date(2026,  3, 12),  8, 30, 'CPI'),
    (date(2026,  4, 10),  8, 30, 'CPI'),
    # PPI — 8:30am ET (día siguiente al CPI)
    (date(2025,  9, 11),  8, 30, 'PPI'),
    (date(2025, 10, 16),  8, 30, 'PPI'),
    (date(2025, 11, 13),  8, 30, 'PPI'),
    (date(2025, 12, 11),  8, 30, 'PPI'),
    (date(2026,  1, 16),  8, 30, 'PPI'),
    (date(2026,  2, 13),  8, 30, 'PPI'),
    (date(2026,  3, 13),  8, 30, 'PPI'),
    (date(2026,  4, 11),  8, 30, 'PPI'),
    # FOMC decisión — 2:00pm ET (DESPUÉS del cierre de ventana ORB 1:30pm → NO se filtra)
    (date(2025,  9, 17), 14,  0, 'FOMC'),
    (date(2025, 10, 29), 14,  0, 'FOMC'),
    (date(2025, 12, 10), 14,  0, 'FOMC'),
    (date(2026,  1, 28), 14,  0, 'FOMC'),
    (date(2026,  3, 19), 14,  0, 'FOMC'),
    (date(2026,  5,  7), 14,  0, 'FOMC'),
]

# Ventana de filtro: 8:00am – 1:30pm ET
# Eventos dentro de este rango → saltear el día
FILTRO_INICIO_MIN = 8 * 60        # 480 min = 8:00am
FILTRO_FIN_MIN    = 13 * 60 + 30  # 810 min = 1:30pm


def check_noticia(fecha: date):
    """Devuelve (True, nombre_evento) si hay evento dentro de la ventana ORB, sino (False, None)."""
    for ev_fecha, ev_h, ev_m, ev_nombre in EVENTOS:
        if ev_fecha == fecha:
            ev_mins = ev_h * 60 + ev_m
            if FILTRO_INICIO_MIN <= ev_mins <= FILTRO_FIN_MIN:
                return True, ev_nombre
    return False, None


def correr_backtest(df, fecha_inicio, fecha_fin, usar_filtro, nombre_run):
    capital   = float(PLANES[CUENTA]['capital_inicial'])
    target    = PLANES[CUENTA]['profit_target']
    drawdown  = PLANES[CUENTA]['max_drawdown']
    capital_0 = capital

    st_orb = {'orb_sizes': [], 'trades': 0, 'take_profit': 0, 'stop_loss': 0, 'timeout': 0, 'skip': 0}
    st_ict = {'obs_usados': set(), 'trades': 0, 'take_profit': 0, 'stop_loss': 0, 'timeout': 0, 'skip': 0}

    fechas_dias = sorted({
        ts.astimezone(ET).date()
        for ts in df.index
        if fecha_inicio <= ts.astimezone(ET).date() <= fecha_fin
        and ts.astimezone(ET).weekday() < 5
    })

    log_orb = []
    log_ict = []

    # ── ORB ─────────────────────────────────────────────────────────────
    capital_orb = float(PLANES[CUENTA]['capital_inicial'])
    for hoy in fechas_dias:
        df_corte = df[[ts.astimezone(ET).date() <= hoy for ts in df.index]]
        if len(df_corte) < 50:
            continue

        hay_noticia, nombre_ev = check_noticia(hoy) if usar_filtro else (False, None)
        if hay_noticia:
            st_orb['skip'] += 1
            log_orb.append((hoy, 'SKIP', '-', '-', '-', 0, '$0', 'noticia: %s 8:30am' % nombre_ev))
            continue

        # ¿FOMC hoy? (2pm ET, fuera de ventana) → marcar para contexto pero no saltear
        es_fomc = any(ev[0] == hoy and ev[3] == 'FOMC' for ev in EVENTOS)
        fomc_tag = ' [FOMC 2pm]' if (usar_filtro and es_fomc) else ''

        trade, sizes, motivo = signal_orb(df_corte, hoy, capital_orb, st_orb['orb_sizes'])
        st_orb['orb_sizes'] = sizes
        if trade:
            st_orb['trades'] += 1
            st_orb[trade['resultado']] += 1
            capital_orb += trade['ganancia']
            log_orb.append((
                hoy,
                trade['direccion'] + fomc_tag,
                trade['entrada'],
                trade['sl'],
                trade['tp'],
                trade['contratos'],
                '$%+.0f' % trade['ganancia'],
                trade['resultado'],
            ))
        else:
            log_orb.append((hoy, 'Sin señal', '-', '-', '-', 0, '$0', (motivo or '') + fomc_tag))

        if capital_orb - capital_0 <= -drawdown:
            log_orb.append((hoy, '***', '-', '-', '-', 0, '$0', 'DRAWDOWN MAX — funded perdida'))
            break
        if capital_orb - capital_0 >= target:
            log_orb.append((hoy, '***', '-', '-', '-', 0, '$0', 'PROFIT TARGET — funded pasada!'))
            break

    # ── ICT ─────────────────────────────────────────────────────────────
    capital_ict = float(PLANES[CUENTA]['capital_inicial'])
    for hoy in fechas_dias:
        df_corte = df[[ts.astimezone(ET).date() <= hoy for ts in df.index]]
        if len(df_corte) < 50:
            continue

        hay_noticia, nombre_ev = check_noticia(hoy) if usar_filtro else (False, None)
        if hay_noticia:
            st_ict['skip'] += 1
            log_ict.append((hoy, 'SKIP', '-', '-', '-', 0, '$0', 'noticia: %s 8:30am' % nombre_ev))
            continue

        es_fomc = any(ev[0] == hoy and ev[3] == 'FOMC' for ev in EVENTOS)
        fomc_tag = ' [FOMC 2pm]' if (usar_filtro and es_fomc) else ''

        trade, obs, motivo = signal_ict(df_corte, hoy, capital_ict, st_ict['obs_usados'])
        st_ict['obs_usados'] = obs
        if trade:
            st_ict['trades'] += 1
            st_ict[trade['resultado']] += 1
            capital_ict += trade['ganancia']
            log_ict.append((
                hoy,
                trade['direccion'] + fomc_tag,
                trade['entrada'],
                trade['sl'],
                trade['tp'],
                trade['contratos'],
                '$%+.0f' % trade['ganancia'],
                trade['resultado'],
            ))
        else:
            log_ict.append((hoy, 'Sin señal', '-', '-', '-', 0, '$0', (motivo or '') + fomc_tag))

        if capital_ict - capital_0 <= -drawdown:
            log_ict.append((hoy, '***', '-', '-', '-', 0, '$0', 'DRAWDOWN MAX — funded perdida'))
            break

    # ── Imprimir ─────────────────────────────────────────────────────────
    filtro_label = 'CON FILTRO HORARIO (NFP/CPI/PPI 8:30am — FOMC 2pm NO filtrado)' if usar_filtro else 'SIN FILTRO'
    print('\n' + '=' * 78)
    print('  %s  —  %s' % (nombre_run, filtro_label))
    print('=' * 78)

    col = '  %-12s  %-22s  %-8s  %-8s  %-8s  %4s  %8s  %s'
    sep = '  ' + '-' * 76
    hdr = col % ('Dia', 'Dir', 'Entrada', 'SL', 'TP', 'Ctrs', 'P&L', 'Resultado')

    print('\n  ORB — detalle:')
    print(hdr); print(sep)
    for r in log_orb:
        print(col % r)

    orb_pnl = capital_orb - capital_0
    print('\n  ORB → Trades: %d | TP: %d | SL: %d | Timeout: %d | Skip noticias: %d | P&L: $%+.0f' % (
        st_orb['trades'], st_orb['take_profit'], st_orb['stop_loss'],
        st_orb['timeout'], st_orb['skip'], orb_pnl))

    print('\n  ICT — detalle:')
    print(hdr); print(sep)
    for r in log_ict:
        print(col % r)

    ict_pnl = capital_ict - capital_0
    print('\n  ICT → Trades: %d | TP: %d | SL: %d | Timeout: %d | Skip noticias: %d | P&L: $%+.0f' % (
        st_ict['trades'], st_ict['take_profit'], st_ict['stop_loss'],
        st_ict['timeout'], st_ict['skip'], ict_pnl))

    print('=' * 78)
    return orb_pnl, ict_pnl


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print('Descargando datos MES=F (1h, desde jul 2025 para warmup)...')
    import pandas as pd
    df = yf.download('MES=F', start='2025-07-01', end='2026-05-14',
                     interval='1h', progress=False, auto_adjust=True)
    if df.empty:
        raise SystemExit('Error: no se pudo descargar MES=F')

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    df.index = (df.index.tz_convert(ET) if df.index.tzinfo
                else df.index.tz_localize('UTC').tz_convert(ET))

    inicio = date(2025, 9, 13)
    fin    = date(2026, 5,  9)

    orb_sin, ict_sin = correr_backtest(df, inicio, fin,
                                       usar_filtro=False,
                                       nombre_run='Sep 13, 2025 – May 9, 2026')

    orb_con, ict_con = correr_backtest(df, inicio, fin,
                                       usar_filtro=True,
                                       nombre_run='Sep 13, 2025 – May 9, 2026')

    # ── Resumen final ─────────────────────────────────────────────────────
    dias_filtrados = sorted(
        ev[0] for ev in EVENTOS
        if inicio <= ev[0] <= fin
        and FILTRO_INICIO_MIN <= ev[1] * 60 + ev[2] <= FILTRO_FIN_MIN
    )
    fomc_no_filtrado = sorted(
        ev[0] for ev in EVENTOS
        if inicio <= ev[0] <= fin and ev[3] == 'FOMC'
    )

    print('\n' + '=' * 78)
    print('  RESUMEN COMPARATIVO — 8 MESES')
    print('  ' + '-' * 76)
    print('  %-35s  %18s  %18s' % ('', 'SIN FILTRO', 'CON FILTRO'))
    print('  %-35s  %18s  %18s' % ('ORB P&L', '$%+.0f' % orb_sin, '$%+.0f' % orb_con))
    print('  %-35s  %18s  %18s' % ('ICT P&L', '$%+.0f' % ict_sin, '$%+.0f' % ict_con))
    print()
    print('  Días FILTRADOS (evento 8:30am ET dentro de ventana ORB):')
    for d in dias_filtrados:
        nombres = [ev[3] for ev in EVENTOS if ev[0] == d and FILTRO_INICIO_MIN <= ev[1]*60+ev[2] <= FILTRO_FIN_MIN]
        print('    ✗  %s — %s' % (d, ', '.join(nombres)))
    print()
    print('  Días con FOMC (2pm ET) — NO filtrados, bot puede operar:')
    for d in fomc_no_filtrado:
        print('    ✓  %s — FOMC 2pm (fuera de ventana ORB)' % d)
    print('=' * 78)
