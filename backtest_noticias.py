"""
Backtest noticias — con vs sin filtro de noticias — LucidFlex 25K
=================================================================
Verifica si operar en dias de noticias alto impacto (NFP, FOMC, CPI, etc.)
mejora o empeora el rendimiento vs saltear esos dias.
  A — con filtro (actual): skip dias con noticias
  B — sin filtro:          opera igual en dias con noticias
  C — size reducido:       opera en noticias con 1 contrato forzado (ambos trades)
"""

import random
import numpy as np
import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo

from config import (PLANES, CUENTA, MULT, RIESGO_PCT, COSTO_CONTRATO,
                    ORB_STOP_MULT, ORB_TARGET_MULT, ORB_VOLT_FILTRO,
                    ORB_HORA_INICIO, ORB_VENTANA_H, ORB_VENTANA_M,
                    CIERRE_HORA, CIERRE_MIN, ADX_MIN)
from estrategias import calcular_adx_ayer, calcular_vwap, _calcular_trade
from filtro_noticias import check_noticia

ET        = ZoneInfo("America/New_York")
CAPITAL_0 = float(PLANES[CUENTA]['capital_inicial'])
TARGET    = PLANES[CUENTA]['profit_target']
DRAWDOWN  = PLANES[CUENTA]['max_drawdown']
ITB       = CAPITAL_0 + DRAWDOWN   # $26,000: trigger lock MLL
MLL_FIJO  = CAPITAL_0              # $25,000: MLL congelado
MAX_C     = PLANES[CUENTA]['max_contratos']
N_EVALS   = 50
WARMUP    = 30
MAX_CONSEC_PERDIDAS = 2


def _simular_salida(direccion, indices_resto, highs, lows, closes, sl, tp, n_total):
    if not indices_resto:
        return 'timeout', closes[n_total - 1]
    for m in indices_resto:
        if direccion == 'LONG':
            if lows[m] <= sl:
                return 'stop_loss', sl
            if highs[m] >= tp:
                return 'take_profit', tp
        else:
            if highs[m] >= sl:
                return 'stop_loss', sl
            if lows[m] <= tp:
                return 'take_profit', tp
    return 'timeout', closes[indices_resto[-1]]


def orb_trades_dia(df_c, hoy, capital, orb_sizes, force_size=None):
    """
    force_size=None  → sizing normal por riesgo
    force_size=1     → fuerza 1 contrato en todos los trades (modo noticias reducido)
    """
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

    for trade_num in range(1, 3):
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

            if force_size is not None:
                contratos = force_size
            else:
                riesgo_usd      = capital_actual * RIESGO_PCT
                riesgo_puntos   = abs(entrada - sl)
                riesgo_contrato = riesgo_puntos * MULT
                if riesgo_contrato == 0:
                    continue
                if riesgo_contrato > riesgo_usd:
                    if trade_num == 2:
                        contratos = 1
                    else:
                        continue
                else:
                    contratos = min(max(1, int(riesgo_usd / riesgo_contrato)), MAX_C)

            resto = [j for j in indices_post_orb[k+1:]
                     if not (fechas[j].astimezone(ET).hour > CIERRE_HORA or
                             (fechas[j].astimezone(ET).hour == CIERRE_HORA and
                              fechas[j].astimezone(ET).minute >= CIERRE_MIN))]

            resultado, precio_salida = _simular_salida(
                direccion, resto, highs, lows, closes, sl, tp, len(closes))
            puntos, ganancia = _calcular_trade(
                capital_actual, entrada, sl, direccion, resultado, precio_salida, contratos)

            trades.append({
                'resultado':  resultado,
                'ganancia':   ganancia,
                'trade_num':  trade_num,
                'es_noticia': True,
            })
            capital_actual += ganancia
            min_idx = i + 1
            break

    return trades, sizes_nuevos


