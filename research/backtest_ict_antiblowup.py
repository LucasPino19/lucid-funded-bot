"""
Backtest ICT Kill Zones — Anti-Explosión — 2 años — 1h
=======================================================
4 variantes × 100 evals × 5 seeds = 2000 simulaciones.
BASE ya corrida: resultados conocidos hardcodeados.
Variantes 1-3 corren: circuit breaker, consec. losses, volatility filter.
"""

import sys
import random
import warnings
import numpy as np
import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo

warnings.filterwarnings('ignore', category=RuntimeWarning)

ET = ZoneInfo("America/New_York")

# ── Parametros de la cuenta ────────────────────────────────────────────────────
MULT           = 5
CAPITAL_0      = 25_000.0
TARGET         = 1_250.0
DRAWDOWN       = 1_000.0
ITB            = 26_250.0
MLL_FIJO       = 24_900.0
RIESGO_PCT     = 0.01
COSTO_CONTRATO = 4
MAX_CONTRATOS  = 20
N_CUENTAS      = 100
WARMUP         = 30
SEEDS          = [42, 123, 456, 789, 999]

# ── Parametros Kill Zone adaptada a 1h ────────────────────────────────────────
KZ_HORA_INICIO = 7
KZ_HORA_FIN    = 12   # ventana 7am-12pm ET (5 velas 1h)
KZ_SWING_BARS  = 3    # lookback 3 barras = 3 horas
KZ_SL_MULT     = 1.0
KZ_TP_MULT     = 2.0
ADX_MIN        = 20
FEE            = 75
SPLIT          = 0.90

# Resultados BASE conocidos (hardcodeados del backtest anterior)
BASE_RESULTS = {
    42:  {'pas': 64, 'inv': 0, 'exp': 30, 'inc': 6,  'dias_prom': 32.7, 'pnl_pas': 0.0},
    123: {'pas': 63, 'inv': 0, 'exp': 31, 'inc': 6,  'dias_prom': 32.7, 'pnl_pas': 0.0},
    456: {'pas': 64, 'inv': 0, 'exp': 30, 'inc': 6,  'dias_prom': 32.7, 'pnl_pas': 0.0},
    789: {'pas': 64, 'inv': 0, 'exp': 30, 'inc': 6,  'dias_prom': 32.7, 'pnl_pas': 0.0},
    999: {'pas': 64, 'inv': 0, 'exp': 29, 'inc': 7,  'dias_prom': 32.7, 'pnl_pas': 0.0},
}
# Totales BASE conocidos
BASE_TOTALES = {
    'pas': 319, 'inv': 0, 'exp': 150, 'inc': 31,
    'dias_glob': 32.7, 'roi': 1002.4
}


# ══════════════════════════════════════════════════════════════════════════════
# UTILIDADES (idénticas a backtest_ict_largo.py)
# ══════════════════════════════════════════════════════════════════════════════

def calcular_vwap(df):
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    d  = df.copy()
    d['_tp']   = tp
    d['_tpv']  = tp * d['Volume']
    d['_date'] = d.index.normalize()
    d['_ctpv'] = d.groupby('_date')['_tpv'].cumsum()
    d['_cvol'] = d.groupby('_date')['Volume'].cumsum()
    return (d['_ctpv'] / d['_cvol']).values


def calcular_adx_ayer(df, period=14):
    """ADX del dia anterior al ultimo dia en df. Sin look-ahead."""
    df_d = df[['Open', 'High', 'Low', 'Close']].resample('D').agg(
        {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}
    ).dropna()

    n = len(df_d)
    if n < 2 * period + 2:
        return 0.0

    hi = df_d['High'].values
    lo = df_d['Low'].values
    cl = df_d['Close'].values

    tr = np.zeros(n)
    dp = np.zeros(n)
    dn = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(hi[i] - lo[i], abs(hi[i] - cl[i-1]), abs(lo[i] - cl[i-1]))
        u = hi[i] - hi[i-1]
        d = lo[i-1] - lo[i]
        if u > d and u > 0: dp[i] = u
        if d > u and d > 0: dn[i] = d

    atr = np.zeros(n)
    sdp = np.zeros(n)
    sdn = np.zeros(n)
    atr[period] = tr[1:period+1].sum()
    sdp[period] = dp[1:period+1].sum()
    sdn[period] = dn[1:period+1].sum()
    for i in range(period + 1, n):
        atr[i] = atr[i-1] - atr[i-1] / period + tr[i]
        sdp[i] = sdp[i-1] - sdp[i-1] / period + dp[i]
        sdn[i] = sdn[i-1] - sdn[i-1] / period + dn[i]

    with np.errstate(divide='ignore', invalid='ignore'):
        dip = np.where(atr > 0, 100 * sdp / atr, 0)
        din = np.where(atr > 0, 100 * sdn / atr, 0)
        dx  = np.where(dip + din > 0, 100 * np.abs(dip - din) / (dip + din), 0)

    adx = np.zeros(n)
    if n > 2 * period:
        adx[2 * period] = dx[period:2 * period].mean()
        for i in range(2 * period + 1, n):
            adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period

    return float(adx[-2])


