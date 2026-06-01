"""
Backtest con filtro ATH — compara estrategia base vs. filtro que bloquea
LONGs cuando el precio está cerca del máximo de los últimos N días.

Lógica del filtro:
  - Calcula el máximo High de los últimos ATH_DIAS días de trading (excluyendo hoy)
  - Si el precio de entrada >= ese máximo * (1 - ATH_UMBRAL), salta el LONG
  - SHORTs no se filtran (solo afecta entradas largas en zonas extendidas)
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
CAPITAL_0 = float(PLAN['capital_inicial'])
DRAWDOWN  = float(PLAN['max_drawdown'])
ITB       = CAPITAL_0 + DRAWDOWN
MLL_FIJO  = CAPITAL_0
MAX_C     = PLAN['max_contratos']
SPLIT     = 0.90
DIAS_POS_MIN = 5
DIAS_CICLO   = 30
MAX_PAYOUTS  = 5

ATH_DIAS   = 20    # días de trading para calcular el máximo reciente
ATH_UMBRAL = 0.005  # bloquea LONG si precio >= max_20d * (1 - 0.5%)


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


def orb_trades_dia(df_c, hoy, capital, orb_sizes, riesgo_pct=RIESGO_PCT, filtro_ath=False):
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

    # Máximo de los últimos ATH_DIAS días de trading (excluye hoy)
    if filtro_ath:
        indices_previos = [i for i, ts in enumerate(fechas)
                           if ts.astimezone(ET).date() < hoy]
        dias_previos = sorted({fechas[i].astimezone(ET).date() for i in indices_previos})
        dias_ventana = dias_previos[-ATH_DIAS:] if len(dias_previos) >= ATH_DIAS else dias_previos
        idx_ventana  = [i for i in indices_previos
                        if fechas[i].astimezone(ET).date() in set(dias_ventana)]
        max_reciente = highs[idx_ventana].max() if idx_ventana else None
    else:
        max_reciente = None

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
                # Filtro ATH: bloquear LONG si el precio está cerca del máximo reciente
                if filtro_ath and max_reciente is not None:
                    if precio >= max_reciente * (1 - ATH_UMBRAL):
                        continue  # skip — demasiado cerca del ATH
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


def simular_funded(dias, start, riesgo_pct=RIESGO_PCT, filtro_ath=False):
    capital      = CAPITAL_0
    peak         = CAPITAL_0
    orb_sizes    = []
    consecutivas = 0

    end_date = start + timedelta(days=N_MESES * 31)

    ciclo_start    = start
    ciclo_dias_pos = 0
    payouts        = 0
    income_total   = 0.0
    ciclos_total   = 0
    ciclos_eleg    = 0
    max_dd         = 0.0
    dias_trade     = 0
    wins_dias      = 0

    for hoy in [d for d in dias if start <= d < end_date]:
        hay_noticia, _ = check_noticia(hoy)
        if hay_noticia:
            continue
        if consecutivas >= MAX_CONSEC_PERDIDAS:
            consecutivas = 0

        df_c = df_global[df_global.index.tz_convert(ET).date <= hoy]
        if len(df_c) < 50:
            continue

        trades_dia, orb_sizes = orb_trades_dia(df_c, hoy, capital, orb_sizes, riesgo_pct, filtro_ath)

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

        peak      = max(peak, capital)
        limite    = MLL_FIJO if peak >= ITB else peak - DRAWDOWN
        dd_actual = peak - capital
        max_dd    = max(max_dd, dd_actual)

        if capital <= limite:
            return {'explotada': True, 'payouts': payouts, 'income_total': income_total,
                    'capital_final': capital, 'max_dd': max_dd, 'ciclos_eleg': ciclos_eleg,
                    'ciclos_total': ciclos_total, 'dias_trade': dias_trade, 'wins_dias': wins_dias}

        if trades_dia:
            dias_trade += 1
            if ganancia_dia > 0:
                wins_dias += 1
                ciclo_dias_pos += 1

        dias_en_ciclo = (hoy - ciclo_start).days
        if dias_en_ciclo >= DIAS_CICLO:
            ciclos_total += 1
            pnl_ciclo     = capital - CAPITAL_0
            elegible = ciclo_dias_pos >= DIAS_POS_MIN and pnl_ciclo > 0
            if elegible:
                ciclos_eleg += 1
                income_total += pnl_ciclo * SPLIT
                payouts      += 1
                capital      = CAPITAL_0
                peak         = CAPITAL_0
                if payouts >= MAX_PAYOUTS:
                    break
            ciclo_start    = hoy
            ciclo_dias_pos = 0

    return {'explotada': False, 'payouts': payouts, 'income_total': income_total,
            'capital_final': capital, 'max_dd': max_dd, 'ciclos_eleg': ciclos_eleg,
            'ciclos_total': ciclos_total, 'dias_trade': dias_trade, 'wins_dias': wins_dias}


def imprimir_resumen(nombre, resultados):
    explotadas  = [r for r in resultados if r['explotada']]
    sobreviven  = [r for r in resultados if not r['explotada']]
    blowup_pct  = len(explotadas) / N_SIMS * 100
    incomes     = [r['income_total'] for r in sobreviven]
    income_mens = [r['income_total'] / N_MESES for r in sobreviven]
    pct_ciclos  = [r['ciclos_eleg'] / r['ciclos_total'] * 100
                   for r in resultados if r['ciclos_total'] > 0]
    max_dds     = [r['max_dd'] for r in resultados]
    wr_list     = [r['wins_dias'] / r['dias_trade'] * 100
                   for r in resultados if r['dias_trade'] > 0]
    p25, p50, p75 = (np.percentile(incomes, [25, 50, 75]) if incomes else (0, 0, 0))

    print('\n  ── %s ──' % nombre)
    print('  %-42s %s' % ('Blowup en 12m:',
                           '%.0f%%  (%d/%d)' % (blowup_pct, len(explotadas), N_SIMS)))
    if sobreviven:
        print('  %-42s $%.0f / mes  (mediana $%.0f)' % (
            'Income neto (90%% split):', np.mean(income_mens), np.median(income_mens)))
        print('  %-42s $%.0f / $%.0f / $%.0f' % (
            'Income total 12m (p25/p50/p75):', p25, p50, p75))
        print('  %-42s %.1f / %d' % (
            'Payouts promedio:', np.mean([r['payouts'] for r in sobreviven]), MAX_PAYOUTS))
    print('  %-42s %.0f%%' % ('Payout eligibility:', np.mean(pct_ciclos) if pct_ciclos else 0))
    print('  %-42s $%.0f  (p90: $%.0f  max: $%.0f)' % (
        'Max DD prom / p90 / max:',
        np.mean(max_dds), np.percentile(max_dds, 90), np.max(max_dds)))
    print('  %-42s %.0f%%' % ('Win rate días:', np.mean(wr_list) if wr_list else 0))


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
    if len(dias_utiles) < N_SIMS:
        raise SystemExit('No hay suficientes días históricos.')
    starts = sorted(random.sample(dias_utiles, N_SIMS))

    sep = '=' * 72
    escenarios = [
        ('BASE — sin filtro ATH  (1%% riesgo)',   False, 0.0100),
        ('FILTRO ATH 0.5%%  (20d high, 1%% riesgo)', True,  0.0100),
    ]

    print(sep)
    print('  BACKTEST ATH FILTER — LucidFlex 25K  |  %d sims × %d meses' % (N_SIMS, N_MESES))
    print('  Filtro: bloquea LONG si precio >= max(20d) * %.3f' % (1 - ATH_UMBRAL))
    print(sep)

    for nombre, filtro_ath, riesgo_pct in escenarios:
        print('\nSimulando %s...' % nombre, end=' ', flush=True)
        resultados = []
        for start in starts:
            r = simular_funded(dias, start, riesgo_pct, filtro_ath)
            resultados.append(r)
        print('listo.')
        imprimir_resumen(nombre, resultados)

    print('\n' + sep)
