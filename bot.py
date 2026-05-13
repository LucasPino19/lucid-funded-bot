"""
LucidFlex Funded Bot — Motor Principal
=======================================
Corre una vez por día después del cierre del mercado (vía GitHub Actions).
Maneja dos cuentas independientes: una ORB y una ICT.
Aplica todas las reglas reales de LucidFlex.
"""

import json
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import yfinance as yf

from config import (CUENTA, PLANES, TICKER, ESTADO_FILE, REPORTS_DIR,
                    MAX_CONSEC_PERDIDAS)
from estrategias import signal_orb, signal_ict

ET = ZoneInfo("America/New_York")
PLAN = PLANES[CUENTA]


# ══════════════════════════════════════════════
# ESTADO — carga y guardado
# ══════════════════════════════════════════════

def estado_inicial(estrategia):
    return {
        'estrategia':       estrategia,
        'cuenta':           CUENTA,
        'capital_inicial':  PLAN['capital_inicial'],
        'capital':          PLAN['capital_inicial'],
        'ganancia_total':   0.0,
        'ganancia_por_dia': {},
        'trades':           [],
        'estado':           'activa',   # activa | pasada | explotada
        'razon_fin':        '',
        'consecutivas_hoy': 0,
        'ultimo_dia':       '',
        # ORB específico
        'orb_sizes':        [],
        # ICT específico
        'obs_usados':       [],
    }


def cargar_estado():
    os.makedirs(os.path.dirname(ESTADO_FILE), exist_ok=True)
    if os.path.exists(ESTADO_FILE):
        with open(ESTADO_FILE) as f:
            return json.load(f)
    return {
        'ORB': estado_inicial('ORB'),
        'ICT': estado_inicial('ICT'),
    }


def guardar_estado(estado):
    os.makedirs(os.path.dirname(ESTADO_FILE), exist_ok=True)
    with open(ESTADO_FILE, 'w') as f:
        json.dump(estado, f, indent=2, default=str)


# ══════════════════════════════════════════════
# REGLAS LUCIDFLEX
# ══════════════════════════════════════════════

def aplicar_reglas(cuenta_estado, dia_str):
    capital        = cuenta_estado['capital']
    ganancia_total = cuenta_estado['ganancia_total']
    gan_por_dia    = cuenta_estado['ganancia_por_dia']
    trades         = cuenta_estado['trades']

    # 1. Drawdown máximo
    if capital <= PLAN['capital_inicial'] - PLAN['max_drawdown']:
        return 'explotada', 'Drawdown maximo alcanzado — capital: $%.0f' % capital

    # 2. Profit target + consistencia 50%
    if ganancia_total >= PLAN['profit_target']:
        max_dia = max(gan_por_dia.values()) if gan_por_dia else 0
        if ganancia_total > 0 and max_dia / ganancia_total <= 0.50:
            return 'pasada', 'Target alcanzado: $%+.0f en %d trades' % (ganancia_total, len(trades))
        # Viola consistencia → sigue operando
        return 'activa', 'Target alcanzado pero consistencia viola 50%% — dia max: $%.0f / total: $%.0f' % (max_dia, ganancia_total)

    return 'activa', ''


# ══════════════════════════════════════════════
# DESCARGA DE DATOS
# ══════════════════════════════════════════════

def descargar_datos(periodo='90d'):
    print('Descargando %s (%s, 1h)...' % (TICKER, periodo))
    df = yf.download(TICKER, period=periodo, interval='1h',
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, type(df.columns)) and hasattr(df.columns, 'levels'):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.dropna()
    print('  %d velas | %s → %s' % (len(df), df.index[0].date(), df.index[-1].date()))
    return df


# ══════════════════════════════════════════════
# PROCESAMIENTO DE UN TRADE
# ══════════════════════════════════════════════