def _simular_salida(direccion, indices_resto, highs, lows, closes, sl, tp, n_total):
    """Simula stop/target en el resto del dia. Cierra antes de las 4:30pm."""
    if not indices_resto:
        return 'timeout', closes[n_total - 1]
    resultado     = 'timeout'
    precio_salida = closes[indices_resto[-1]]
    for m in indices_resto:
        if direccion == 'LONG':
            if lows[m] <= sl:  return 'stop_loss', sl
            if highs[m] >= tp: return 'take_profit', tp
        else:
            if highs[m] >= sl: return 'stop_loss', sl
            if lows[m] <= tp:  return 'take_profit', tp
    return resultado, precio_salida


def _calcular_trade(entrada, sl, direccion, resultado, precio_salida, contratos):
    puntos   = (precio_salida - entrada) if direccion == 'LONG' else (entrada - precio_salida)
    ganancia = round(puntos * MULT * contratos - COSTO_CONTRATO * contratos, 2)
    return round(puntos, 2), ganancia


def ajustar_realismo(trade, rng):
    contratos = trade['contratos']
    resultado = trade['resultado']
    ganancia  = trade['ganancia']
    ajuste    = 0.0
    if rng.random() < 0.30:
        ajuste -= rng.uniform(0.25, 0.75) * MULT * contratos
    if resultado == 'stop_loss':
        ajuste -= rng.uniform(0.0, 0.50) * MULT * contratos
    if resultado == 'take_profit' and rng.random() < 0.05:
        ajuste += ganancia * rng.uniform(0.3, 0.7) - ganancia
    if rng.random() < 0.02:
        ajuste -= abs(ganancia) * rng.uniform(0.20, 0.50)
    return round(ganancia + ajuste, 2)


# ══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA BASE — ICT Kill Zones (idéntica a backtest_ict_largo.py)
# ══════════════════════════════════════════════════════════════════════════════

