"""
Backtest comparativo — 1 trade vs 2 trades/dia, 1% vs 1.2% riesgo — LucidFlex 25K
====================================================================================
Compara 5 escenarios en N_EVALS evaluaciones distintas:
  A: 1 trade/dia, riesgo 1%   (config actual)
  B: 2 trades/dia, riesgo 1%  (2do se saltea si ORB muy grande)
  C: 2 trades/dia, 2do forzado 1 contrato, riesgo 1%
  D: 1 trade/dia, riesgo 1.2%
  E: 2 trades/dia, 2do forzado 1 contrato, riesgo 1.2%
"""

import yfinance as yf
import numpy as np
from zoneinfo import ZoneInfo

from config import (PLANES, CUENTA, MULT, RIESGO_PCT, COSTO_CONTRATO,
                    ORB_STOP_MULT, ORB_TARGET_MULT, ORB_VOLT_FILTRO,
                    ORB_HORA_INICIO, ORB_VENTANA_H, ORB_VENTANA_M,
                    CIERRE_HORA, CIERRE_MIN, ADX_MIN)
from estrategias import (calcular_adx_ayer, calcular_vwap,
                         _simular_salida, _calcular_trade)
from filtro_noticias import check_noticia

ET        = ZoneInfo("America/New_York")
CAPITAL_0 = float(PLANES[CUENTA]['capital_inicial'])
TARGET    = PLANES[CUENTA]['profit_target']
DRAWDOWN  = PLANES[CUENTA]['max_drawdown']
ITB       = CAPITAL_0 + DRAWDOWN   # $26,000: trigger lock MLL
MLL_FIJO  = CAPITAL_0              # $25,000: MLL congelado
N_EVALS   = 30
WARMUP    = 30
MAX_CONSEC_PERDIDAS = 2


def orb_trades_dia(df_c, hoy, capital, orb_sizes, max_trades, force_2do, riesgo_pct=RIESGO_PCT):
    """
    Encuentra hasta max_trades señales ORB en el dia hoy.
    force_2do=True: el 2do trade usa 1 contrato forzado si riesgo > 1%.
    Devuelve (lista_de_trades, orb_sizes_actualizado).
    """
    max_c = PLANES[CUENTA]['max_contratos']

    adx_ayer = calcular_adx_ayer(df_c)
    if 0 < adx_ayer < ADX_MIN:
        return [], orb_sizes

    vwap_vals = calcular_vwap(df_c)
    closes    = df_c['Close'].values
    highs     = df_c['High'].values
    lows      = df_c['Low'].values
    fechas    = df_c.index

    indices_hoy = [i for i, ts in enumerate(fechas)
                   if ts.astimezone(ET).date() == hoy]
    if len(indices_hoy) < 3:
        return [], orb_sizes

    i0 = next((i for i in indices_hoy
               if fechas[i].astimezone(ET).hour >= ORB_HORA_INICIO), None)
    if i0 is None:
        return [], orb_sizes

    orb_high = highs[i0]
    orb_low  = lows[i0]
    orb_size = orb_high - orb_low
    if orb_size <= 0:
        return [], orb_sizes

    sizes_nuevos = orb_sizes.copy()
    if len(orb_sizes) >= 5:
        promedio = np.mean(orb_sizes[-10:])
        if orb_size > promedio * ORB_VOLT_FILTRO:
            sizes_nuevos.append(orb_size)
            return [], sizes_nuevos
    sizes_nuevos.append(orb_size)

    stop_dist   = orb_size * ORB_STOP_MULT
    target_dist = orb_size * ORB_TARGET_MULT

    indices_post_orb = [i for i in indices_hoy if i > i0]

    trades         = []
    min_idx        = 0
    capital_actual = capital

    for trade_num in range(1, max_trades + 1):
        for k, i in enumerate(indices_post_orb):
            if i < min_idx:
                continue

            hora_et = fechas[i].astimezone(ET)
            if hora_et.hour > CIERRE_HORA or (hora_et.hour == CIERRE_HORA and hora_et.minute >= CIERRE_MIN):
                break
            if hora_et.hour > ORB_VENTANA_H or (hora_et.hour == ORB_VENTANA_H and hora_et.minute >= ORB_VENTANA_M):
                break

            precio  = closes[i]
            vwap_i  = vwap_vals[i]
            entrada = None

            if closes[i] > orb_high and precio > vwap_i:
                entrada   = orb_high
                sl        = entrada - stop_dist
                tp        = entrada + target_dist
                direccion = 'LONG'
            elif closes[i] < orb_low and precio < vwap_i:
                entrada   = orb_low
                sl        = entrada + stop_dist
                tp        = entrada - target_dist
                direccion = 'SHORT'

            if entrada is None:
                continue

            riesgo_usd      = capital_actual * riesgo_pct
            riesgo_puntos   = abs(entrada - sl)
            riesgo_contrato = riesgo_puntos * MULT

            if riesgo_contrato == 0:
                continue

            if riesgo_contrato > riesgo_usd:
                if trade_num == 2 and force_2do:
                    contratos = 1
                else:
                    continue
            else:
                contratos = min(max(1, int(riesgo_usd / riesgo_contrato)), max_c)

            resto = [j for j in indices_post_orb[k+1:]
                     if not (fechas[j].astimezone(ET).hour > CIERRE_HORA or
                             (fechas[j].astimezone(ET).hour == CIERRE_HORA and
                              fechas[j].astimezone(ET).minute >= CIERRE_MIN))]

            resultado, precio_salida = _simular_salida(
                direccion, resto, highs, lows, closes, sl, tp, len(closes)
            )
            puntos, ganancia = _calcular_trade(
                capital_actual, entrada, sl, direccion, resultado, precio_salida, contratos
            )

            trades.append({
                'direccion':   direccion,
                'entrada':     round(entrada, 2),
                'salida':      round(precio_salida, 2),
                'resultado':   resultado,
                'contratos':   contratos,
                'puntos':      puntos,
                'ganancia':    ganancia,
                'hora_entrada': hora_et.hour,
                'trade_num':   trade_num,
            })

            capital_actual += ganancia
            min_idx = i + 1
            break

    return trades, sizes_nuevos


