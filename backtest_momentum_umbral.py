"""
Backtest comparativo: ORB_MOMENTUM_UMBRAL — 9 valores × 100 simulaciones.

Optimización: ADX, VWAP y max_20d se pre-computan UNA vez por día,
no en cada simulación. 100x más rápido.
"""
from __future__ import annotations

import warnings
import random
import numpy as np
import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo
from datetime import date, timedelta

warnings.filterwarnings('ignore')

from config import (PLANES, MULT, RIESGO_PCT, COSTO_CONTRATO,
                    ORB_STOP_MULT, ORB_TARGET_MULT, ORB_VOLT_FILTRO,
                    ORB_HORA_INICIO, ORB_MIN_INICIO, ORB_VENTANA_H, ORB_VENTANA_M,
                    CIERRE_HORA, CIERRE_MIN, ADX_MIN)
from filtro_noticias import check_noticia

ET         = ZoneInfo("America/New_York")
N_EVALS    = 100
WARMUP     = 30
MAX_CONSEC = 2
PLAN       = PLANES['25k']
UMBRALES   = [0.00, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]


# ── Pre-cómputo de indicadores ────────────────────────────────────────────────

def precompute_indicators(df: pd.DataFrame, dias: list[date]) -> dict[date, dict]:
    """
    Calcula ADX, VWAP, max_20d y datos ORB para cada día de trading.
    Se llama UNA sola vez antes de todas las simulaciones.
    """
    highs  = df['High'].values
    lows   = df['Low'].values
    closes = df['Close'].values
    fechas = df.index

    # ADX vectorizado (14 períodos)
    def _adx(h, l, c):
        tr  = np.maximum(h[1:]-l[1:], np.maximum(np.abs(h[1:]-c[:-1]), np.abs(l[1:]-c[:-1])))
        dmp = np.where((h[1:]-h[:-1]) > (l[:-1]-l[1:]), np.maximum(h[1:]-h[:-1], 0), 0)
        dmn = np.where((l[:-1]-l[1:]) > (h[1:]-h[:-1]), np.maximum(l[:-1]-l[1:], 0), 0)
        period = 14
        atr_ = pd.Series(tr).ewm(span=period, adjust=False).mean().values
        dip_ = pd.Series(dmp).ewm(span=period, adjust=False).mean().values
        din_ = pd.Series(dmn).ewm(span=period, adjust=False).mean().values
        with np.errstate(invalid='ignore', divide='ignore'):
            dip_pct = np.where(atr_ > 0, 100*dip_/atr_, 0)
            din_pct = np.where(atr_ > 0, 100*din_/atr_, 0)
            dx      = np.where(dip_pct+din_pct > 0, 100*np.abs(dip_pct-din_pct)/(dip_pct+din_pct), 0)
        adx_ = pd.Series(dx).ewm(span=period, adjust=False).mean().values
        return adx_

    adx_series = _adx(highs, lows, closes)

    # VWAP acumulado (reset diario)
    cum_pv  = np.zeros(len(df))
    cum_vol = np.zeros(len(df))
    vwap_arr = np.zeros(len(df))
    prev_day = None
    for i in range(len(df)):
        d = fechas[i].astimezone(ET).date()
        if d != prev_day:
            cum_pv[i]  = (highs[i]+lows[i]+closes[i])/3 * 1  # vol=1 para datos sin volumen real
            cum_vol[i] = 1
            prev_day   = d
        else:
            cum_pv[i]  = cum_pv[i-1]  + (highs[i]+lows[i]+closes[i])/3
            cum_vol[i] = cum_vol[i-1] + 1
        vwap_arr[i] = cum_pv[i] / cum_vol[i] if cum_vol[i] > 0 else closes[i]

    # Índices por día
    day_indices: dict[date, list[int]] = {}
    for i, ts in enumerate(fechas):
        d = ts.astimezone(ET).date()
        if d not in day_indices:
            day_indices[d] = []
        day_indices[d].append(i)

    result: dict[date, dict] = {}

    for dia in dias:
        indices_hoy = day_indices.get(dia, [])
        if len(indices_hoy) < 2:
            continue

        # ADX del día anterior (último valor del día anterior)
        dias_antes = [d for d in day_indices if d < dia]
        if not dias_antes:
            continue
        dia_ant    = max(dias_antes)
        last_i_ant = day_indices[dia_ant][-1]
        adx_val    = adx_series[last_i_ant - 1] if last_i_ant > 0 else 0

        # ORB bar (primera vela >= 9:30 ET)
        i0 = next((i for i in indices_hoy
                   if (fechas[i].astimezone(ET).hour, fechas[i].astimezone(ET).minute)
                   >= (ORB_HORA_INICIO, ORB_MIN_INICIO)), None)
        if i0 is None:
            continue

        orb_high = float(highs[i0])
        orb_low  = float(lows[i0])
        orb_size = orb_high - orb_low
        if orb_size <= 0:
            continue

        # Máximo de 20 días previos
        dias_20 = sorted([d for d in day_indices if d < dia])[-20:]
        idx_20  = [i for d in dias_20 for i in day_indices[d]]
        max_20d = float(highs[idx_20].max()) if idx_20 else None

        # Velas post-ORB del día
        post_orb = []
        for i in indices_hoy:
            if i <= i0:
                continue
            hora_et = fechas[i].astimezone(ET)
            if hora_et.hour > CIERRE_HORA or (hora_et.hour == CIERRE_HORA and hora_et.minute >= CIERRE_MIN):
                break
            if hora_et.hour > ORB_VENTANA_H or (hora_et.hour == ORB_VENTANA_H and hora_et.minute >= ORB_VENTANA_M):
                break
            post_orb.append(i)

        # Velas hasta cierre (para simular salida)
        hasta_cierre = []
        for i in indices_hoy:
            if i <= i0:
                continue
            hora_et = fechas[i].astimezone(ET)
            if hora_et.hour > CIERRE_HORA or (hora_et.hour == CIERRE_HORA and hora_et.minute >= CIERRE_MIN):
                break
            hasta_cierre.append(i)

        result[dia] = {
            'adx'         : adx_val,
            'orb_high'    : orb_high,
            'orb_low'     : orb_low,
            'orb_size'    : orb_size,
            'max_20d'     : max_20d,
            'post_orb'    : post_orb,
            'hasta_cierre': hasta_cierre,
            'highs'       : highs,
            'lows'        : lows,
            'closes'      : closes,
            'vwap'        : vwap_arr,
        }

    return result