def signal_kz_base(df_completo, fecha_hoy, capital):
    """
    NY Open Kill Zone: 7am-12pm ET en barras 1h.
    Swing lookback: 3 barras.
    Confirmacion: cierre > swing_high y > VWAP (LONG), o < swing_low y < VWAP (SHORT).
    """
    adx_ayer = calcular_adx_ayer(df_completo)
    if adx_ayer > 0 and adx_ayer < ADX_MIN:
        return None

    vwap_vals = calcular_vwap(df_completo)
    closes    = df_completo['Close'].values
    highs     = df_completo['High'].values
    lows      = df_completo['Low'].values
    fechas    = df_completo.index
    n_total   = len(fechas)

    indices_kz = []
    for i, ts in enumerate(fechas):
        t = ts.astimezone(ET)
        if t.date() != fecha_hoy:
            continue
        if t.hour < KZ_HORA_INICIO or t.hour >= KZ_HORA_FIN:
            continue
        indices_kz.append(i)

    if len(indices_kz) < KZ_SWING_BARS + 1:
        return None

    indices_dia = []
    for i, ts in enumerate(fechas):
        t = ts.astimezone(ET)
        if t.date() != fecha_hoy:
            continue
        if t.hour < KZ_HORA_INICIO:
            continue
        if t.hour > 16 or (t.hour == 16 and t.minute >= 30):
            continue
        indices_dia.append(i)

    for i in indices_kz:
        if i < KZ_SWING_BARS:
            continue

        swing_high  = float(np.max(highs[i - KZ_SWING_BARS:i]))
        swing_low   = float(np.min(lows[i - KZ_SWING_BARS:i]))
        swing_range = swing_high - swing_low

        if swing_range <= 0:
            continue

        close_i = closes[i]
        vwap_i  = vwap_vals[i]

        direccion = None
        if close_i > swing_high and close_i > vwap_i:
            direccion = 'LONG'
        elif close_i < swing_low and close_i < vwap_i:
            direccion = 'SHORT'

        if direccion is None:
            continue

        if direccion == 'LONG':
            entrada = swing_high
            sl      = entrada - KZ_SL_MULT * swing_range
            tp      = entrada + KZ_TP_MULT * swing_range
        else:
            entrada = swing_low
            sl      = entrada + KZ_SL_MULT * swing_range
            tp      = entrada - KZ_TP_MULT * swing_range

        riesgo_puntos = abs(entrada - sl)
        if riesgo_puntos <= 0:
            continue

        contratos = min(max(1, int(capital * RIESGO_PCT / (riesgo_puntos * MULT))), MAX_CONTRATOS)

        resto = [j for j in indices_dia if j > i]
        resultado, precio_salida = _simular_salida(
            direccion, resto, highs, lows, closes, sl, tp, n_total
        )
        puntos, ganancia = _calcular_trade(
            entrada, sl, direccion, resultado, precio_salida, contratos
        )

        return {
            'estrategia': 'KZ_1H',
            'direccion':  direccion,
            'entrada':    round(entrada, 2),
            'sl':         round(sl, 2),
            'tp':         round(tp, 2),
            'salida':     round(precio_salida, 2),
            'resultado':  resultado,
            'contratos':  contratos,
            'puntos':     puntos,
            'ganancia':   ganancia,
        }

    return None


# ══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA VARIANTE 3: Volatility Filter
# signal_kz_volt: recibe swing_hist, devuelve (trade_o_None, swing_promedio)
# ══════════════════════════════════════════════════════════════════════════════