def procesar_trade(cuenta_estado, trade, dia_str):
    ganancia = trade['ganancia']

    cuenta_estado['capital']        += ganancia
    cuenta_estado['ganancia_total'] += ganancia

    prev = cuenta_estado['ganancia_por_dia'].get(dia_str, 0)
    cuenta_estado['ganancia_por_dia'][dia_str] = round(prev + ganancia, 2)

    # Circuit breaker
    if trade['resultado'] == 'stop_loss':
        if cuenta_estado['ultimo_dia'] == dia_str:
            cuenta_estado['consecutivas_hoy'] += 1
        else:
            cuenta_estado['consecutivas_hoy'] = 1
    else:
        cuenta_estado['consecutivas_hoy'] = 0

    cuenta_estado['ultimo_dia'] = dia_str

    cuenta_estado['trades'].append({
        'dia':        dia_str,
        'estrategia': trade['estrategia'],
        'direccion':  trade['direccion'],
        'resultado':  trade['resultado'],
        'puntos':     trade['puntos'],
        'contratos':  trade['contratos'],
        'ganancia':   round(ganancia, 2),
        'capital':    round(cuenta_estado['capital'], 2),
    })

    nuevo_estado, razon = aplicar_reglas(cuenta_estado, dia_str)
    cuenta_estado['estado']    = nuevo_estado
    cuenta_estado['razon_fin'] = razon

    return cuenta_estado


# ══════════════════════════════════════════════
# REPORTE DIARIO
# ══════════════════════════════════════════════

def generar_reporte(estado, dia_str, trades_hoy):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, 'reporte_%s.md' % dia_str)

    lineas = [
        '# Reporte LucidFlex — %s' % dia_str,
        '**Cuenta:** %s | **Plan:** $%s' % (CUENTA, PLAN['capital_inicial']),
        '',
    ]

    for estrategia in ['ORB', 'ICT']:
        c = estado[estrategia]
        ganancia_total = c['ganancia_total']
        capital        = c['capital']
        n_trades       = len(c['trades'])
        estado_cuenta  = c['estado']
        target         = PLAN['profit_target']
        drawdown       = PLAN['max_drawdown']
        progreso       = ganancia_total / target * 100 if target else 0

        tp = sum(1 for t in c['trades'] if t['resultado'] == 'take_profit')
        sl = sum(1 for t in c['trades'] if t['resultado'] == 'stop_loss')
        wr = tp / n_trades * 100 if n_trades else 0

        icono = {'activa': '🟡', 'pasada': '✅', 'explotada': '❌'}.get(estado_cuenta, '❓')

        lineas += [
            '## %s Cuenta %s %s' % (icono, estrategia, estado_cuenta.upper()),
            '| | |',
            '|---|---|',
            '| Capital | $%.2f |' % capital,
            '| Ganancia total | $%+.2f |' % ganancia_total,
            '| Progreso target | %.1f%% ($%+.0f / $%+.0f) |' % (progreso, ganancia_total, target),
            '| Trades | %d (WR %.0f%%) |' % (n_trades, wr),
            '| TP / SL | %d / %d |' % (tp, sl),
        ]

        if c['razon_fin']:
            lineas.append('| Estado | %s |' % c['razon_fin'])

        # Trade de hoy
        trade_hoy = trades_hoy.get(estrategia)
        if trade_hoy:
            r = trade_hoy
            icono_r = {'take_profit': '✅', 'stop_loss': '❌', 'timeout': '⏱️'}.get(r['resultado'], '❓')
            lineas += [
                '',
                '**Trade de hoy:**',
                '`%s %s | Entrada %.2f → Salida %.2f | %d contratos | %+.1f pts | $%+.0f` %s' % (
                    r['direccion'], r['estrategia'],
                    r['entrada'], r['salida'],
                    r['contratos'], r['puntos'], r['ganancia'],
                    icono_r
                ),
            ]
        else:
            lineas.append('')
            lineas.append('*Sin trade hoy.*')

        lineas.append('')

    # Resumen combinado
    capital_orb = estado['ORB']['capital']
    capital_ict = estado['ICT']['capital']
    gan_orb     = estado['ORB']['ganancia_total']
    gan_ict     = estado['ICT']['ganancia_total']

    lineas += [
        '---',
        '## Resumen combinado',
        '| Cuenta | Capital | Ganancia | Estado |',
        '|---|---|---|---|',
        '| ORB | $%.0f | $%+.0f | %s |' % (capital_orb, gan_orb, estado['ORB']['estado']),
        '| ICT | $%.0f | $%+.0f | %s |' % (capital_ict, gan_ict, estado['ICT']['estado']),
        '| **Total** | **$%.0f** | **$%+.0f** | |' % (capital_orb + capital_ict, gan_orb + gan_ict),
        '',
        '*Generado automáticamente por LucidFlex Bot — %s*' % datetime.now(ET).strftime('%Y-%m-%d %H:%M ET'),
    ]

    with open(path, 'w') as f:
        f.write('\n'.join(lineas))

    print('Reporte guardado: %s' % path)
    return path


# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════

def main():
    hoy    = datetime.now(ET).date()
    dia_str = str(hoy)

    print('\n' + '=' * 60)
    print('  LucidFlex Bot — %s — Cuenta %s' % (dia_str, CUENTA))
    print('=' * 60)

    # Fin de semana → no hay mercado
    if hoy.weekday() >= 5:
        print('Fin de semana — sin operaciones.')
        return

    estado = cargar_estado()
    trades_hoy = {}

    # ── Descargar datos ──
    df_90d = descargar_datos('90d')

    # Filtrar solo datos de hoy en adelante (walk-forward estricto)
    df_hasta_hoy = df_90d[df_90d.index.date <= hoy]
    if df_hasta_hoy.empty:
        print('Sin datos para hoy.')
        return

    # ── ORB ──
    print('\n[ORB] Estado: %s | Capital: $%.0f | Ganancia: $%+.0f' % (
        estado['ORB']['estado'],
        estado['ORB']['capital'],
        estado['ORB']['ganancia_total'],
    ))

    if estado['ORB']['estado'] == 'activa':
        # Circuit breaker
        if (estado['ORB']['ultimo_dia'] == dia_str and
                estado['ORB']['consecutivas_hoy'] >= MAX_CONSEC_PERDIDAS):
            print('[ORB] Circuit breaker activo — 2 pérdidas hoy, skip.')
        else:
            trade_orb, orb_sizes_nuevos, motivo = signal_orb(
                df_hasta_hoy,
                hoy,
                estado['ORB']['capital'],
                estado['ORB']['orb_sizes'],
            )
            estado['ORB']['orb_sizes'] = orb_sizes_nuevos[-20:]  # guardar últimos 20

            if trade_orb:
                print('[ORB] Trade: %s %s → %s | %+.1f pts | $%+.0f' % (
                    trade_orb['direccion'], trade_orb['resultado'],
                    trade_orb['salida'], trade_orb['puntos'], trade_orb['ganancia']
                ))
                estado['ORB'] = procesar_trade(estado['ORB'], trade_orb, dia_str)
                trades_hoy['ORB'] = trade_orb
            else:
                print('[ORB] Sin señal — %s' % (motivo or 'sin setup'))
    else:
        print('[ORB] Cuenta %s — %s' % (estado['ORB']['estado'], estado['ORB']['razon_fin']))

    # ── ICT ──
    print('\n[ICT] Estado: %s | Capital: $%.0f | Ganancia: $%+.0f' % (
        estado['ICT']['estado'],
        estado['ICT']['capital'],
        estado['ICT']['ganancia_total'],
    ))

    if estado['ICT']['estado'] == 'activa':
        if (estado['ICT']['ultimo_dia'] == dia_str and
                estado['ICT']['consecutivas_hoy'] >= MAX_CONSEC_PERDIDAS):
            print('[ICT] Circuit breaker activo — 2 pérdidas hoy, skip.')
        else:
            obs_usados_set = set(estado['ICT']['obs_usados'])
            trade_ict, obs_nuevos, motivo = signal_ict(
                df_hasta_hoy,
                hoy,
                estado['ICT']['capital'],
                obs_usados_set,
            )
            estado['ICT']['obs_usados'] = list(obs_nuevos)

            if trade_ict:
                print('[ICT] Trade: %s %s → %s | %+.1f pts | $%+.0f' % (
                    trade_ict['direccion'], trade_ict['resultado'],
                    trade_ict['salida'], trade_ict['puntos'], trade_ict['ganancia']
                ))
                estado['ICT'] = procesar_trade(estado['ICT'], trade_ict, dia_str)
                trades_hoy['ICT'] = trade_ict
            else:
                print('[ICT] Sin señal — %s' % (motivo or 'sin setup'))
    else:
        print('[ICT] Cuenta %s — %s' % (estado['ICT']['estado'], estado['ICT']['razon_fin']))

    # ── Guardar y reportar ──
    guardar_estado(estado)
    generar_reporte(estado, dia_str, trades_hoy)

    print('\n' + '=' * 60)
    print('  ORB: $%+.0f | ICT: $%+.0f | Total: $%+.0f' % (
        estado['ORB']['ganancia_total'],
        estado['ICT']['ganancia_total'],
        estado['ORB']['ganancia_total'] + estado['ICT']['ganancia_total'],
    ))
    print('=' * 60)


if __name__ == '__main__':
    main()
