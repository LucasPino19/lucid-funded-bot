"""
Backtest ICT Kill Zones — 4 Variantes (~60 dias)
=================================================
Compara la estrategia base ICT Kill Zones contra 3 variantes
que agregan filtros de riesgo:
  - Base:            Breakout swing H/L + VWAP
  - + Liquidity Sweep: Verifica barrida de stops antes del move
  - + HTF Trend:     Filtra por tendencia del dia anterior (EMA 20 diario)
  - + FVG Entry:     Entrada en pullback a Fair Value Gap

20 evals, misma ventana temporal (~60 dias), seed=42.

Uso:
    python3 backtest_ict_variantes.py          # seed 42
    python3 backtest_ict_variantes.py 99       # seed custom
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

# ── Parametros del backtest ────────────────────────────────────────────────────
N_CUENTAS = 20
WARMUP    = 20
SEED      = int(sys.argv[1]) if len(sys.argv) > 1 else 42

# ── Parametros Kill Zone ───────────────────────────────────────────────────────
KZ_HORA_INICIO = 7
KZ_HORA_FIN    = 10
KZ_SWING_BARS  = 12
KZ_SL_MULT     = 1.0
KZ_TP_MULT     = 2.0
ADX_MIN        = 20


# ══════════════════════════════════════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════════════════════════════════════

def calcular_vwap(df):
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    df2 = df.copy()
    df2['_tp']   = tp
    df2['_tpv']  = tp * df2['Volume']
    df2['_date'] = df2.index.normalize()
    df2['_ctpv'] = df2.groupby('_date')['_tpv'].cumsum()
    df2['_cvol'] = df2.groupby('_date')['Volume'].cumsum()
    return (df2['_ctpv'] / df2['_cvol']).values


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


def _simular_salida(direccion, indices_resto, highs, lows, closes, sl, tp, n_total):
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
    ganancia = puntos * MULT * contratos - COSTO_CONTRATO * contratos
    return round(puntos, 2), round(ganancia, 2)


def _indices_dia(fechas, fecha_hoy, hora_ini=None, hora_fin_h=16, hora_fin_m=30):
    """Devuelve indices de fechas que caen en fecha_hoy dentro de la ventana."""
    out = []
    for i, ts in enumerate(fechas):
        t = ts.astimezone(ET)
        if t.date() != fecha_hoy:
            continue
        if hora_ini is not None and t.hour < hora_ini:
            continue
        if t.hour > hora_fin_h or (t.hour == hora_fin_h and t.minute >= hora_fin_m):
            continue
        out.append(i)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# VARIANTE BASE — ICT Kill Zones (breakout + VWAP)
# ══════════════════════════════════════════════════════════════════════════════

def signal_kz_base(df_completo, fecha_hoy, capital):
    """
    NY Open Kill Zone: 7am-10am ET en 5m.
    Detecta ruptura de swing high/low con confirmacion VWAP.
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

    # Indices en la kill zone 7am-10am ET
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

    # Indices del dia completo hasta 4:30pm para simular salida
    indices_dia_completo = _indices_dia(fechas, fecha_hoy, hora_ini=KZ_HORA_INICIO)

    for i in indices_kz:
        if i < KZ_SWING_BARS:
            continue

        swing_high  = float(np.max(highs[i-KZ_SWING_BARS:i]))
        swing_low   = float(np.min(lows[i-KZ_SWING_BARS:i]))
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

        resto = [j for j in indices_dia_completo if j > i]
        resultado, precio_salida = _simular_salida(
            direccion, resto, highs, lows, closes, sl, tp, n_total
        )
        puntos, ganancia = _calcular_trade(
            entrada, sl, direccion, resultado, precio_salida, contratos
        )

        return {
            'estrategia': 'KZ_BASE',
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
# VARIANTE 1 — + Liquidity Sweep
# ══════════════════════════════════════════════════════════════════════════════

def signal_kz_sweep(df_completo, fecha_hoy, capital):
    """
    Igual que BASE pero verifica que hubo un liquidity sweep del lado contrario
    en las 30 barras previas antes de confirmar la senal.
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

    indices_dia_completo = _indices_dia(fechas, fecha_hoy, hora_ini=KZ_HORA_INICIO)

    SWEEP_LOOKBACK = 30

    for i in indices_kz:
        if i < KZ_SWING_BARS:
            continue

        swing_high  = float(np.max(highs[i-KZ_SWING_BARS:i]))
        swing_low   = float(np.min(lows[i-KZ_SWING_BARS:i]))
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

        # Verificar liquidity sweep en las 30 barras previas
        sweep_start = max(0, i - SWEEP_LOOKBACK)
        sweep_range = range(sweep_start, i)

        if direccion == 'LONG':
            # Para señal LONG: verificar que alguna barra barrió el swing_low (stops de longs)
            hubo_sweep = any(lows[j] <= swing_low for j in sweep_range)
        else:
            # Para señal SHORT: verificar que alguna barra barrió el swing_high (stops de shorts)
            hubo_sweep = any(highs[j] >= swing_high for j in sweep_range)

        if not hubo_sweep:
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

        resto = [j for j in indices_dia_completo if j > i]
        resultado, precio_salida = _simular_salida(
            direccion, resto, highs, lows, closes, sl, tp, n_total
        )
        puntos, ganancia = _calcular_trade(
            entrada, sl, direccion, resultado, precio_salida, contratos
        )

        return {
            'estrategia': 'KZ_SWEEP',
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
# VARIANTE 2 — + HTF Trend (EMA 20 diario)
# ══════════════════════════════════════════════════════════════════════════════

def signal_kz_htf(df_completo, fecha_hoy, capital):
    """
    Igual que BASE pero filtra por tendencia del dia anterior:
    cierre_ayer > EMA20_ayer => solo LONG
    cierre_ayer < EMA20_ayer => solo SHORT
    """
    adx_ayer = calcular_adx_ayer(df_completo)
    if adx_ayer > 0 and adx_ayer < ADX_MIN:
        return None

    # Calcular EMA(20) sobre cierres diarios
    filtro_htf = None  # None = sin filtro (fallback)
    try:
        df_d = df_completo[['Close']].resample('D').agg({'Close': 'last'}).dropna()
        # Solo dias estrictamente anteriores a fecha_hoy
        df_d_ant = df_d[df_d.index.date < fecha_hoy]
        if len(df_d_ant) >= 21:
            cierres_d = df_d_ant['Close'].values
            # EMA 20 manual
            ema = np.zeros(len(cierres_d))
            k = 2.0 / (20 + 1)
            ema[0] = cierres_d[0]
            for idx in range(1, len(cierres_d)):
                ema[idx] = cierres_d[idx] * k + ema[idx-1] * (1 - k)
            close_ayer = cierres_d[-1]
            ema_ayer   = ema[-1]
            if close_ayer > ema_ayer:
                filtro_htf = 'LONG'
            elif close_ayer < ema_ayer:
                filtro_htf = 'SHORT'
    except Exception:
        filtro_htf = None  # fallback: sin filtro

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

    indices_dia_completo = _indices_dia(fechas, fecha_hoy, hora_ini=KZ_HORA_INICIO)

    for i in indices_kz:
        if i < KZ_SWING_BARS:
            continue

        swing_high  = float(np.max(highs[i-KZ_SWING_BARS:i]))
        swing_low   = float(np.min(lows[i-KZ_SWING_BARS:i]))
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

        # Aplicar filtro HTF si esta disponible
        if filtro_htf is not None and direccion != filtro_htf:
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

        resto = [j for j in indices_dia_completo if j > i]
        resultado, precio_salida = _simular_salida(
            direccion, resto, highs, lows, closes, sl, tp, n_total
        )
        puntos, ganancia = _calcular_trade(
            entrada, sl, direccion, resultado, precio_salida, contratos
        )

        return {
            'estrategia': 'KZ_HTF',
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
# VARIANTE 3 — + FVG Entry (pullback a Fair Value Gap)
# ══════════════════════════════════════════════════════════════════════════════

def signal_kz_fvg(df_completo, fecha_hoy, capital):
    """
    Detecta FVGs durante kill zone y entra en pullback hacia el FVG.
    Bullish FVG: lows[i] > highs[i-2] → zona de soporte
    Bearish FVG: highs[i] < lows[i-2] → zona de resistencia
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

    if len(indices_kz) < 3:
        return None

    # Indices del dia completo hasta 4:30pm para simular salida
    indices_dia_completo = _indices_dia(fechas, fecha_hoy, hora_ini=KZ_HORA_INICIO)

    FVG_MIN_SIZE = 0.5

    for k, i_fvg in enumerate(indices_kz):
        # Necesitamos al menos 2 barras previas (i-2)
        if i_fvg < 2:
            continue

        vwap_i = vwap_vals[i_fvg]

        # Detectar FVG bullish: lows[i] > highs[i-2]
        if (lows[i_fvg] > highs[i_fvg - 2] and
                closes[i_fvg] > vwap_i):
            fvg_low  = highs[i_fvg - 2]
            fvg_high = lows[i_fvg]
            fvg_size = fvg_high - fvg_low
            if fvg_size < FVG_MIN_SIZE:
                continue
            tipo_fvg  = 'BULL'

        # Detectar FVG bearish: highs[i] < lows[i-2]
        elif (highs[i_fvg] < lows[i_fvg - 2] and
              closes[i_fvg] < vwap_i):
            fvg_low  = highs[i_fvg]
            fvg_high = lows[i_fvg - 2]
            fvg_size = fvg_high - fvg_low
            if fvg_size < FVG_MIN_SIZE:
                continue
            tipo_fvg  = 'BEAR'

        else:
            continue

        # Buscar pullback hacia el FVG en barras siguientes (hasta 4:30pm)
        barras_siguientes = [j for j in indices_dia_completo if j > i_fvg]

        for j in barras_siguientes:
            if tipo_fvg == 'BULL':
                # Precio toca el techo del FVG (zona de soporte)
                if lows[j] <= fvg_high:
                    entrada = fvg_high
                    sl      = fvg_low - fvg_size * 0.5
                    tp      = entrada + fvg_size * 3.0
                    direccion = 'LONG'
                else:
                    continue
            else:  # BEAR
                # Precio toca el piso del FVG (zona de resistencia)
                if highs[j] >= fvg_low:
                    entrada = fvg_low
                    sl      = fvg_high + fvg_size * 0.5
                    tp      = entrada - fvg_size * 3.0
                    direccion = 'SHORT'
                else:
                    continue

            riesgo_puntos = abs(entrada - sl)
            if riesgo_puntos <= 0:
                continue
            if riesgo_puntos * MULT > capital * RIESGO_PCT:
                continue

            contratos = min(max(1, int(capital * RIESGO_PCT / (riesgo_puntos * MULT))), MAX_CONTRATOS)

            # Simular salida desde la barra siguiente al pullback
            resto = [m for m in indices_dia_completo if m > j]
            resultado, precio_salida = _simular_salida(
                direccion, resto, highs, lows, closes, sl, tp, n_total
            )
            puntos, ganancia = _calcular_trade(
                entrada, sl, direccion, resultado, precio_salida, contratos
            )

            return {
                'estrategia': 'KZ_FVG',
                'direccion':  direccion,
                'entrada':    round(entrada, 2),
                'sl':         round(sl, 2),
                'tp':         round(tp, 2),
                'salida':     round(precio_salida, 2),
                'resultado':  resultado,
                'contratos':  contratos,
                'puntos':     puntos,
                'ganancia':   ganancia,
                'fvg_tipo':   tipo_fvg,
                'fvg_size':   round(fvg_size, 2),
            }

    return None


# ══════════════════════════════════════════════════════════════════════════════
# MOTOR DE BACKTEST GENERICO
# ══════════════════════════════════════════════════════════════════════════════

def run_eval(estrategia_fn, df, dias, start, rng):
    from filtro_noticias import check_noticia

    capital         = CAPITAL_0
    peak            = CAPITAL_0
    resultado_final = 'INCOMPLETA'
    trades          = 0
    ultimo_dia      = start
    dias_activos    = [d for d in dias if d >= start]
    gan_por_dia     = {}   # date -> ganancia acumulada del dia

    for hoy in dias_activos:
        ultimo_dia = hoy

        if rng.random() < 0.02:
            continue

        df_c = df[[ts.astimezone(ET).date() <= hoy for ts in df.index]]
        if len(df_c) < 50:
            continue

        hay_noticia, _ = check_noticia(hoy)
        if hay_noticia:
            continue

        trade = estrategia_fn(df_c, hoy, capital)

        if trade:
            ganancia_real             = ajustar_realismo(trade, rng)
            capital                  += ganancia_real
            trades                   += 1
            gan_por_dia[hoy]          = gan_por_dia.get(hoy, 0.0) + ganancia_real

        peak   = max(peak, capital)
        limite = MLL_FIJO if peak >= ITB else peak - DRAWDOWN

        pnl = capital - CAPITAL_0
        if pnl >= TARGET:
            # ── Reglas LucidFlex antes de marcar PASADA ──────────────────────
            dias_op       = len(gan_por_dia)
            mejor_dia_val = max(gan_por_dia.values()) if gan_por_dia else 0.0
            min_dias_ok   = dias_op >= 2
            consist_ok    = pnl > 0 and mejor_dia_val / pnl <= 0.50

            if min_dias_ok and consist_ok:
                resultado_final = 'PASADA'
            elif not min_dias_ok:
                resultado_final = 'INVALIDA_DIAS'      # menos de 2 dias operados
            else:
                resultado_final = 'INVALIDA_CONSIST'   # un dia > 50% del profit
            break
        if capital <= limite:
            resultado_final = 'EXPLOTADA'
            break

    duracion      = (ultimo_dia - start).days
    pnl_final     = round(capital - CAPITAL_0, 0)
    mejor_dia_pct = 0.0
    if gan_por_dia and capital - CAPITAL_0 > 0:
        mejor_dia_pct = max(gan_por_dia.values()) / (capital - CAPITAL_0) * 100
    return resultado_final, duracion, trades, pnl_final, round(mejor_dia_pct, 1)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 66)
    print("  ICT KILL ZONES — 4 VARIANTES — COMPARACION")
    print("  MES=F | ~60 DIAS | Seed: %d" % SEED)
    print("=" * 66)
    print()

    # ── Descargar datos ───────────────────────────────────────────────────────
    print("Descargando MES=F 5m (~59 dias)...")
    df_5m = yf.download('MES=F', period='59d', interval='5m',
                        progress=False, auto_adjust=True)
    if df_5m.empty:
        raise SystemExit("Error: no se pudo descargar MES=F 5m")
    if isinstance(df_5m.columns, pd.MultiIndex):
        df_5m.columns = df_5m.columns.droplevel(1)
    df_5m.index = (df_5m.index.tz_convert(ET) if df_5m.index.tzinfo
                   else df_5m.index.tz_localize('UTC').tz_convert(ET))

    # ── Dias de referencia ────────────────────────────────────────────────────
    dias = sorted({
        ts.astimezone(ET).date()
        for ts in df_5m.index
        if ts.astimezone(ET).weekday() < 5
    })
    print("Datos: %s -> %s (%d dias de trading)" % (dias[0], dias[-1], len(dias)))

    dias_utiles = dias[WARMUP:]
    if len(dias_utiles) < N_CUENTAS:
        n_eval = len(dias_utiles)
        print("Advertencia: solo %d dias utiles — ajustando a %d evals" % (
            len(dias_utiles), n_eval))
    else:
        n_eval = N_CUENTAS

    paso   = max(1, (len(dias_utiles) - 1) // (n_eval - 1)) if n_eval > 1 else 1
    starts = [dias_utiles[min(i * paso, len(dias_utiles) - 1)]
              for i in range(n_eval)]

    print("Warmup: %d dias | Espaciado: ~%d dia(s) entre cuentas" % (WARMUP, paso))
    print()

    rng_master = random.Random(SEED)
    semillas   = [rng_master.randint(0, 999_999) for _ in range(n_eval)]

    # ── Variantes a comparar ──────────────────────────────────────────────────
    variantes = [
        ('Base (breakout)',    signal_kz_base),
        ('+ Liquidity Sweep', signal_kz_sweep),
        ('+ HTF Trend',       signal_kz_htf),
        ('+ FVG Entry',       signal_kz_fvg),
    ]

    resultados_por_variante = {}

    for nombre, fn in variantes:
        print("Corriendo %d evals — %s..." % (n_eval, nombre))
        res = []
        for idx, (start, semilla) in enumerate(zip(starts, semillas)):
            rng = random.Random(semilla)
            rf, dur, trades, pnl, mejor_pct = run_eval(fn, df_5m, dias, start, rng)
            res.append((rf, dur, trades, pnl, mejor_pct))
            label = rf
            if rf in ('INVALIDA_DIAS', 'INVALIDA_CONSIST'):
                label = 'INVALIDA'
            print("  [%2d] %s -> %-10s | %3d dias | %2d trades | $%+.0f | mejor dia %4.0f%%" % (
                idx + 1, start, label, dur, trades, pnl, mejor_pct))
        resultados_por_variante[nombre] = res
        print()

    # ── Tabla final ───────────────────────────────────────────────────────────
    FEE   = 75
    SPLIT = 0.90
    fmt_n = "/%d" % n_eval

    print("=" * 78)
    print("  ICT KILL ZONES — REGLAS LUCID — %d EVALS — ~60 DIAS — Seed: %d" % (n_eval, SEED))
    print("=" * 78)
    print("  %-22s  %9s  %9s  %10s  %9s  %9s  %5s" % (
        "Variante", "Pasadas", "Invalidas", "Explotadas", "Incomplet", "Dias prom", "ROI"))
    print("  " + "-" * 74)

    for nombre, fn in variantes:
        res = resultados_por_variante[nombre]
        pas  = [r for r in res if r[0] == 'PASADA']
        inv  = [r for r in res if r[0] in ('INVALIDA_DIAS', 'INVALIDA_CONSIST')]
        exp  = [r for r in res if r[0] == 'EXPLOTADA']
        inc  = [r for r in res if r[0] == 'INCOMPLETA']

        dias_prom     = np.mean([r[1] for r in pas]) if pas else 0.0
        pnl_pasadas   = sum(r[3] for r in pas)
        ganancia_neta = pnl_pasadas * SPLIT - n_eval * FEE
        roi           = ganancia_neta / (n_eval * FEE) * 100

        print("  %-22s  %3d%-6s  %3d%-6s  %3d%-7s  %3d%-6s  %9.0f  %5.1f%%" % (
            nombre,
            len(pas), fmt_n,
            len(inv), fmt_n,
            len(exp), fmt_n,
            len(inc), fmt_n,
            dias_prom,
            roi))

    print("=" * 78)
    print()
    print("  Invalidas = target alcanzado pero viola regla de consistencia (>50% un dia)")
    print("             o menos de 2 dias operados. La cuenta no puede cobrarse.")
    print("  ROI calculado solo sobre evals PASADAS (cobrables).")
    print("=" * 78)
    print()


if __name__ == '__main__':
    main()
