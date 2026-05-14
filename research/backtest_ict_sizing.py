"""
Backtest ICT Kill Zones — Position Sizing — 200 sims
======================================================
3 variantes: BASE (1% fijo), DYNAMIC (riesgo dinámico tras pérdidas), HALF (0.5% fijo).
Seeds: 42, 123 | 100 evals cada seed = 200 sims por variante.
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
COSTO_CONTRATO = 4
MAX_CONTRATOS  = 20
N_CUENTAS      = 100
WARMUP         = 30
SEEDS_CORTOS   = [42, 123]

# ── Parametros Kill Zone adaptada a 1h ────────────────────────────────────────
KZ_HORA_INICIO = 7
KZ_HORA_FIN    = 12
KZ_SWING_BARS  = 3
KZ_SL_MULT     = 1.0
KZ_TP_MULT     = 2.0
ADX_MIN        = 20
FEE            = 75
SPLIT          = 0.90

# ── Tablas de riesgo por variante ─────────────────────────────────────────────
RIESGO_BASE    = 0.01
RIESGO_HALF    = 0.005
RIESGO_TABLA_DYN = [0.01, 0.005, 0.0025]   # 0, 1, 2+ pérdidas consecutivas


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

def signal_kz(df_completo, fecha_hoy, capital, riesgo_pct=RIESGO_BASE):
    """
    NY Open Kill Zone: 7am-12pm ET en barras 1h.
    riesgo_pct: porcentaje de riesgo a usar para el sizing inicial.
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

        contratos = min(max(1, int(capital * riesgo_pct / (riesgo_puntos * MULT))), MAX_CONTRATOS)

        resto = [j for j in indices_dia if j > i]
        resultado, precio_salida = _simular_salida(
            direccion, resto, highs, lows, closes, sl, tp, n_total
        )

        puntos   = (precio_salida - entrada) if direccion == 'LONG' else (entrada - precio_salida)
        ganancia = round(puntos * MULT * contratos - COSTO_CONTRATO * contratos, 2)

        return {
            'estrategia':    'KZ_1H',
            'direccion':     direccion,
            'entrada':       round(entrada, 2),
            'sl':            round(sl, 2),
            'tp':            round(tp, 2),
            'salida':        round(precio_salida, 2),
            'resultado':     resultado,
            'contratos':     contratos,
            'puntos':        round(puntos, 2),
            'ganancia':      ganancia,
            'riesgo_puntos': riesgo_puntos,
        }

    return None


# ══════════════════════════════════════════════════════════════════════════════
# MOTOR DE BACKTEST — con soporte para variantes de sizing
# ══════════════════════════════════════════════════════════════════════════════