def signal_kz_volt(df_completo, fecha_hoy, capital, swing_hist):
    """
    Igual a signal_kz_base pero con filtro de volatilidad.
    Calcula swing_promedio_hoy de las barras kill zone del dia.
    Si swing_promedio_hoy > 1.5 * mean(swing_hist[-10:]): no operar.
    Siempre devuelve (trade_o_None, swing_promedio_hoy).
    """
    adx_ayer = calcular_adx_ayer(df_completo)
    if adx_ayer > 0 and adx_ayer < ADX_MIN:
        return None, None

    vwap_vals = calcular_vwap(df_completo)
    closes    = df_completo['Close'].values
    highs     = df_completo['High'].values
    lows      = df_completo['Low'].values
    fechas    = df_completo.index
    n_total   = len(fechas)

    indices_kz = []
    for i, ts in enumerate(fechas):
        t = ts.astimezone(ET)
        if t.date() != fecha_hoy:
            continue
        if t.hour < KZ_HORA_INICIO or t.hour >= KZ_HORA_FIN:
            continue
        indices_kz.append(i)

    if len(indices_kz) < KZ_SWING_BARS + 1:
        return None, None

    indices_dia = []
    for i, ts in enumerate(fechas):
        t = ts.astimezone(ET)
        if t.date() != fecha_hoy:
            continue
        if t.hour < KZ_HORA_INICIO:
            continue
        if t.hour > 16 or (t.hour == 16 and t.minute >= 30):
            continue
        indices_dia.append(i)

    # Calcular swing_ranges de las barras kill zone del dia
    swing_ranges_hoy = []
    for i in indices_kz:
        if i < KZ_SWING_BARS:
            continue
        sh = float(np.max(highs[i - KZ_SWING_BARS:i]))
        sl_val = float(np.min(lows[i - KZ_SWING_BARS:i]))
        sr = sh - sl_val
        if sr > 0:
            swing_ranges_hoy.append(sr)

    swing_promedio_hoy = float(np.mean(swing_ranges_hoy)) if swing_ranges_hoy else None

    # Filtro de volatilidad
    if swing_promedio_hoy is not None and len(swing_hist) >= 3:
        hist_ref = swing_hist[-10:]
        umbral   = 1.5 * float(np.mean(hist_ref))
        if swing_promedio_hoy > umbral:
            return None, swing_promedio_hoy

    # Señal normal
    for i in indices_kz:
        if i < KZ_SWING_BARS:
            continue

        swing_high  = float(np.max(highs[i - KZ_SWING_BARS:i]))
        swing_low   = float(np.min(lows[i - KZ_SWING_BARS:i]))
        swing_range = swing_high - swing_low

        if swing_range <= 0:
            continue

        close_i = closes[i]
        vwap_i  = vwap_vals[i]

        direccion = None
        if close_i > swing_high and close_i > vwap_i:
            direccion = 'LONG'
        elif close_i < swing_low and close_i < vwap_i:
            direccion = 'SHORT'

        if direccion is None:
            continue

        if direccion == 'LONG':
            entrada = swing_high
            sl      = entrada - KZ_SL_MULT * swing_range
            tp      = entrada + KZ_TP_MULT * swing_range
        else:
            entrada = swing_low
            sl      = entrada + KZ_SL_MULT * swing_range
            tp      = entrada - KZ_TP_MULT * swing_range

        riesgo_puntos = abs(entrada - sl)
        if riesgo_puntos <= 0:
            continue

        contratos = min(max(1, int(capital * RIESGO_PCT / (riesgo_puntos * MULT))), MAX_CONTRATOS)

        resto = [j for j in indices_dia if j > i]
        resultado, precio_salida = _simular_salida(
            direccion, resto, highs, lows, closes, sl, tp, n_total
        )
        puntos, ganancia = _calcular_trade(
            entrada, sl, direccion, resultado, precio_salida, contratos
        )

        return {
            'estrategia': 'KZ_1H',
            'direccion':  direccion,
            'entrada':    round(entrada, 2),
            'sl':         round(sl, 2),
            'tp':         round(tp, 2),
            'salida':     round(precio_salida, 2),
            'resultado':  resultado,
            'contratos':  contratos,
            'puntos':     puntos,
            'ganancia':   ganancia,
        }, swing_promedio_hoy

    return None, swing_promedio_hoy


# ══════════════════════════════════════════════════════════════════════════════
# MOTORES run_eval (uno por variante)
# ══════════════════════════════════════════════════════════════════════════════

def _check_fin(capital, peak, gan_por_dia):
    """Evalua condicion de fin: TARGET o DRAWDOWN. Retorna resultado o None."""
    limite = MLL_FIJO if peak >= ITB else peak - DRAWDOWN
    pnl    = capital - CAPITAL_0

    if pnl >= TARGET:
        dias_op   = len(gan_por_dia)
        mejor_dia = max(gan_por_dia.values()) if gan_por_dia else 0.0
        if dias_op >= 2 and pnl > 0 and mejor_dia / pnl <= 0.50:
            return 'PASADA'
        elif dias_op < 2:
            return 'INVALIDA_DIAS'
        else:
            return 'INVALIDA_CONSIST'

    if capital <= limite:
        return 'EXPLOTADA'

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Variante BASE
# ──────────────────────────────────────────────────────────────────────────────

def run_eval_base(df, dias, start, rng):
    from filtro_noticias import check_noticia

    capital         = CAPITAL_0
    peak            = CAPITAL_0
    resultado_final = 'INCOMPLETA'
    trades          = 0
    ultimo_dia      = start
    gan_por_dia     = {}

    for hoy in [d for d in dias if d >= start]:
        ultimo_dia = hoy

        if rng.random() < 0.02:
            continue

        df_c = df[[ts.astimezone(ET).date() <= hoy for ts in df.index]]
        if len(df_c) < 50:
            continue

        hay_noticia, _ = check_noticia(hoy)
        if hay_noticia:
            continue

        trade = signal_kz_base(df_c, hoy, capital)

        if trade:
            g        = ajustar_realismo(trade, rng)
            capital += g
            trades  += 1
            gan_por_dia[hoy] = gan_por_dia.get(hoy, 0.0) + g

        peak = max(peak, capital)
        res  = _check_fin(capital, peak, gan_por_dia)
        if res:
            resultado_final = res
            break

    duracion  = (ultimo_dia - start).days
    pnl_final = round(capital - CAPITAL_0, 0)
    return resultado_final, duracion, trades, pnl_final