def simular_eval(dias, start, max_trades, force_2do, riesgo_pct=RIESGO_PCT):
    capital   = CAPITAL_0
    peak      = CAPITAL_0
    orb_sizes = []
    consecutivas = 0
    trades_eval  = []

    for hoy in [d for d in dias if d >= start]:
        hay_noticia, _ = check_noticia(hoy)
        if hay_noticia:
            continue
        if consecutivas >= MAX_CONSEC_PERDIDAS:
            consecutivas = 0  # reset cada dia

        df_c = df_global[df_global.index.tz_convert(ET).date <= hoy]
        if len(df_c) < 50:
            continue

        trades_dia, orb_sizes = orb_trades_dia(
            df_c, hoy, capital, orb_sizes, max_trades, force_2do, riesgo_pct)

        for t in trades_dia:
            if consecutivas >= MAX_CONSEC_PERDIDAS:
                break
            capital += t['ganancia']
            trades_eval.append(t)
            if t['resultado'] == 'stop_loss':
                consecutivas += 1
            else:
                consecutivas = 0

        peak    = max(peak, capital)
        limite  = MLL_FIJO if peak >= ITB else peak - DRAWDOWN

        if capital - CAPITAL_0 >= TARGET:
            return 'PASADA', hoy, capital, trades_eval
        if capital <= limite:
            return 'EXPLOTADA', hoy, capital, trades_eval

    return 'INCOMPLETA', hoy, capital, trades_eval


def resumen(nombre, resultados):
    pasadas    = [r for r in resultados if r['resultado'] == 'PASADA']
    explotadas = [r for r in resultados if r['resultado'] == 'EXPLOTADA']
    todos      = [t for r in resultados for t in r['trades']]
    wins       = sum(1 for t in todos if t['resultado'] == 'take_profit')

    print("\n%s" % nombre)
    print("-" * 50)
    print("  Pasadas:    %d/%d (%.0f%%)" % (len(pasadas), N_EVALS, len(pasadas)/N_EVALS*100))
    print("  Explotadas: %d/%d (%.0f%%)" % (len(explotadas), N_EVALS, len(explotadas)/N_EVALS*100))
    if pasadas:
        print("  Dias prom para pasar: %.0f" % np.mean([(r['fin'] - r['start']).days for r in pasadas]))
        print("  Trades prom/eval:     %.1f" % np.mean([len(r['trades']) for r in pasadas]))
    if todos:
        print("  Win rate:             %.0f%% (%d/%d)" % (wins/len(todos)*100, wins, len(todos)))
        print("  P&L prom por trade:   $%+.0f" % np.mean([t['ganancia'] for t in todos]))
        t2 = [t for t in todos if t['trade_num'] == 2]
        if t2:
            wins2 = sum(1 for t in t2 if t['resultado'] == 'take_profit')
            print("  2do trade: %d trades | WR %.0f%% | P&L prom $%+.0f" % (
                len(t2), wins2/len(t2)*100, np.mean([t['ganancia'] for t in t2])))


if __name__ == '__main__':
    print("Descargando MES=F (2 años, 1h)...")
    import pandas as pd
    raw = yf.download('MES=F', period='730d', interval='1h',
                      progress=False, auto_adjust=True)
    if raw.empty:
        raise SystemExit("Error: no se pudo descargar MES=F")
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.droplevel(1)
    raw.index = (raw.index.tz_convert(ET) if raw.index.tzinfo
                 else raw.index.tz_localize('UTC').tz_convert(ET))
    df_global = raw.dropna()

    dias = sorted({ts.astimezone(ET).date() for ts in df_global.index
                   if ts.astimezone(ET).weekday() < 5})
    print("Datos: %s → %s (%d días)\n" % (dias[0], dias[-1], len(dias)))

    import random
    random.seed(99)
    dias_utiles = dias[WARMUP:-60]  # dejar 60 dias al final para que cada eval tenga recorrido
    starts      = sorted(random.sample(dias_utiles, N_EVALS))

    escenarios = [
        ('A — 1 trade/dia,  1.0% riesgo (actual)',      1, False, 0.010),
        ('B — 2 trades/dia, 1.0% riesgo (mismo riesgo)', 2, False, 0.010),
        ('C — 2 trades/dia, 1.0% riesgo (2do forzado)', 2, True,  0.010),
        ('D — 1 trade/dia,  1.2% riesgo',               1, False, 0.012),
        ('E — 2 trades/dia, 1.2% riesgo (2do forzado)', 2, True,  0.012),
    ]

    for nombre, max_t, force, riesgo in escenarios:
        resultados = []
        for start in starts:
            res, fin, cap, trades = simular_eval(dias, start, max_t, force, riesgo)
            resultados.append({
                'resultado': res, 'start': start, 'fin': fin,
                'capital': cap, 'trades': trades,
            })
        resumen(nombre, resultados)

    print()
