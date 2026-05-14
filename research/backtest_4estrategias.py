"""
Backtest 4 Estrategias — MES=F (~60 dias)
==========================================
Compara ICT Kill Zones (5m), VWAP Bounce (15m), Gap Fill (1h), Pivot Points (1h).
20 evals por estrategia, misma ventana temporal.

Uso:
    python3 backtest_4estrategias.py          # seed 42
    python3 backtest_4estrategias.py 99       # seed custom
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


def calcular_atr(highs, lows, closes, end_idx, period=14):
    """ATR promedio de las ultimas `period` barras hasta end_idx."""
    start = max(1, end_idx - period + 1)
    if end_idx - start < 1:
        return 1.0
    trs = []
    for i in range(start, end_idx + 1):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i-1]),
                 abs(lows[i] - closes[i-1]))
        trs.append(tr)
    if not trs:
        return 1.0
    return float(np.mean(trs))


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
        # excluir barras >= 4:30pm
        if t.hour > hora_fin_h or (t.hour == hora_fin_h and t.minute >= hora_fin_m):
            continue
        out.append(i)
    return out


# ══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA 1 — ICT Kill Zones (5m)
# ══════════════════════════════════════════════════════════════════════════════

def signal_kill_zone(df_completo, fecha_hoy, capital):
    """
    NY Open Kill Zone: 7am-10am ET en 5m.
    Detecta ruptura de swing high/low con confirmacion VWAP.
    Retorna trade dict o None.
    """
    adx_ayer = calcular_adx_ayer(df_completo)
    # Si adx==0.0 es warmup insuficiente, no saltear
    if adx_ayer > 0 and adx_ayer < 20:
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
        if t.hour < 7 or t.hour >= 10:
            continue
        indices_kz.append(i)

    if len(indices_kz) < 13:  # necesitamos al menos 12 barras previas + 1 señal
        return None

    # Indices del dia completo hasta 4:30pm para simular salida
    indices_dia_completo = _indices_dia(fechas, fecha_hoy, hora_ini=7)

    for k, i in enumerate(indices_kz):
        if i < 12:
            continue

        swing_high = float(np.max(highs[i-12:i]))
        swing_low  = float(np.min(lows[i-12:i]))
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
            sl      = entrada - swing_range
            tp      = entrada + 2 * swing_range
        else:
            entrada = swing_low
            sl      = entrada + swing_range
            tp      = entrada - 2 * swing_range

        riesgo_puntos = abs(entrada - sl)
        if riesgo_puntos <= 0:
            continue
        contratos = min(max(1, int(capital * RIESGO_PCT / (riesgo_puntos * MULT))), MAX_CONTRATOS)

        # Barras restantes del dia (desde la siguiente barra hasta 4:30pm)
        resto = [j for j in indices_dia_completo if j > i]

        resultado, precio_salida = _simular_salida(
            direccion, resto, highs, lows, closes, sl, tp, n_total
        )
        puntos, ganancia = _calcular_trade(
            entrada, sl, direccion, resultado, precio_salida, contratos
        )

        return {
            'estrategia': 'KILL_ZONE',
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
# ESTRATEGIA 2 — VWAP Bounce (15m)
# ══════════════════════════════════════════════════════════════════════════════

def signal_vwap_bounce(df_completo, fecha_hoy, capital):
    """
    Toca VWAP y cierra al lado contrario en 15m. Primera senal del dia.
    """
    adx_ayer = calcular_adx_ayer(df_completo)
    if adx_ayer > 0 and adx_ayer < 20:
        return None

    vwap_vals = calcular_vwap(df_completo)
    closes    = df_completo['Close'].values
    highs     = df_completo['High'].values
    lows      = df_completo['Low'].values
    fechas    = df_completo.index
    n_total   = len(fechas)

    # Indice del primer bar de ayer (para calcular ATR hasta cierre de ayer)
    # Usamos todos los indices antes de hoy para ATR
    indices_hasta_ayer = [i for i, ts in enumerate(fechas)
                          if ts.astimezone(ET).date() < fecha_hoy]
    if not indices_hasta_ayer:
        return None
    idx_ayer_ultimo = indices_hasta_ayer[-1]

    atr = calcular_atr(highs, lows, closes, idx_ayer_ultimo, period=14)

    # Indices de hoy en ventana 9am-4:30pm
    indices_hoy = _indices_dia(fechas, fecha_hoy, hora_ini=9)
    if len(indices_hoy) < 2:
        return None

    for k, i in enumerate(indices_hoy):
        vwap_i  = vwap_vals[i]
        high_i  = highs[i]
        low_i   = lows[i]
        close_i = closes[i]

        direccion = None
        if low_i <= vwap_i and close_i > vwap_i:
            direccion = 'LONG'
        elif high_i >= vwap_i and close_i < vwap_i:
            direccion = 'SHORT'

        if direccion is None:
            continue

        entrada  = close_i
        sl_dist  = max(atr, 0.5)

        if direccion == 'LONG':
            sl = entrada - sl_dist
            tp = entrada + 2 * sl_dist
        else:
            sl = entrada + sl_dist
            tp = entrada - 2 * sl_dist

        contratos = min(max(1, int(capital * RIESGO_PCT / (sl_dist * MULT))), MAX_CONTRATOS)

        resto = [j for j in indices_hoy if j > i]
        resultado, precio_salida = _simular_salida(
            direccion, resto, highs, lows, closes, sl, tp, n_total
        )
        puntos, ganancia = _calcular_trade(
            entrada, sl, direccion, resultado, precio_salida, contratos
        )

        return {
            'estrategia': 'VWAP_BOUNCE',
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
# ESTRATEGIA 3 — Gap Fill (1h)
# ══════════════════════════════════════════════════════════════════════════════

def signal_gap_fill(df_completo, fecha_hoy, capital):
    """
    Gap nocturno significativo (2-20 pts). Opera hacia el cierre de ayer.
    Sin filtro ADX — gaps funcionan en mercados laterales tambien.
    """
    closes  = df_completo['Close'].values
    highs   = df_completo['High'].values
    lows    = df_completo['Low'].values
    opens   = df_completo['Open'].values
    fechas  = df_completo.index
    n_total = len(fechas)

    # Ultima barra de ayer con hora < 4:30pm ET
    close_ayer = None
    for i in range(len(fechas) - 1, -1, -1):
        t = fechas[i].astimezone(ET)
        if t.date() >= fecha_hoy:
            continue
        if t.hour > 16 or (t.hour == 16 and t.minute >= 30):
            continue
        close_ayer = closes[i]
        break

    if close_ayer is None:
        return None

    # Primera barra de hoy con hora >= 9am
    open_hoy   = None
    idx_open   = None
    for i, ts in enumerate(fechas):
        t = ts.astimezone(ET)
        if t.date() == fecha_hoy and t.hour >= 9:
            open_hoy = opens[i]
            idx_open = i
            break

    if open_hoy is None or idx_open is None:
        return None

    gap = open_hoy - close_ayer
    if abs(gap) < 2.0 or abs(gap) > 20.0:
        return None

    if gap > 0:
        direccion = 'SHORT'
        entrada   = open_hoy
        tp        = close_ayer
        sl        = open_hoy + abs(gap)
    else:
        direccion = 'LONG'
        entrada   = open_hoy
        tp        = close_ayer
        sl        = open_hoy - abs(gap)

    riesgo_puntos = abs(entrada - sl)
    if riesgo_puntos <= 0:
        return None
    contratos = min(max(1, int(capital * RIESGO_PCT / (riesgo_puntos * MULT))), MAX_CONTRATOS)

    # Barras del dia a partir de la siguiente a open
    indices_hoy = _indices_dia(fechas, fecha_hoy, hora_ini=9)
    resto = [j for j in indices_hoy if j > idx_open]

    resultado, precio_salida = _simular_salida(
        direccion, resto, highs, lows, closes, sl, tp, n_total
    )
    puntos, ganancia = _calcular_trade(
        entrada, sl, direccion, resultado, precio_salida, contratos
    )

    return {
        'estrategia': 'GAP_FILL',
        'direccion':  direccion,
        'entrada':    round(entrada, 2),
        'sl':         round(sl, 2),
        'tp':         round(tp, 2),
        'salida':     round(precio_salida, 2),
        'resultado':  resultado,
        'contratos':  contratos,
        'puntos':     puntos,
        'ganancia':   ganancia,
        'gap':        round(gap, 2),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA 4 — Pivot Points (1h)
# ══════════════════════════════════════════════════════════════════════════════

def signal_pivot(df_completo, fecha_hoy, capital):
    """
    Pivotes diarios clasicos (PP, R1, S1). Entrada en rebote con VWAP.
    Filtro ADX > 20.
    """
    adx_ayer = calcular_adx_ayer(df_completo)
    if adx_ayer > 0 and adx_ayer < 20:
        return None

    # Calcular pivotes del dia anterior
    df_d = df_completo[['High', 'Low', 'Close']].resample('D').agg(
        {'High': 'max', 'Low': 'min', 'Close': 'last'}
    ).dropna()

    # Filtrar solo dias estrictamente anteriores a fecha_hoy
    dias_ant = [d for d in df_d.index if d.date() < fecha_hoy]
    if not dias_ant:
        return None

    prev = df_d.loc[dias_ant[-1]]
    prev_H = float(prev['High'])
    prev_L = float(prev['Low'])
    prev_C = float(prev['Close'])

    pp = (prev_H + prev_L + prev_C) / 3
    r1 = 2 * pp - prev_L
    s1 = 2 * pp - prev_H

    vwap_vals = calcular_vwap(df_completo)
    closes    = df_completo['Close'].values
    highs     = df_completo['High'].values
    lows      = df_completo['Low'].values
    fechas    = df_completo.index
    n_total   = len(fechas)

    indices_hoy = _indices_dia(fechas, fecha_hoy, hora_ini=9)
    if len(indices_hoy) < 2:
        return None

    for k, i in enumerate(indices_hoy):
        high_i  = highs[i]
        low_i   = lows[i]
        close_i = closes[i]
        vwap_i  = vwap_vals[i]

        direccion = None
        entrada   = None
        tp        = None
        sl        = None

        # Rebote en S1 -> LONG hacia PP
        if low_i <= s1 and close_i > s1 and close_i > vwap_i:
            entrada   = s1 + 0.25
            tp        = pp
            sl        = s1 - (pp - s1) * 0.5
            direccion = 'LONG'

        # Rechazo en R1 -> SHORT hacia PP
        elif high_i >= r1 and close_i < r1 and close_i < vwap_i:
            entrada   = r1 - 0.25
            tp        = pp
            sl        = r1 + (r1 - pp) * 0.5
            direccion = 'SHORT'

        if direccion is None:
            continue

        # Verificar que TP tenga sentido
        if direccion == 'LONG' and tp <= entrada:
            continue
        if direccion == 'SHORT' and tp >= entrada:
            continue

        riesgo_puntos = abs(entrada - sl)
        if riesgo_puntos <= 0:
            continue
        contratos = min(max(1, int(capital * RIESGO_PCT / (riesgo_puntos * MULT))), MAX_CONTRATOS)

        resto = [j for j in indices_hoy if j > i]
        resultado, precio_salida = _simular_salida(
            direccion, resto, highs, lows, closes, sl, tp, n_total
        )
        puntos, ganancia = _calcular_trade(
            entrada, sl, direccion, resultado, precio_salida, contratos
        )

        return {
            'estrategia': 'PIVOT',
            'direccion':  direccion,
            'entrada':    round(entrada, 2),
            'sl':         round(sl, 2),
            'tp':         round(tp, 2),
            'salida':     round(precio_salida, 2),
            'resultado':  resultado,
            'contratos':  contratos,
            'puntos':     puntos,
            'ganancia':   ganancia,
            'pp':         round(pp, 2),
            'r1':         round(r1, 2),
            's1':         round(s1, 2),
        }

    return None


# ══════════════════════════════════════════════════════════════════════════════
# MOTOR DE BACKTEST GENERICO
# ══════════════════════════════════════════════════════════════════════════════

def run_eval(estrategia, df, dias, start, rng):
    from filtro_noticias import check_noticia

    capital         = CAPITAL_0
    peak            = CAPITAL_0
    resultado_final = 'INCOMPLETA'
    trades          = 0
    ultimo_dia      = start
    dias_activos    = [d for d in dias if d >= start]

    for hoy in dias_activos:
        ultimo_dia = hoy

        if rng.random() < 0.02:  # falla de conexion 2%
            continue

        df_c = df[[ts.astimezone(ET).date() <= hoy for ts in df.index]]
        if len(df_c) < 50:
            continue

        hay_noticia, _ = check_noticia(hoy)
        if hay_noticia:
            continue

        trade = None
        if estrategia == 'KILL_ZONE':
            trade = signal_kill_zone(df_c, hoy, capital)
        elif estrategia == 'VWAP_BOUNCE':
            trade = signal_vwap_bounce(df_c, hoy, capital)
        elif estrategia == 'GAP_FILL':
            trade = signal_gap_fill(df_c, hoy, capital)
        elif estrategia == 'PIVOT':
            trade = signal_pivot(df_c, hoy, capital)

        if trade:
            ganancia_real = ajustar_realismo(trade, rng)
            capital      += ganancia_real
            trades       += 1

        peak   = max(peak, capital)
        limite = MLL_FIJO if peak >= ITB else peak - DRAWDOWN

        if capital - CAPITAL_0 >= TARGET:
            resultado_final = 'PASADA'
            break
        if capital <= limite:
            resultado_final = 'EXPLOTADA'
            break

    duracion  = (ultimo_dia - start).days
    pnl_final = round(capital - CAPITAL_0, 0)
    return resultado_final, duracion, trades, pnl_final


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 64)
    print("  COMPARACION — 4 ESTRATEGIAS — %d EVALS — ~60 DIAS" % N_CUENTAS)
    print("  MES=F | Seed: %d" % SEED)
    print("=" * 64)
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

    print("Descargando MES=F 15m (~59 dias)...")
    df_15m = yf.download('MES=F', period='59d', interval='15m',
                         progress=False, auto_adjust=True)
    if df_15m.empty:
        raise SystemExit("Error: no se pudo descargar MES=F 15m")
    if isinstance(df_15m.columns, pd.MultiIndex):
        df_15m.columns = df_15m.columns.droplevel(1)
    df_15m.index = (df_15m.index.tz_convert(ET) if df_15m.index.tzinfo
                    else df_15m.index.tz_localize('UTC').tz_convert(ET))

    print("Descargando MES=F 1h (~59 dias)...")
    df_1h = yf.download('MES=F', period='59d', interval='1h',
                        progress=False, auto_adjust=True)
    if df_1h.empty:
        raise SystemExit("Error: no se pudo descargar MES=F 1h")
    if isinstance(df_1h.columns, pd.MultiIndex):
        df_1h.columns = df_1h.columns.droplevel(1)
    df_1h.index = (df_1h.index.tz_convert(ET) if df_1h.index.tzinfo
                   else df_1h.index.tz_localize('UTC').tz_convert(ET))

    # ── Dias de referencia (mas restrictivo = 15m) ────────────────────────────
    dias = sorted({
        ts.astimezone(ET).date()
        for ts in df_15m.index
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

    # ── Configuracion de cada estrategia ──────────────────────────────────────
    estrategias_cfg = [
        ('KILL_ZONE',   'ICT Kill Zones',  '5m',  df_5m),
        ('VWAP_BOUNCE', 'VWAP Bounce',     '15m', df_15m),
        ('GAP_FILL',    'Gap Fill',         '1h',  df_1h),
        ('PIVOT',       'Pivot Points',     '1h',  df_1h),
    ]

    resultados_por_estrategia = {}

    for codigo, nombre, tf, df_usado in estrategias_cfg:
        print("Corriendo %d evals — %s (%s)..." % (n_eval, nombre, tf))
        res = []
        for idx, (start, semilla) in enumerate(zip(starts, semillas)):
            rng = random.Random(semilla)
            rf, dur, trades, pnl = run_eval(codigo, df_usado, dias, start, rng)
            res.append((rf, dur, trades, pnl))
            print("  [%2d] %s -> %-10s | %3d dias | %2d trades | $%+.0f" % (
                idx + 1, start, rf, dur, trades, pnl))
        resultados_por_estrategia[codigo] = (nombre, tf, res)
        print()

    # ── Tabla comparativa ─────────────────────────────────────────────────────
    print("=" * 68)
    print("  COMPARACION — 4 ESTRATEGIAS — %d EVALS — ~60 DIAS" % n_eval)
    print("=" * 68)
    print("  %-20s  %3s  %7s  %10s  %11s  %9s  %5s" % (
        "Estrategia", "TF", "Pasadas", "Explotadas", "Incompletas", "Dias prom", "ROI"))
    print("  " + "-" * 64)

    fmt_n = "/%d" % n_eval
    mejor_roi    = -1e9
    mejor_nombre = ""

    for codigo, nombre, tf, df_usado in estrategias_cfg:
        nombre_disp, tf_disp, res = resultados_por_estrategia[codigo]
        pas  = [r for r in res if r[0] == 'PASADA']
        exp  = [r for r in res if r[0] == 'EXPLOTADA']
        inc  = [r for r in res if r[0] == 'INCOMPLETA']

        dias_prom = np.mean([r[1] for r in pas]) if pas else 0.0

        # ROI simple: PnL promedio de todas las evals / capital inicial * 100
        pnl_total = sum(r[3] for r in res)
        roi = (pnl_total / (CAPITAL_0 * n_eval)) * 100

        print("  %-20s  %3s  %3d%-4s  %6d%-4s  %7d%-4s  %9.0f  %5.1f%%" % (
            nombre_disp, tf_disp,
            len(pas), fmt_n,
            len(exp), fmt_n,
            len(inc), fmt_n,
            dias_prom,
            roi))

        if roi > mejor_roi:
            mejor_roi    = roi
            mejor_nombre = nombre_disp

    print("=" * 68)
    print()
    if mejor_roi > -1e9:
        print("  Estrategia con mayor ROI: %s (%.1f%%)" % (mejor_nombre, mejor_roi))
    print("=" * 68)


if __name__ == '__main__':
    main()