# ──────────────────────────────────────────────────────────────────────────────
# Variante 1: Circuit Breaker (1 dia de pausa tras cada perdida)
# ──────────────────────────────────────────────────────────────────────────────

def run_eval_cb(df, dias, start, rng):
    from filtro_noticias import check_noticia

    capital         = CAPITAL_0
    peak            = CAPITAL_0
    resultado_final = 'INCOMPLETA'
    trades          = 0
    ultimo_dia      = start
    gan_por_dia     = {}
    dias_pausa      = 0   # cuantos dias de pausa quedan

    for hoy in [d for d in dias if d >= start]:
        ultimo_dia = hoy

        # Circuit breaker: si hay dias de pausa, saltear
        if dias_pausa > 0:
            dias_pausa -= 1
            continue

        if rng.random() < 0.02:
            continue

        df_c = df[[ts.astimezone(ET).date() <= hoy for ts in df.index]]
        if len(df_c) < 50:
            continue

        hay_noticia, _ = check_noticia(hoy)
        if hay_noticia:
            continue

        trade = signal_kz_base(df_c, hoy, capital)

        if trade:
            g        = ajustar_realismo(trade, rng)
            capital += g
            trades  += 1
            gan_por_dia[hoy] = gan_por_dia.get(hoy, 0.0) + g

            # Si el trade fue perdedor: activar 1 dia de pausa
            if g < 0:
                dias_pausa = 1

        peak = max(peak, capital)
        res  = _check_fin(capital, peak, gan_por_dia)
        if res:
            resultado_final = res
            break

    duracion  = (ultimo_dia - start).days
    pnl_final = round(capital - CAPITAL_0, 0)
    return resultado_final, duracion, trades, pnl_final


# ──────────────────────────────────────────────────────────────────────────────
# Variante 2: Filtro de perdidas consecutivas (pausa tras 2 losses seguidos)
# ──────────────────────────────────────────────────────────────────────────────

def run_eval_cl(df, dias, start, rng):
    from filtro_noticias import check_noticia

    capital         = CAPITAL_0
    peak            = CAPITAL_0
    resultado_final = 'INCOMPLETA'
    trades          = 0
    ultimo_dia      = start
    gan_por_dia     = {}
    consec_losses   = 0
    en_pausa        = False   # True: este dia es el dia de pausa

    for hoy in [d for d in dias if d >= start]:
        ultimo_dia = hoy

        # Dia de pausa por 2 consecutivas
        if en_pausa:
            consec_losses = 0
            en_pausa      = False
            continue

        if rng.random() < 0.02:
            continue

        df_c = df[[ts.astimezone(ET).date() <= hoy for ts in df.index]]
        if len(df_c) < 50:
            continue

        hay_noticia, _ = check_noticia(hoy)
        if hay_noticia:
            continue

        trade = signal_kz_base(df_c, hoy, capital)

        if trade:
            g        = ajustar_realismo(trade, rng)
            capital += g
            trades  += 1
            gan_por_dia[hoy] = gan_por_dia.get(hoy, 0.0) + g

            if g < 0:
                consec_losses += 1
                if consec_losses >= 2:
                    en_pausa = True
            else:
                consec_losses = 0

        peak = max(peak, capital)
        res  = _check_fin(capital, peak, gan_por_dia)
        if res:
            resultado_final = res
            break

    duracion  = (ultimo_dia - start).days
    pnl_final = round(capital - CAPITAL_0, 0)
    return resultado_final, duracion, trades, pnl_final


# ──────────────────────────────────────────────────────────────────────────────
# Variante 3: Volatility Filter (skip si swing_hoy > 1.5 * media últimos 10)
# ──────────────────────────────────────────────────────────────────────────────

