"""
LucidFlex Funded Bot — Motor Intraday
======================================
Corre cada hora durante el mercado (10:35am–4:35pm ET, vía GitHub Actions).
Entra en el cierre de cada vela 1h y gestiona posiciones abiertas en tiempo real.
Dos cuentas independientes: ORB+VWAP e ICT Order Blocks.
"""

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

from config import (CUENTA, PLANES, TICKER, ESTADO_FILE, REPORTS_DIR,
                    MAX_CONSEC_PERDIDAS, CIERRE_HORA, CIERRE_MIN)
from estrategias import signal_orb_entry, signal_ict_entry, gestionar_posicion

# LIVE_MODE=true → ejecuta ordenes reales via Rithmic
LIVE_MODE = os.environ.get('LIVE_MODE', 'false').lower() == 'true'
if LIVE_MODE:
    from live_exec import submit_bracket_entry, get_open_position, flatten_position

ET   = ZoneInfo("America/New_York")
PLAN = PLANES[CUENTA]


# ══════════════════════════════════════════════
# ESTADO
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
        'estado':           'activa',
        'razon_fin':        '',
        'consecutivas_hoy': 0,
        'ultimo_dia':       '',
        'orb_sizes':        [],
        'obs_usados':       [],
        'posicion_abierta': None,   # posicion open intraday
        'ya_opero_hoy':     '',     # fecha del ultimo trade del dia
    }


def cargar_estado():
    os.makedirs(os.path.dirname(ESTADO_FILE), exist_ok=True)
    if os.path.exists(ESTADO_FILE):
        with open(ESTADO_FILE) as f:
            return json.load(f)
    return {
        'ORB_LIVE': estado_inicial('ORB_LIVE'),
        'ORB_SIM':  estado_inicial('ORB_SIM'),
        'ICT_SIM':  estado_inicial('ICT_SIM'),
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

    if capital <= PLAN['capital_inicial'] - PLAN['max_drawdown']:
        return 'explotada', 'Drawdown maximo alcanzado — capital: $%.0f' % capital

    if ganancia_total >= PLAN['profit_target']:
        max_dia = max(gan_por_dia.values()) if gan_por_dia else 0
        if ganancia_total > 0 and max_dia / ganancia_total <= 0.50:
            return 'pasada', 'Target alcanzado: $%+.0f en %d trades' % (ganancia_total, len(trades))
        return 'activa', 'Target alcanzado pero consistencia viola 50%% — dia max: $%.0f / total: $%.0f' % (max_dia, ganancia_total)

    return 'activa', ''


# ══════════════════════════════════════════════
# PROCESAMIENTO DE TRADE CERRADO
# ══════════════════════════════════════════════

def procesar_trade(cuenta_estado, trade, dia_str):
    ganancia = trade['ganancia']

    cuenta_estado['capital']        += ganancia
    cuenta_estado['ganancia_total'] += ganancia

    prev = cuenta_estado['ganancia_por_dia'].get(dia_str, 0)
    cuenta_estado['ganancia_por_dia'][dia_str] = round(prev + ganancia, 2)

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
# REPORTE
# ══════════════════════════════════════════════

def generar_reporte(estado, dia_str, trades_cerrados_hoy):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, 'reporte_%s.md' % dia_str)

    lineas = [
        '# Reporte LucidFlex — %s' % dia_str,
        '**Cuenta:** %s | **Plan:** $%s' % (CUENTA, PLAN['capital_inicial']),
        '',
    ]

    estrategias_reporte = ['ORB_LIVE'] if LIVE_MODE else ['ORB_SIM', 'ICT_SIM']
    for estrategia in estrategias_reporte:
        c = estado[estrategia]
        ganancia_total = c['ganancia_total']
        capital        = c['capital']
        n_trades       = len(c['trades'])
        estado_cuenta  = c['estado']
        target         = PLAN['profit_target']
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

        pos = c.get('posicion_abierta')
        if pos:
            lineas += [
                '',
                '**Posicion abierta:**',
                '`%s %s | Entrada %.2f | SL %.2f | TP %.2f | %d contratos`' % (
                    pos['direccion'], pos['estrategia'],
                    pos['entrada'], pos['sl'], pos['tp'], pos['contratos']
                ),
            ]

        trade_hoy = trades_cerrados_hoy.get(estrategia)
        if trade_hoy:
            r = trade_hoy
            icono_r = {'take_profit': '✅', 'stop_loss': '❌', 'timeout': '⏱️'}.get(r['resultado'], '❓')
            lineas += [
                '',
                '**Trade cerrado hoy:**',
                '`%s %s | Entrada %.2f -> Salida %.2f | %d contratos | %+.1f pts | $%+.0f` %s' % (
                    r['direccion'], r['estrategia'],
                    r['entrada'], r['salida'],
                    r['contratos'], r['puntos'], r['ganancia'],
                    icono_r
                ),
            ]
        elif not pos:
            lineas += ['', '*Sin trade hoy.*']

        lineas.append('')

    claves = ['ORB_LIVE'] if LIVE_MODE else ['ORB_SIM', 'ICT_SIM']
    total_capital = sum(estado[k]['capital']        for k in claves if k in estado)
    total_gan     = sum(estado[k]['ganancia_total'] for k in claves if k in estado)

    lineas += [
        '---',
        '## Resumen combinado',
        '| Cuenta | Capital | Ganancia | Estado |',
        '|---|---|---|---|',
    ]
    for k in claves:
        if k in estado:
            lineas.append('| %s | $%.0f | $%+.0f | %s |' % (
                k, estado[k]['capital'], estado[k]['ganancia_total'], estado[k]['estado']))
    lineas += [
        '| **Total** | **$%.0f** | **$%+.0f** | |' % (total_capital, total_gan),
        '',
        '*Actualizado — %s*' % datetime.now(ET).strftime('%Y-%m-%d %H:%M ET'),
    ]

    with open(path, 'w') as f:
        f.write('\n'.join(lineas))

    print('Reporte guardado: %s' % path)
    return path


# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════

def main():
    ahora_et = datetime.now(ET)
    hoy      = ahora_et.date()
    dia_str  = str(hoy)

    print('\n' + '=' * 60)
    print('  LucidFlex Bot — %s %s ET — Cuenta %s' % (
        dia_str, ahora_et.strftime('%H:%M'), CUENTA))
    print('=' * 60)

    if hoy.weekday() >= 5:
        print('Fin de semana — sin operaciones.')
        return

    estado = cargar_estado()

    # Cerrar posiciones huerfanas de dias anteriores
    for est in ['ORB', 'ICT']:
        if (estado[est].get('posicion_abierta') and
                estado[est].get('ya_opero_hoy', '') < dia_str):
            print('[%s] Posicion de dia anterior detectada — limpiando.' % est)
            estado[est]['posicion_abierta'] = None

    # Descargar datos hasta ahora (solo velas completas)
    print('Descargando %s (30d, 1h)...' % TICKER)
    df = yf.download(TICKER, period='30d', interval='1h',
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, type(df.columns)) and hasattr(df.columns, 'levels'):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.dropna()

    # Solo velas que cerraron hace al menos 30 min (velas completas)
    if df.index.tz is None:
        df.index = df.index.tz_localize('UTC')
    cutoff = ahora_et - timedelta(minutes=30)
    df_hasta_ahora = df[df.index.tz_convert(ET) <= cutoff]

    if df_hasta_ahora.empty:
        print('Sin datos completos aun (mercado recien abrio).')
        guardar_estado(estado)
        return

    print('  %d velas | ultima: %s ET' % (
        len(df_hasta_ahora),
        df_hasta_ahora.index[-1].tz_convert(ET).strftime('%H:%M')
    ))

    trades_cerrados_hoy = {}

    estrategias_activas = ['ORB_LIVE'] if LIVE_MODE else ['ORB_SIM', 'ICT_SIM']

    for estrategia in estrategias_activas:
        c = estado[estrategia]

        print('\n[%s] Estado: %s | Capital: $%.0f | Ganancia: $%+.0f' % (
            estrategia, c['estado'], c['capital'], c['ganancia_total']))

        if c['estado'] != 'activa':
            print('[%s] %s' % (estrategia, c['razon_fin']))
            continue

        # Circuit breaker
        if (c['ultimo_dia'] == dia_str and
                c['consecutivas_hoy'] >= MAX_CONSEC_PERDIDAS):
            print('[%s] Circuit breaker — %d perdidas hoy.' % (estrategia, MAX_CONSEC_PERDIDAS))
            continue

        # ── EOD: cerrar posicion real si es tarde ──
        es_eod = (ahora_et.hour > CIERRE_HORA or
                  (ahora_et.hour == CIERRE_HORA and ahora_et.minute >= CIERRE_MIN))
        if LIVE_MODE and c.get('posicion_abierta') and es_eod:
            pos = c['posicion_abierta']
            qty_real, dir_real = get_open_position()
            if qty_real > 0:
                print('[%s] EOD — cerrando posicion real %s x%d' % (estrategia, dir_real, qty_real))
                flatten_position(qty_real, dir_real)

        # ── Gestionar posicion abierta ──
        if c.get('posicion_abierta'):
            trade_cerrado = gestionar_posicion(c['posicion_abierta'], df_hasta_ahora, hoy)
            if trade_cerrado:
                print('[%s] CERRADO: %s | %+.1f pts | $%+.0f' % (
                    estrategia, trade_cerrado['resultado'],
                    trade_cerrado['puntos'], trade_cerrado['ganancia']))
                estado[estrategia] = procesar_trade(c, trade_cerrado, dia_str)
                estado[estrategia]['posicion_abierta'] = None
                trades_cerrados_hoy[estrategia] = trade_cerrado
            else:
                pos = c['posicion_abierta']
                print('[%s] Posicion abierta — %s desde %.2f | SL %.2f | TP %.2f' % (
                    estrategia, pos['direccion'], pos['entrada'],
                    pos['sl'], pos['tp']))

        # ── Buscar entrada nueva ──
        elif c.get('ya_opero_hoy') != dia_str:
            if estrategia in ('ORB_LIVE', 'ORB_SIM'):
                entry, orb_sizes_nuevos, motivo = signal_orb_entry(
                    df_hasta_ahora, hoy, c['capital'], c['orb_sizes'])
                estado[estrategia]['orb_sizes'] = orb_sizes_nuevos[-20:]
                if entry:
                    if LIVE_MODE:
                        order_id = submit_bracket_entry(entry)
                        if order_id is None:
                            print('[ORB] Orden rechazada — no se registra posicion.')
                            continue
                        entry['order_id'] = order_id
                    estado[estrategia]['posicion_abierta'] = entry
                    estado[estrategia]['ya_opero_hoy']     = dia_str
                    print('[ORB] ENTRADA: %s | %.2f | SL %.2f | TP %.2f | %d contratos' % (
                        entry['direccion'], entry['entrada'],
                        entry['sl'], entry['tp'], entry['contratos']))
                else:
                    print('[ORB] Sin senal — %s' % (motivo or 'sin setup'))

            else:
                obs_usados_set = set(c['obs_usados'])
                entry, obs_nuevos, motivo = signal_ict_entry(
                    df_hasta_ahora, hoy, c['capital'], obs_usados_set)
                estado[estrategia]['obs_usados'] = list(obs_nuevos)
                if entry:
                    estado[estrategia]['posicion_abierta'] = entry
                    estado[estrategia]['ya_opero_hoy']     = dia_str
                    print('[ICT] ENTRADA: %s | %.2f | SL %.2f | TP %.2f | %d contratos' % (
                        entry['direccion'], entry['entrada'],
                        entry['sl'], entry['tp'], entry['contratos']))
                else:
                    print('[ICT] Sin senal — %s' % (motivo or 'sin setup'))

        else:
            print('[%s] Ya opero hoy — esperando manana.' % estrategia)

    guardar_estado(estado)
    generar_reporte(estado, dia_str, trades_cerrados_hoy)

    print('\n' + '=' * 60)
    for k in claves:
        if k in estado:
            print('  %s: $%+.0f' % (k, estado[k]['ganancia_total']))
    print('  TOTAL: $%+.0f' % total_gan)
    print('=' * 60)


if __name__ == '__main__':
    main()