def run_eval(df, dias, start, rng, variante='BASE'):
    from filtro_noticias import check_noticia

    capital         = CAPITAL_0
    peak            = CAPITAL_0
    resultado_final = 'INCOMPLETA'
    trades          = 0
    ultimo_dia      = start
    gan_por_dia     = {}
    consec_losses   = 0           # solo usado en DYNAMIC

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

        # ── Determinar riesgo_pct según variante ──────────────────────────────
        if variante == 'BASE':
            riesgo_pct = RIESGO_BASE
        elif variante == 'HALF':
            riesgo_pct = RIESGO_HALF
        else:  # DYNAMIC — el riesgo se calcula aquí; signal_kz recibirá 1% base
            # Se pasa 1% a signal_kz y luego se recalculan contratos con el riesgo real
            riesgo_pct = RIESGO_BASE

        trade = signal_kz(df_c, hoy, capital, riesgo_pct=riesgo_pct)

        if trade:
            # ── Recalcular contratos para DYNAMIC ────────────────────────────
            if variante == 'DYNAMIC':
                rp            = RIESGO_TABLA_DYN[min(consec_losses, 2)]
                riesgo_puntos = trade['riesgo_puntos']
                nuevos_contratos = min(
                    max(1, int(capital * rp / (riesgo_puntos * MULT))),
                    MAX_CONTRATOS
                )
                trade['contratos'] = nuevos_contratos
                puntos = trade['puntos']
                trade['ganancia'] = round(
                    puntos * MULT * nuevos_contratos - COSTO_CONTRATO * nuevos_contratos, 2
                )

            g       = ajustar_realismo(trade, rng)
            capital += g
            trades  += 1
            gan_por_dia[hoy] = gan_por_dia.get(hoy, 0.0) + g

            # ── Actualizar racha de pérdidas (DYNAMIC) ────────────────────────
            if variante == 'DYNAMIC':
                if g > 0:
                    consec_losses = 0
                else:
                    consec_losses += 1

        peak   = max(peak, capital)
        limite = MLL_FIJO if peak >= ITB else peak - DRAWDOWN
        pnl    = capital - CAPITAL_0

        if pnl >= TARGET:
            dias_op   = len(gan_por_dia)
            mejor_dia = max(gan_por_dia.values()) if gan_por_dia else 0.0
            min_dias_ok = dias_op >= 2
            consist_ok  = pnl > 0 and mejor_dia / pnl <= 0.50

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
    print("=" * 64)
    print("  ICT KZ — POSITION SIZING — 200 sims x 3 variantes")
    print("  Seeds: 42, 123 | 100 evals cada seed")
    print("=" * 64)

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

    dias = sorted({
        ts.astimezone(ET).date()
        for ts in df.index
        if ts.astimezone(ET).weekday() < 5
    })

    print("Datos: %s -> %s (%d dias de trading)" % (dias[0], dias[-1], len(dias)))

    dias_utiles = dias[WARMUP:]
    n_eval = min(N_CUENTAS, len(dias_utiles))
    paso   = max(1, (len(dias_utiles) - 1) // (n_eval - 1)) if n_eval > 1 else 1

    starts_base = [dias_utiles[min(i * paso, len(dias_utiles) - 1)]
                   for i in range(n_eval)]

    print("Warmup: %d dias | %d evals | Espaciado: ~%d dia(s)" % (WARMUP, n_eval, paso))
    print()

    VARIANTES = ['Base', 'Dynamic', 'Half']
    VARIANTE_MAP = {'Base': 'BASE', 'Dynamic': 'DYNAMIC', 'Half': 'HALF'}

    # Acumuladores por variante
    resultados_variante = {v: [] for v in VARIANTES}

    for variante in VARIANTES:
        print("─" * 64)
        print("  VARIANTE: %s" % variante)
        print("─" * 64)

        for seed in SEEDS_CORTOS:
            print("  Seed %d..." % seed)
            rng_master = random.Random(seed)
            semillas   = [rng_master.randint(0, 999_999) for _ in range(n_eval)]

            for idx, (start, semilla) in enumerate(zip(starts_base, semillas)):
                rng = random.Random(semilla)
                rf, dur, tr, pnl = run_eval(df, dias, start, rng, variante=VARIANTE_MAP[variante])
                resultados_variante[variante].append({
                    'resultado': rf,
                    'duracion':  dur,
                    'trades':    tr,
                    'pnl':       pnl,
                })

        total_v = len(resultados_variante[variante])
        pas_v   = [r for r in resultados_variante[variante] if r['resultado'] == 'PASADA']
        print("  -> %d/200 pasadas" % len(pas_v))
        print()

    # ── Tabla resumen ──────────────────────────────────────────────────────────
    total_n = n_eval * len(SEEDS_CORTOS)   # 200

    print("=" * 64)
    print("  ICT KZ — POSITION SIZING — 200 sims x 3 variantes")
    print("  Seeds: 42, 123 | 100 evals cada seed")
    print("=" * 64)
    print("  %-12s  %-10s  %-11s  %-10s  %-9s  %s" % (
        "Variante", "Pasadas", "Explotadas", "Incomplet", "Dias prom", "ROI"))
    print("  " + "-" * 60)

    mejores = {}

    for variante in VARIANTES:
        res = resultados_variante[variante]
        pas = [r for r in res if r['resultado'] == 'PASADA']
        exp = [r for r in res if r['resultado'] == 'EXPLOTADA']
        inc = [r for r in res if r['resultado'] == 'INCOMPLETA']

        dias_prom   = np.mean([r['duracion'] for r in pas]) if pas else 0.0
        pnl_pasadas = sum(r['pnl'] for r in pas)
        gan_neta    = pnl_pasadas * SPLIT - total_n * FEE
        roi         = gan_neta / (total_n * FEE) * 100 if total_n > 0 else 0.0

        label = variante if variante != 'Half' else 'Half (0.5%)'
        print("  %-12s  %3d/%-6d  %3d/%-7d  %3d/%-6d  %9.1f  %6.1f%%" % (
            label,
            len(pas), total_n,
            len(exp), total_n,
            len(inc), total_n,
            dias_prom,
            roi))

        mejores[variante] = {
            'explosiones': len(exp),
            'roi':         roi,
            'pas':         len(pas),
        }

    print("=" * 64)

    # ── Conclusiones ───────────────────────────────────────────────────────────
    mejor_exp = min(mejores, key=lambda v: mejores[v]['explosiones'])
    mejor_roi = max(mejores, key=lambda v: mejores[v]['roi'])

    print()
    print("  Mejor reduccion de explosiones: %s (%d explotadas)" % (
        mejor_exp,
        mejores[mejor_exp]['explosiones']
    ))
    print("  Mejor ROI: %s (%.1f%%)" % (
        mejor_roi,
        mejores[mejor_roi]['roi']
    ))
    print("=" * 64)


if __name__ == '__main__':
    main()