def run_eval_vf(df, dias, start, rng):
    from filtro_noticias import check_noticia

    capital         = CAPITAL_0
    peak            = CAPITAL_0
    resultado_final = 'INCOMPLETA'
    trades          = 0
    ultimo_dia      = start
    gan_por_dia     = {}
    swing_hist      = []   # historial de swing_promedio por dia

    for hoy in [d for d in dias if d >= start]:
        ultimo_dia = hoy

        if rng.random() < 0.02:
            continue

        df_c = df[[ts.astimezone(ET).date() <= hoy for ts in df.index]]
        if len(df_c) < 50:
            continue

        hay_noticia, _ = check_noticia(hoy)
        if hay_noticia:
            continue

        trade, swing_hoy = signal_kz_volt(df_c, hoy, capital, swing_hist)

        # Actualizar historial (incluso si se filtra o no hay señal)
        if swing_hoy is not None:
            swing_hist.append(swing_hoy)
            if len(swing_hist) > 50:   # mantener historial razonable
                swing_hist = swing_hist[-50:]

        if trade:
            g        = ajustar_realismo(trade, rng)
            capital += g
            trades  += 1
            gan_por_dia[hoy] = gan_por_dia.get(hoy, 0.0) + g

        peak = max(peak, capital)
        res  = _check_fin(capital, peak, gan_por_dia)
        if res:
            resultado_final = res
            break

    duracion  = (ultimo_dia - start).days
    pnl_final = round(capital - CAPITAL_0, 0)
    return resultado_final, duracion, trades, pnl_final


# ══════════════════════════════════════════════════════════════════════════════
# RUNNER GENERICO
# ══════════════════════════════════════════════════════════════════════════════

