"""
Backtest payout strategies — LucidFlex 25K Funded
===================================================
El MLL NO resetea después de un payout (confirmado: proptradingvibes.com).
El balance baja, el MLL se queda donde estaba.

Compara 3 estrategias de retiro con riesgo 0.75% (funded config):

  A — Sin retiro        : nunca retira, capital compounding
  B — Retiro agresivo   : retira todo el profit del ciclo (MLL queda fijo)
  C — Retiro con floor  : retira solo lo que supera FLOOR ($26,500)
                          mantiene siempre $1,500 de buffer sobre MLL ($25k)
"""

import random
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from config import (PLANES, MULT, RIESGO_PCT,
                    ORB_STOP_MULT, ORB_TARGET_MULT, ORB_VOLT_FILTRO,
                    ORB_HORA_INICIO, ORB_MIN_INICIO,
                    ORB_VENTANA_H, ORB_VENTANA_M,
                    CIERRE_HORA, CIERRE_MIN, ADX_MIN)
from estrategias import calcular_adx_ayer, calcular_vwap, _calcular_trade
from filtro_noticias import check_noticia

ET      = ZoneInfo("America/New_York")
N_SIMS  = 50
N_MESES = 12
WARMUP  = 30
MAX_CONSEC_PERDIDAS = 2

PLAN      = PLANES['25k']
CAPITAL_0 = float(PLAN['capital_inicial'])   # $25,000
DRAWDOWN  = float(PLAN['max_drawdown'])       # $1,000
ITB       = CAPITAL_0 + DRAWDOWN             # $26,000 — lock trigger
MLL_FIJO  = CAPITAL_0                        # $25,000 — MLL bloqueado
MAX_C     = PLAN['max_contratos']
SPLIT     = 0.90
DIAS_POS_MIN = 5
DIAS_CICLO   = 30
MAX_PAYOUTS  = 5

RIESGO_FUNDED = 0.0075   # 0.75% — config funded
FLOOR         = 26_500.0  # estrategia C: nunca bajar de acá


def _simular_salida(direccion, indices_resto, highs, lows, closes, sl, tp, n_total):
    if not indices_resto:
        return 'timeout', closes[n_total - 1]
    for m in indices_resto:
        if direccion == 'LONG':
            if lows[m] <= sl:  return 'stop_loss', sl
            if highs[m] >= tp: return 'take_profit', tp
        else:
            if highs[m] >= sl: return 'stop_loss', sl
            if lows[m] <= tp:  return 'take_profit', tp
    return 'timeout', closes[indices_resto[-1]]


def orb_trades_dia(df_c, hoy, capital, orb_sizes):
    adx_ayer = calcular_adx_ayer(df_c)
    if 0 < adx_ayer < ADX_MIN:
        return [], orb_sizes

    vwap_vals = calcular_vwap(df_c)
    closes = df_c['Close'].values
    highs  = df_c['High'].values
    lows   = df_c['Low'].values
    fechas = df_c.index

    indices_hoy = [i for i, ts in enumerate(fechas)
                   if ts.astimezone(ET).date() == hoy]
    if len(indices_hoy) < 3:
        return [], orb_sizes

    i0 = next((i for i in indices_hoy
               if (fechas[i].astimezone(ET).hour,
                   fechas[i].astimezone(ET).minute) >= (ORB_HORA_INICIO, ORB_MIN_INICIO)), None)
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
    trades           = []
    min_idx          = 0
    capital_actual   = capital

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
                entrada, sl, tp, direccion = orb_high, orb_high - stop_dist, orb_high + target_dist, 'LONG'
            elif closes[i] < orb_low and precio < vwap_i:
                entrada, sl, tp, direccion = orb_low, orb_low + stop_dist, orb_low - target_dist, 'SHORT'

            if entrada is None:
                continue

            riesgo_usd      = capital_actual * RIESGO_FUNDED
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

            trades.append({'resultado': resultado, 'ganancia': ganancia})
            capital_actual += ganancia
            min_idx = i + 1
            break

    return trades, sizes_nuevos