# ── Trades del día (usa datos pre-computados) ─────────────────────────────────

def orb_trades_dia(dia_data: dict, capital: float, orb_sizes: list, momentum_umbral: float):
    adx = dia_data['adx']
    if 0 < adx < ADX_MIN:
        return [], orb_sizes

    orb_high  = dia_data['orb_high']
    orb_low   = dia_data['orb_low']
    orb_size  = dia_data['orb_size']
    max_20d   = dia_data['max_20d']
    post_orb  = dia_data['post_orb']
    hasta_c   = dia_data['hasta_cierre']
    highs     = dia_data['highs']
    lows      = dia_data['lows']
    closes    = dia_data['closes']
    vwap      = dia_data['vwap']

    sizes_nuevos = orb_sizes.copy()
    if len(orb_sizes) >= 5:
        promedio = np.mean(orb_sizes[-10:])
        if orb_size > promedio * ORB_VOLT_FILTRO:
            sizes_nuevos.append(orb_size)
            return [], sizes_nuevos
    sizes_nuevos.append(orb_size)

    stop_dist   = orb_size * ORB_STOP_MULT
    target_dist = orb_size * ORB_TARGET_MULT
    trades      = []
    min_idx     = 0
    capital_act = capital

    for trade_num in range(1, 3):
        for k, i in enumerate(post_orb):
            if i < min_idx:
                continue

            precio     = closes[i]
            vwap_i     = vwap[i]
            rango_vela = highs[i] - lows[i]
            close_pos  = (closes[i] - lows[i]) / rango_vela if rango_vela > 0 else 0.5
            entrada    = None
            direccion  = None

            if closes[i] > orb_high and precio > vwap_i:
                if momentum_umbral > 0 and close_pos < (1 - momentum_umbral):
                    continue
                if max_20d and precio >= max_20d * 0.995:
                    continue
                entrada   = orb_high
                sl        = entrada - stop_dist
                tp        = entrada + target_dist
                direccion = 'LONG'
            elif closes[i] < orb_low and precio < vwap_i:
                if momentum_umbral > 0 and close_pos > momentum_umbral:
                    continue
                entrada   = orb_low
                sl        = entrada + stop_dist
                tp        = entrada - target_dist
                direccion = 'SHORT'

            if entrada is None:
                continue

            riesgo_usd      = capital_act * RIESGO_PCT
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
                contratos = min(max(1, int(riesgo_usd / riesgo_contrato)), PLAN['max_contratos'])

            resto = [j for j in hasta_c if j > i]
            resultado, precio_salida = 'timeout', closes[resto[-1]] if resto else closes[i]
            for m in resto:
                if direccion == 'LONG':
                    if lows[m] <= sl:  resultado, precio_salida = 'stop_loss', sl; break
                    if highs[m] >= tp: resultado, precio_salida = 'take_profit', tp; break
                else:
                    if highs[m] >= sl: resultado, precio_salida = 'stop_loss', sl; break
                    if lows[m] <= tp:  resultado, precio_salida = 'take_profit', tp; break

            puntos   = (precio_salida - entrada) if direccion == 'LONG' else (entrada - precio_salida)
            ganancia = puntos * MULT * contratos - COSTO_CONTRATO * contratos

            trades.append({'resultado': resultado, 'ganancia': ganancia})
            capital_act += ganancia
            min_idx = i + 1
            break

    return trades, sizes_nuevos