def simular_eval(dias, start, modo='con_filtro'):
    capital      = CAPITAL_0
    peak         = CAPITAL_0
    orb_sizes    = []
    consecutivas = 0
    trades_eval  = []

    for hoy in [d for d in dias if d >= start]:
        hay_noticia, _ = check_noticia(hoy)

        if hay_noticia and modo == 'con_filtro':
            continue
        if consecutivas >= MAX_CONSEC_PERDIDAS:
            consecutivas = 0

        df_c = df_global[df_global.index.tz_convert(ET).date <= hoy]
        if len(df_c) < 50:
            continue

        force_size = 1 if (hay_noticia and modo == 'size_reducido') else None

        trades_dia, orb_sizes = orb_trades_dia(df_c, hoy, capital, orb_sizes, force_size)

        for t in trades_dia:
            if consecutivas >= MAX_CONSEC_PERDIDAS:
                break
            capital += t['ganancia']
            trades_eval.append(t)
            if t['resultado'] == 'stop_loss':
                consecutivas += 1
            else:
                consecutivas = 0

        peak   = max(peak, capital)
        limite = MLL_FIJO if peak >= ITB else peak - DRAWDOWN

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
    noticias   = [t for t in todos if t.get('es_noticia')]

    print('\n%s' % nombre)
    print('-' * 60)
    print('  Pasadas:              %d/%d (%.0f%%)' % (len(pasadas), N_EVALS, len(pasadas) / N_EVALS * 100))
    print('  Explotadas:           %d/%d (%.0f%%)' % (len(explotadas), N_EVALS, len(explotadas) / N_EVALS * 100))
    if pasadas:
        print('  Dias prom para pasar: %.0f' % np.mean([(r['fin'] - r['start']).days for r in pasadas]))
        print('  Trades prom/eval:     %.1f' % np.mean([len(r['trades']) for r in pasadas]))
    if todos:
        print('  Win rate total:       %.0f%% (%d/%d)' % (wins / len(todos) * 100, wins, len(todos)))
        print('  P&L prom por trade:   $%+.0f' % np.mean([t['ganancia'] for t in todos]))
    if noticias:
        n_wins = sum(1 for t in noticias if t['resultado'] == 'take_profit')
        print('  Trades en noticias:   %d | WR %.0f%% | P&L prom $%+.0f' % (
            len(noticias), n_wins / len(noticias) * 100,
            np.mean([t['ganancia'] for t in noticias])))


if __name__ == '__main__':
    print('Descargando MES=F (2 años, 1h)...')
    raw = yf.download('MES=F', period='730d', interval='1h',
                      progress=False, auto_adjust=True)
    if raw.empty:
        raise SystemExit('Error: no se pudo descargar MES=F')
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.droplevel(1)
    raw.index = (raw.index.tz_convert(ET) if raw.index.tzinfo
                 else raw.index.tz_localize('UTC').tz_convert(ET))
    df_global = raw.dropna()

    dias = sorted({ts.astimezone(ET).date() for ts in df_global.index
                   if ts.astimezone(ET).weekday() < 5})
    print('Datos: %s → %s (%d dias)\n' % (dias[0], dias[-1], len(dias)))

    dias_con_noticia = sum(1 for d in dias if check_noticia(d)[0])
    print('Dias con noticias alto impacto: %d/%d (%.0f%%)\n' % (
        dias_con_noticia, len(dias), dias_con_noticia / len(dias) * 100))

    random.seed(2025)
    dias_utiles = dias[WARMUP:-60]
    starts      = sorted(random.sample(dias_utiles, N_EVALS))

    for modo, nombre in [
        ('con_filtro',    'A — Con filtro de noticias (actual)'),
        ('sin_filtro',    'B — Sin filtro (opera en dias de noticias)'),
        ('size_reducido', 'C — Size reducido en noticias (1 contrato)'),
    ]:
        resultados = []
        for start in starts:
            res, fin, cap, trades = simular_eval(dias, start, modo=modo)
            resultados.append({
                'resultado': res, 'start': start, 'fin': fin,
                'capital': cap, 'trades': trades,
            })
        resumen(nombre, resultados)

    print()