def simular(dias, start, modo='sin_retiro'):
    """
    modo: 'sin_retiro' | 'agresivo' | 'floor'
    """
    capital      = CAPITAL_0
    peak         = CAPITAL_0   # peak HISTORICO — nunca baja con payouts
    orb_sizes    = []
    consecutivas = 0

    end_date = start + timedelta(days=N_MESES * 31)

    ciclo_start       = start
    capital_ciclo_ini = CAPITAL_0
    ciclo_dias_pos    = 0
    payouts           = 0
    income_total      = 0.0
    ciclos_total      = 0
    ciclos_eleg       = 0
    max_dd            = 0.0
    dias_trade        = 0
    wins_dias         = 0
    buffer_minimo     = float('inf')

    for hoy in [d for d in dias if start <= d < end_date]:
        hay_noticia, _ = check_noticia(hoy)
        if hay_noticia:
            continue
        if consecutivas >= MAX_CONSEC_PERDIDAS:
            consecutivas = 0

        df_c = df_global[df_global.index.tz_convert(ET).date <= hoy]
        if len(df_c) < 50:
            continue

        trades_dia, orb_sizes = orb_trades_dia(df_c, hoy, capital, orb_sizes)

        ganancia_dia = 0.0
        for t in trades_dia:
            if consecutivas >= MAX_CONSEC_PERDIDAS:
                break
            capital      += t['ganancia']
            ganancia_dia += t['ganancia']
            if t['resultado'] == 'stop_loss':
                consecutivas += 1
            else:
                consecutivas = 0

        # EOD: actualizar peak histórico (no baja con payouts)
        peak   = max(peak, capital)
        mll    = MLL_FIJO if peak >= ITB else peak - DRAWDOWN
        buffer = capital - mll
        buffer_minimo = min(buffer_minimo, buffer)
        max_dd = max(max_dd, peak - capital)

        # Blowup check
        if capital <= mll:
            return {
                'explotada': True, 'payouts': payouts,
                'income_total': income_total, 'capital_final': capital,
                'max_dd': max_dd, 'ciclos_eleg': ciclos_eleg,
                'ciclos_total': ciclos_total, 'dias_trade': dias_trade,
                'wins_dias': wins_dias, 'buffer_min': buffer_minimo,
            }

        if trades_dia:
            dias_trade += 1
            if ganancia_dia > 0:
                wins_dias += 1
                ciclo_dias_pos += 1

        # Fin de ciclo
        dias_en_ciclo = (hoy - ciclo_start).days
        if dias_en_ciclo >= DIAS_CICLO:
            ciclos_total += 1
            pnl_ciclo     = capital - capital_ciclo_ini

            elegible = ciclo_dias_pos >= DIAS_POS_MIN and pnl_ciclo > 0
            if elegible:
                ciclos_eleg += 1

                if modo == 'sin_retiro':
                    ingreso = 0.0   # no retira nada

                elif modo == 'agresivo':
                    # Retira todo el profit del ciclo (90%)
                    # MLL NO resetea — peak se mantiene
                    ingreso  = pnl_ciclo * SPLIT
                    capital -= ingreso

                elif modo == 'floor':
                    # Solo retira lo que supera el FLOOR
                    retiro_max = max(0.0, capital - FLOOR)
                    ingreso    = min(pnl_ciclo * SPLIT, retiro_max)
                    capital   -= ingreso

                income_total += ingreso
                payouts      += 1

                if payouts >= MAX_PAYOUTS:
                    break

            ciclo_start       = hoy
            capital_ciclo_ini = capital
            ciclo_dias_pos    = 0

    return {
        'explotada': False, 'payouts': payouts,
        'income_total': income_total, 'capital_final': capital,
        'max_dd': max_dd, 'ciclos_eleg': ciclos_eleg,
        'ciclos_total': ciclos_total, 'dias_trade': dias_trade,
        'wins_dias': wins_dias, 'buffer_min': buffer_minimo,
    }


def imprimir_resumen(nombre, resultados):
    explotadas = [r for r in resultados if r['explotada']]
    sobreviven = [r for r in resultados if not r['explotada']]

    blowup_pct   = len(explotadas) / N_SIMS * 100
    incomes      = [r['income_total'] for r in sobreviven]
    income_mens  = [r['income_total'] / N_MESES for r in sobreviven]
    buffers_min  = [r['buffer_min'] for r in resultados]
    capital_fin  = [r['capital_final'] for r in sobreviven]
    max_dds      = [r['max_dd'] for r in resultados]
    pct_eleg     = [r['ciclos_eleg'] / r['ciclos_total'] * 100
                    for r in resultados if r['ciclos_total'] > 0]
    wr_list      = [r['wins_dias'] / r['dias_trade'] * 100
                    for r in resultados if r['dias_trade'] > 0]

    p25, p50, p75 = (np.percentile(incomes, [25, 50, 75]) if incomes else (0, 0, 0))

    print('\n  ── %s ──' % nombre)
    print('  %-44s %s' % ('Blowup en 12m:',
          '%.0f%%  (%d/%d)' % (blowup_pct, len(explotadas), N_SIMS)))
    if explotadas:
        print('  %-44s %.1f payouts' % ('Payouts antes de explotar:',
              np.mean([r['payouts'] for r in explotadas])))
    if sobreviven:
        print('  %-44s $%.0f / mes  (mediana $%.0f)' % (
            'Income neto (90%% split):',
            np.mean(income_mens), np.median(income_mens)))
        print('  %-44s $%.0f / $%.0f / $%.0f' % (
            'Income total 12m (p25/p50/p75):', p25, p50, p75))
        print('  %-44s $%.0f' % (
            'Capital final mediano en cuenta:', np.median(capital_fin)))
    print('  %-44s $%.0f  (mínimo absoluto: $%.0f)' % (
        'Buffer sobre MLL prom / mínimo:', np.mean(buffers_min), np.min(buffers_min)))
    print('  %-44s $%.0f  (p90: $%.0f)' % (
        'Max DD prom / p90:', np.mean(max_dds), np.percentile(max_dds, 90)))
    print('  %-44s %.0f%%' % ('Win rate días:', np.mean(wr_list) if wr_list else 0))


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
    print('Datos: %s → %s (%d días)\n' % (dias[0], dias[-1], len(dias)))

    random.seed(2025)
    dias_utiles = dias[WARMUP: -int(N_MESES * 21)]
    starts = sorted(random.sample(dias_utiles, N_SIMS))

    sep = '=' * 72
    print(sep)
    print('  BACKTEST ESTRATEGIAS DE PAYOUT — LucidFlex 25K Funded')
    print('  Riesgo: 0.75%% | MLL NO resetea tras payout (regla real)')
    print(sep)

    escenarios = [
        ('A — Sin retiro (capital compounding)',       'sin_retiro'),
        ('B — Retiro agresivo (todo el profit)',       'agresivo'),
        ('C — Retiro con floor $26,500 (+$1,500 MLL)', 'floor'),
    ]

    for nombre, modo in escenarios:
        print('\nSimulando %s...' % nombre, end=' ', flush=True)
        resultados = [simular(dias, s, modo) for s in starts]
        print('listo.')
        imprimir_resumen(nombre, resultados)

    print('\n' + sep)
    print()