# ── Simulación de evaluación ──────────────────────────────────────────────────

def simular_eval(dias_data: dict[date, dict], dias: list[date], start: date, momentum_umbral: float):
    capital_0    = float(PLAN['capital_inicial'])
    target       = PLAN['profit_target']
    drawdown     = PLAN['max_drawdown']
    itb          = capital_0 + drawdown

    capital      = capital_0
    peak         = capital_0
    orb_sizes    = []
    consecutivas = 0
    trades_total = []
    dias_op      = 0

    for dia in [d for d in dias if d >= start]:
        hay_noticia, _ = check_noticia(dia)
        if hay_noticia:
            continue
        if consecutivas >= MAX_CONSEC:
            consecutivas = 0
        if dia not in dias_data:
            continue

        trades_dia, orb_sizes = orb_trades_dia(dias_data[dia], capital, orb_sizes, momentum_umbral)

        if trades_dia:
            dias_op += 1
        for t in trades_dia:
            if consecutivas >= MAX_CONSEC:
                break
            capital += t['ganancia']
            trades_total.append(t)
            consecutivas = (consecutivas + 1) if t['resultado'] == 'stop_loss' else 0

        peak   = max(peak, capital)
        limite = capital_0 if peak >= itb else peak - drawdown

        if capital - capital_0 >= target:
            return 'PASADA', dia, trades_total, dias_op
        if capital <= limite:
            return 'EXPLOTADA', dia, trades_total, dias_op

    return 'INCOMPLETA', dia, trades_total, dias_op


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print('Descargando MES=F (2 años, 1h)...')
    raw = yf.download('MES=F', period='730d', interval='1h', progress=False, auto_adjust=True)
    if raw.empty:
        raise SystemExit('Error descargando datos')
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.droplevel(1)
    raw.index = (raw.index.tz_convert(ET) if raw.index.tzinfo
                 else raw.index.tz_localize('UTC').tz_convert(ET))
    df_global = raw.dropna()

    dias = sorted({ts.astimezone(ET).date() for ts in df_global.index
                   if ts.astimezone(ET).weekday() < 5})
    print(f'Datos: {dias[0]} → {dias[-1]} ({len(dias)} días)')

    print('Pre-computando indicadores...')
    dias_data = precompute_indicators(df_global, dias)
    print(f'Indicadores listos para {len(dias_data)} días\n')

    random.seed(42)
    dias_utiles = dias[WARMUP:-60]
    starts      = sorted(random.sample(dias_utiles, N_EVALS))

    print(f'{"="*82}')
    print(f'  BACKTEST ORB_MOMENTUM_UMBRAL — {N_EVALS} evals × {len(UMBRALES)} umbrales')
    print(f'  25k | target $1,250 | drawdown $1,000 | riesgo 1%')
    print(f'{"="*82}')
    print(f'  {"Umbral":<14} {"Pasadas":>9} {"Explos.":>8} {"Dias":>6} {"T/dia":>7} {"WR%":>6} {"$/trade":>9} {"ROI%":>7}')
    print(f'  {"-"*76}')

    all_results = []
    for umbral in UMBRALES:
        resultados = []
        for start in starts:
            res, fin, trades, dias_op = simular_eval(dias_data, dias, start, umbral)
            resultados.append({'resultado': res, 'start': start, 'fin': fin,
                               'trades': trades, 'dias_op': dias_op})

        pasadas    = [r for r in resultados if r['resultado'] == 'PASADA']
        explotadas = [r for r in resultados if r['resultado'] == 'EXPLOTADA']
        todos      = [t for r in resultados for t in r['trades']]
        wins       = [t for t in todos if t['resultado'] == 'take_profit']
        dias_op_t  = sum(r['dias_op'] for r in resultados)

        dias_prom  = np.mean([(r['fin']-r['start']).days for r in pasadas]) if pasadas else 0
        tpd        = len(todos) / dias_op_t if dias_op_t > 0 else 0
        avg_trade  = np.mean([t['ganancia'] for t in todos]) if todos else 0
        fee        = PLAN.get('fee_evaluacion', 75)
        roi        = (len(pasadas)*PLAN['profit_target'] - N_EVALS*fee) / (N_EVALS*fee) * 100

        tag = ' ← actual' if umbral == 0.35 else (' ← sin filtro' if umbral == 0.00 else '')
        label = f'{umbral:.2f}{tag}'
        print(f'  {label:<20} {len(pasadas):>3}/{N_EVALS} ({len(pasadas)/N_EVALS*100:>3.0f}%)'
              f'  {len(explotadas):>2}/{N_EVALS} ({len(explotadas)/N_EVALS*100:>2.0f}%)'
              f'  {dias_prom:>5.0f}d'
              f'  {tpd:>5.2f}/d'
              f'  {len(wins)/len(todos)*100 if todos else 0:>4.0f}%'
              f'  ${avg_trade:>+7.0f}'
              f'  {roi:>+5.0f}%')

        all_results.append({'umbral': umbral, 'pass_rate': len(pasadas)/N_EVALS*100,
                            'blowup': len(explotadas)/N_EVALS*100, 'roi': roi,
                            'dias_prom': dias_prom, 'tpd': tpd,
                            'wr': len(wins)/len(todos)*100 if todos else 0})

    best_pass = max(all_results, key=lambda x: x['pass_rate'])
    best_roi  = max(all_results, key=lambda x: x['roi'])
    actual    = next(r for r in all_results if r['umbral'] == 0.35)

    print(f'\n{"="*82}')
    print(f'  Mejor pass rate : umbral={best_pass["umbral"]:.2f}  →  {best_pass["pass_rate"]:.0f}% pasadas  |  ROI {best_pass["roi"]:+.0f}%')
    print(f'  Mejor ROI       : umbral={best_roi["umbral"]:.2f}  →  {best_roi["pass_rate"]:.0f}% pasadas  |  ROI {best_roi["roi"]:+.0f}%')
    print(f'  Umbral actual   : 0.35  →  {actual["pass_rate"]:.0f}% pasadas  |  ROI {actual["roi"]:+.0f}%')
    print(f'{"="*82}\n')