def correr_variante(nombre, run_fn, df, dias_utiles, n_eval, paso):
    """Corre 100 evals x 5 seeds para una variante. Imprime detalle y retorna stats."""

    starts_base = [dias_utiles[min(i * paso, len(dias_utiles) - 1)]
                   for i in range(n_eval)]

    resultados_por_seed = {}

    for seed in SEEDS:
        print("=== %s — SEED %d ===" % (nombre, seed))

        rng_master = random.Random(seed)
        semillas   = [rng_master.randint(0, 999_999) for _ in range(n_eval)]

        res_seed = []
        for idx, (start, semilla) in enumerate(zip(starts_base, semillas)):
            rng = random.Random(semilla)
            rf, dur, tr, pnl = run_fn(df, dias_utiles, start, rng)

            if rf in ('INVALIDA_DIAS', 'INVALIDA_CONSIST'):
                label = 'INVALIDA  '
            elif rf == 'PASADA':
                label = 'PASADA    '
            elif rf == 'EXPLOTADA':
                label = 'EXPLOTADA '
            else:
                label = 'INCOMPLETA'

            print("  [%3d] %s -> %s | %3dd | %2dt | $%+.0f" % (
                idx + 1, start, label, dur, tr, pnl))

            res_seed.append({'resultado': rf, 'duracion': dur, 'trades': tr, 'pnl': pnl})

        resultados_por_seed[seed] = res_seed
        print()

    # Calcular totales
    total_pas = 0
    total_inv = 0
    total_exp = 0
    total_inc = 0
    total_pnl = 0.0
    dias_sum  = 0.0
    dias_n    = 0

    for seed in SEEDS:
        res = resultados_por_seed[seed]
        pas = [r for r in res if r['resultado'] == 'PASADA']
        inv = [r for r in res if r['resultado'] in ('INVALIDA_DIAS', 'INVALIDA_CONSIST')]
        exp = [r for r in res if r['resultado'] == 'EXPLOTADA']
        inc = [r for r in res if r['resultado'] == 'INCOMPLETA']

        total_pas += len(pas)
        total_inv += len(inv)
        total_exp += len(exp)
        total_inc += len(inc)
        total_pnl += sum(r['pnl'] for r in pas)
        if pas:
            dias_sum += np.mean([r['duracion'] for r in pas]) * len(pas)
            dias_n   += len(pas)

    total_n   = n_eval * len(SEEDS)
    dias_glob = dias_sum / dias_n if dias_n > 0 else 0.0
    gan_neta  = total_pnl * SPLIT - total_n * FEE
    roi       = gan_neta / (total_n * FEE) * 100 if total_n > 0 else 0.0

    return {
        'nombre':   nombre,
        'pas':      total_pas,
        'inv':      total_inv,
        'exp':      total_exp,
        'inc':      total_inc,
        'total_n':  total_n,
        'dias_glob': dias_glob,
        'roi':      roi,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 80)
    print("  ICT KILL ZONES — ANTI-EXPLOSION — 500 sims x 4 variantes = 2000 sims")
    print("=" * 80)

    # ── Descargar datos 1h ────────────────────────────────────────────────────
    print("Descargando MES=F 1h (~730 dias)...")
    df = yf.download('MES=F', period='730d', interval='1h',
                     progress=False, auto_adjust=True)

    if df.empty:
        raise SystemExit("Error: no se pudo descargar MES=F 1h")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    if df.index.tzinfo is None:
        df.index = df.index.tz_localize('UTC').tz_convert(ET)
    else:
        df.index = df.index.tz_convert(ET)

    # ── Dias de referencia ────────────────────────────────────────────────────
    dias = sorted({
        ts.astimezone(ET).date()
        for ts in df.index
        if ts.astimezone(ET).weekday() < 5
    })

    print("Datos: %s -> %s (%d dias de trading)" % (dias[0], dias[-1], len(dias)))

    dias_utiles = dias[WARMUP:]
    n_eval = min(N_CUENTAS, len(dias_utiles))
    paso   = max(1, (len(dias_utiles) - 1) // (n_eval - 1)) if n_eval > 1 else 1

    print("Warmup: %d dias | Espaciado: ~%d dia(s) entre cuentas" % (WARMUP, paso))
    print()

    # ── Variantes a correr (BASE ya tiene resultados conocidos) ───────────────
    variantes = [
        ("+ Circuit Breaker", run_eval_cb),
        ("+ Consec. Losses",  run_eval_cl),
        ("+ Volt. Filter",    run_eval_vf),
    ]

    stats_variantes = []

    for nombre, run_fn in variantes:
        print()
        print("=" * 80)
        print("  VARIANTE: %s" % nombre)
        print("=" * 80)
        stats = correr_variante(nombre, run_fn, df, dias_utiles, n_eval, paso)
        stats_variantes.append(stats)

    # ── Tabla final comparativa ───────────────────────────────────────────────
    total_n = n_eval * len(SEEDS)

    print()
    print("=" * 80)
    print("  ICT KILL ZONES — ANTI-EXPLOSION — 500 sims x 4 variantes = 2000 sims")
    print("=" * 80)
    print("  %-22s  %-10s %-10s %-11s %-10s %-10s %s" % (
        "Variante", "Pasadas", "Invalidas", "Explotadas", "Incomplet", "Dias prom", "ROI"))
    print("  " + "-" * 78)

    # Fila BASE (hardcodeada)
    base = BASE_TOTALES
    print("  %-22s  %3d/%-6d %3d/%-6d %3d/%-7d %3d/%-6d %9.1f  %6.1f%%" % (
        "Base",
        base['pas'],  total_n,
        base['inv'],  total_n,
        base['exp'],  total_n,
        base['inc'],  total_n,
        base['dias_glob'],
        base['roi'],
    ))

    for s in stats_variantes:
        print("  %-22s  %3d/%-6d %3d/%-6d %3d/%-7d %3d/%-6d %9.1f  %6.1f%%" % (
            s['nombre'],
            s['pas'],  s['total_n'],
            s['inv'],  s['total_n'],
            s['exp'],  s['total_n'],
            s['inc'],  s['total_n'],
            s['dias_glob'],
            s['roi'],
        ))

    print("=" * 80)

    # ── Mejor por criterio ────────────────────────────────────────────────────
    todas = [
        {'nombre': 'Base', 'exp': base['exp'], 'roi': base['roi']},
    ] + [{'nombre': s['nombre'], 'exp': s['exp'], 'roi': s['roi']} for s in stats_variantes]

    mejor_antiblowup = min(todas, key=lambda x: x['exp'])
    mejor_roi        = max(todas, key=lambda x: x['roi'])

    print()
    print("  Mejor reduccion de explosiones: %s (%d/%d)" % (
        mejor_antiblowup['nombre'], mejor_antiblowup['exp'], total_n))
    print("  Mejor ROI neto:                 %s (%.1f%%)" % (
        mejor_roi['nombre'], mejor_roi['roi']))
    print("=" * 80)


if __name__ == '__main__':
    main()
