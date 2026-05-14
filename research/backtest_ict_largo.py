"""
Backtest ICT Kill Zones — 2 años — 1h
======================================
500 simulaciones: 100 evals x 5 seeds.
Intervalo 1h (max ~730d en yfinance).
Kill zone adaptada: 7am-12pm ET (5 velas 1h), swing lookback 3 barras.
Regla de consistencia LucidFlex aplicada en cada eval.
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


# ══════════════════════════════════════════════════════════════════════════════
# UTILIDADES
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
# ESTRATEGIA — ICT Kill Zones adaptada a 1h
# ══════════════════════════════════════════════════════════════════════════════

def signal_kz(df_completo, fecha_hoy, capital):
    """
    NY Open Kill Zone: 7am-12pm ET en barras 1h.
    Swing lookback: 3 barras.
    Confirmacion: cierre > swing_high y > VWAP (LONG), o < swing_low y < VWAP (SHORT).
    """
    # 1. Filtro ADX
    adx_ayer = calcular_adx_ayer(df_completo)
    if adx_ayer > 0 and adx_ayer < ADX_MIN:
        return None

    # 2. VWAP
    vwap_vals = calcular_vwap(df_completo)
    closes    = df_completo['Close'].values
    highs     = df_completo['High'].values
    lows      = df_completo['Low'].values
    fechas    = df_completo.index
    n_total   = len(fechas)

    # 3. Indices en la kill zone: 7am-12pm ET del dia hoy
    indices_kz = []
    for i, ts in enumerate(fechas):
        t = ts.astimezone(ET)
        if t.date() != fecha_hoy:
            continue
        if t.hour < KZ_HORA_INICIO or t.hour >= KZ_HORA_FIN:
            continue
        indices_kz.append(i)

    # 4. Necesitamos al menos KZ_SWING_BARS+1 barras en la kill zone
    if len(indices_kz) < KZ_SWING_BARS + 1:
        return None

    # 5. Indices del dia completo desde KZ_HORA_INICIO hasta 4:30pm ET (para simular salida)
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

    # 6. Buscar señal
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
# MOTOR DE BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

def run_eval(df, dias, start, rng):
    from filtro_noticias import check_noticia

    capital         = CAPITAL_0
    peak            = CAPITAL_0
    resultado_final = 'INCOMPLETA'
    trades          = 0
    ultimo_dia      = start
    gan_por_dia     = {}

    for hoy in [d for d in dias if d >= start]:
        ultimo_dia = hoy

        # Skip aleatorio 2% de los dias (jornadas sin operar)
        if rng.random() < 0.02:
            continue

        # df sin lookahead: solo datos hasta hoy inclusive
        df_c = df[[ts.astimezone(ET).date() <= hoy for ts in df.index]]
        if len(df_c) < 50:
            continue

        # Filtro de noticias
        hay_noticia, _ = check_noticia(hoy)
        if hay_noticia:
            continue

        trade = signal_kz(df_c, hoy, capital)

        if trade:
            g        = ajustar_realismo(trade, rng)
            capital += g
            trades  += 1
            gan_por_dia[hoy] = gan_por_dia.get(hoy, 0.0) + g

        peak   = max(peak, capital)
        limite = MLL_FIJO if peak >= ITB else peak - DRAWDOWN
        pnl    = capital - CAPITAL_0

        if pnl >= TARGET:
            # ── Reglas LucidFlex ─────────────────────────────────────────────
            dias_op   = len(gan_por_dia)
            mejor_dia = max(gan_por_dia.values()) if gan_por_dia else 0.0
            min_dias_ok  = dias_op >= 2
            consist_ok   = pnl > 0 and mejor_dia / pnl <= 0.50

            if min_dias_ok and consist_ok:
                resultado_final = 'PASADA'
            elif not min_dias_ok:
                resultado_final = 'INVALIDA_DIAS'
            else:
                resultado_final = 'INVALIDA_CONSIST'
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
    print("=" * 60)
    print("  ICT KILL ZONES (1h) — BACKTEST LARGO — 2 AÑOS")
    print("  MES=F | 100 evals x 5 seeds = 500 simulaciones")
    print("=" * 60)

    # ── Descargar datos 1h ────────────────────────────────────────────────────
    print("Descargando MES=F 1h (~730 dias)...")
    df = yf.download('MES=F', period='730d', interval='1h',
                     progress=False, auto_adjust=True)

    if df.empty:
        raise SystemExit("Error: no se pudo descargar MES=F 1h")

    # Fix MultiIndex si existe
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)

    # Convertir index a ET
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
    if len(dias_utiles) < N_CUENTAS:
        print("Advertencia: solo %d dias utiles" % len(dias_utiles))

    n_eval = min(N_CUENTAS, len(dias_utiles))
    paso   = max(1, (len(dias_utiles) - 1) // (n_eval - 1)) if n_eval > 1 else 1

    print("Warmup: %d dias | Espaciado: ~%d dia(s) entre cuentas" % (WARMUP, paso))
    print()

    # Starts comunes para todas las seeds (mismas posiciones en el tiempo)
    starts_base = [dias_utiles[min(i * paso, len(dias_utiles) - 1)]
                   for i in range(n_eval)]

    # ── Correr simulaciones ────────────────────────────────────────────────────
    resultados_por_seed = {}

    for seed in SEEDS:
        print("=== SEED %d ===" % seed)
        print("Corriendo %d evals..." % n_eval)

        rng_master = random.Random(seed)
        semillas   = [rng_master.randint(0, 999_999) for _ in range(n_eval)]

        res_seed = []
        for idx, (start, semilla) in enumerate(zip(starts_base, semillas)):
            rng = random.Random(semilla)
            rf, dur, tr, pnl = run_eval(df, dias, start, rng)

            # Etiqueta simplificada para output
            if rf in ('INVALIDA_DIAS', 'INVALIDA_CONSIST'):
                label = 'INVALIDA   '
            elif rf == 'PASADA':
                label = 'PASADA     '
            elif rf == 'EXPLOTADA':
                label = 'EXPLOTADA  '
            else:
                label = 'INCOMPLETA '

            print("  [%3d] %s -> %s | %3d dias | %2d trades | $%+.0f" % (
                idx + 1, start, label, dur, tr, pnl))

            res_seed.append({
                'resultado': rf,
                'duracion':  dur,
                'trades':    tr,
                'pnl':       pnl,
            })

        resultados_por_seed[seed] = res_seed
        print()

    # ── Tabla resumen por seed ─────────────────────────────────────────────────
    print("=" * 60)
    print("  RESUMEN POR SEED")
    print("=" * 60)
    print("  %-5s  %-8s  %-9s  %-10s  %-9s  %-9s  %s" % (
        "Seed", "Pasadas", "Invalidas", "Explotadas", "Incomplet", "Dias prom", "ROI"))
    print("  " + "-" * 66)

    totales = {'pas': 0, 'inv': 0, 'exp': 0, 'inc': 0,
               'dias_sum': 0.0, 'dias_n': 0, 'pnl_pas': 0.0}

    for seed in SEEDS:
        res = resultados_por_seed[seed]
        pas = [r for r in res if r['resultado'] == 'PASADA']
        inv = [r for r in res if r['resultado'] in ('INVALIDA_DIAS', 'INVALIDA_CONSIST')]
        exp = [r for r in res if r['resultado'] == 'EXPLOTADA']
        inc = [r for r in res if r['resultado'] == 'INCOMPLETA']

        dias_prom   = np.mean([r['duracion'] for r in pas]) if pas else 0.0
        pnl_pasadas = sum(r['pnl'] for r in pas)
        n           = n_eval
        gan_neta    = pnl_pasadas * SPLIT - n * FEE
        roi         = gan_neta / (n * FEE) * 100 if n > 0 else 0.0

        print("  %5d  %3d/%-4d  %3d/%-5d  %3d/%-6d  %3d/%-5d  %9.1f  %6.1f%%" % (
            seed,
            len(pas), n,
            len(inv), n,
            len(exp), n,
            len(inc), n,
            dias_prom,
            roi))

        totales['pas']      += len(pas)
        totales['inv']      += len(inv)
        totales['exp']      += len(exp)
        totales['inc']      += len(inc)
        totales['pnl_pas']  += pnl_pasadas
        if pas:
            totales['dias_sum'] += dias_prom * len(pas)
            totales['dias_n']   += len(pas)

    total_n   = n_eval * len(SEEDS)
    dias_glob = (totales['dias_sum'] / totales['dias_n']) if totales['dias_n'] > 0 else 0.0
    gan_glob  = totales['pnl_pas'] * SPLIT - total_n * FEE
    roi_glob  = gan_glob / (total_n * FEE) * 100 if total_n > 0 else 0.0

    print("  " + "-" * 66)
    print("  %-5s  %3d/%-4d  %3d/%-5d  %3d/%-6d  %3d/%-5d  %9.1f  %6.1f%%" % (
        "Total",
        totales['pas'],  total_n,
        totales['inv'],  total_n,
        totales['exp'],  total_n,
        totales['inc'],  total_n,
        dias_glob,
        roi_glob))
    print("=" * 60)

    # ── Conclusion ─────────────────────────────────────────────────────────────
    winrate     = totales['pas']  / total_n * 100
    inv_pct     = totales['inv']  / total_n * 100
    cobrable    = totales['pas']  / total_n * 100
    no_cobrable = (totales['inv'] + totales['exp'] + totales['inc']) / total_n * 100

    print()
    print("CONCLUSION:")
    print("  Win rate promedio:                          %5.1f%%" % winrate)
    print("  Cuentas invalidas (viola regla LucidFlex): %5.1f%%" % inv_pct)
    print("  Cuentas que realmente se pueden cobrar:    %5.1f%%" % cobrable)
    print("  ROI real (descontando invalidas y fees):   %5.1f%%" % roi_glob)
    print("=" * 60)


if __name__ == '__main__':
    main()
