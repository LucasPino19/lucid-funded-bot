"""
Backtest Comparacion — ORB+VWAP (1h) vs IFVG+OB (15m)
=======================================================
Misma ventana temporal (~60 dias, maximo que permite yfinance para 15m).
ORB usa velas de 1h (mejor timeframe para esa estrategia).
IFVG+OB usa velas de 15m (captura mejor los gaps y bloques).
~20 evals por estrategia (limite por ventana de 60 dias).

Uso:
    python3 backtest_comparacion.py          # seed 42
    python3 backtest_comparacion.py 99       # seed custom
"""

import sys
import random
import numpy as np
import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo

from config import PLANES, CUENTA, MULT
from estrategias import signal_orb, calcular_adx_ayer, _simular_salida, _calcular_trade
from filtro_noticias import check_noticia

ET        = ZoneInfo("America/New_York")
CAPITAL_0 = float(PLANES[CUENTA]['capital_inicial'])
TARGET    = PLANES[CUENTA]['profit_target']
DRAWDOWN  = PLANES[CUENTA]['max_drawdown']
ITB       = CAPITAL_0 + TARGET                      # 26250
MLL_FIJO  = CAPITAL_0 - PLANES[CUENTA]['mll_lock']  # 24900

N_CUENTAS = 20
WARMUP    = 20   # dias de warmup para ADX (necesita ~30 dias diarios; 60-20=40 utiles)

SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 42

# ── Parametros IFVG+OB ────────────────────────────────────────────────────────
# Con 15m: 1 hora = 4 velas. 400 velas ≈ 100h ≈ 2.5 semanas de lookback
IFVG_LOOKBACK    = 400
IFVG_STOP_MULT   = 1.5
IFVG_TARGET_MULT = 2.0
IFVG_OB_TOL      = 0.20
RIESGO_PCT       = 0.01
COSTO_CONTRATO   = 4
ORB_HORA_INICIO  = 9
VENTANA_H        = 13
VENTANA_M        = 30
CIERRE_HORA      = 16
CIERRE_MIN       = 30
ADX_MIN          = 20
MAX_CONTRATOS    = PLANES[CUENTA]['max_contratos']


# ══════════════════════════════════════════════════════════════════════════════
# IFVG — deteccion y seguimiento
# ══════════════════════════════════════════════════════════════════════════════

def detectar_fvgs(highs, lows, max_idx):
    fvg_bull = []
    fvg_bear = []
    for i in range(2, max_idx + 1):
        if lows[i] > highs[i - 2]:
            fvg_bull.append({
                'indice':    i,
                'zona_low':  float(highs[i - 2]),
                'zona_high': float(lows[i]),
            })
        if highs[i] < lows[i - 2]:
            fvg_bear.append({
                'indice':    i,
                'zona_low':  float(highs[i]),
                'zona_high': float(lows[i - 2]),
            })
    return fvg_bull, fvg_bear


def detectar_ifvgs(highs, lows, closes, max_idx, lookback=IFVG_LOOKBACK):
    """
    IFVG: FVG que fue llenado por el precio y actua en reversa como soporte/resistencia.
    Bullish IFVG llenado -> soporte -> entrada LONG.
    Bearish IFVG llenado -> resistencia -> entrada SHORT.
    """
    start = max(2, max_idx - lookback)
    fvg_bull, fvg_bear = detectar_fvgs(highs, lows, max_idx)

    ifvg_bull = []
    ifvg_bear = []

    for fvg in fvg_bull:
        i_fvg     = fvg['indice']
        zona_low  = fvg['zona_low']
        zona_high = fvg['zona_high']
        fvg_size  = zona_high - zona_low

        if i_fvg < start or fvg_size <= 0:
            continue

        llenado    = False
        invalidado = False
        for j in range(i_fvg + 1, max_idx + 1):
            if lows[j] <= zona_high:
                llenado = True
            if llenado and closes[j] < zona_low - fvg_size * 0.5:
                invalidado = True
                break

        if llenado and not invalidado:
            ifvg_bull.append({
                'zona_low':  zona_low,
                'zona_high': zona_high,
                'fvg_size':  fvg_size,
                'tipo':      'bull',
            })

    for fvg in fvg_bear:
        i_fvg     = fvg['indice']
        zona_low  = fvg['zona_low']
        zona_high = fvg['zona_high']
        fvg_size  = zona_high - zona_low

        if i_fvg < start or fvg_size <= 0:
            continue

        llenado    = False
        invalidado = False
        for j in range(i_fvg + 1, max_idx + 1):
            if highs[j] >= zona_low:
                llenado = True
            if llenado and closes[j] > zona_high + fvg_size * 0.5:
                invalidado = True
                break

        if llenado and not invalidado:
            ifvg_bear.append({
                'zona_low':  zona_low,
                'zona_high': zona_high,
                'fvg_size':  fvg_size,
                'tipo':      'bear',
            })

    return ifvg_bull, ifvg_bear


def detectar_obs_para_ifvg(highs, lows, opens, closes, max_idx, impulso_minimo=4):
    ob_bull = []
    ob_bear = []

    for i in range(1, max_idx - impulso_minimo):
        alc = sum(
            1 for k in range(i + 1, min(i + 1 + impulso_minimo, max_idx + 1))
            if closes[k] > opens[k] and closes[k] > closes[k - 1]
        )
        if closes[i] < opens[i] and alc >= impulso_minimo:
            ob_bull.append({
                'ob_high': float(max(opens[i], closes[i])),
                'ob_low':  float(lows[i]),
            })

        baj = sum(
            1 for k in range(i + 1, min(i + 1 + impulso_minimo, max_idx + 1))
            if closes[k] < opens[k] and closes[k] < closes[k - 1]
        )
        if closes[i] > opens[i] and baj >= impulso_minimo:
            ob_bear.append({
                'ob_high': float(highs[i]),
                'ob_low':  float(min(opens[i], closes[i])),
            })

    return ob_bull, ob_bear


def ob_coincide_con_ifvg(ob, ifvg, tolerancia=IFVG_OB_TOL):
    fvg_size = ifvg['fvg_size']
    margen   = fvg_size * tolerancia
    ob_mid   = (ob['ob_high'] + ob['ob_low']) / 2
    ifvg_mid = (ifvg['zona_high'] + ifvg['zona_low']) / 2
    return abs(ob_mid - ifvg_mid) <= (fvg_size / 2 + margen)


# ══════════════════════════════════════════════════════════════════════════════
# ESTRATEGIA IFVG + OB  (diseñada para 15m)
# ══════════════════════════════════════════════════════════════════════════════

def signal_ifvg_ob(df_completo, fecha_hoy, capital):
    adx_ayer = calcular_adx_ayer(df_completo)
    if adx_ayer < ADX_MIN and adx_ayer > 0:
        return None, 'ADX %.1f < %d — lateral, skip' % (adx_ayer, ADX_MIN)

    closes = df_completo['Close'].values
    highs  = df_completo['High'].values
    lows   = df_completo['Low'].values
    opens  = df_completo['Open'].values
    fechas = df_completo.index
    n      = len(fechas)

    indices_hoy = [i for i, ts in enumerate(fechas)
                   if ts.astimezone(ET).date() == fecha_hoy]

    if len(indices_hoy) < 2:
        return None, 'Pocos datos para hoy'

    indices_validos = [
        i for i in indices_hoy
        if (fechas[i].astimezone(ET).hour >= ORB_HORA_INICIO
            and not (fechas[i].astimezone(ET).hour > CIERRE_HORA
                     or (fechas[i].astimezone(ET).hour == CIERRE_HORA
                         and fechas[i].astimezone(ET).minute >= CIERRE_MIN)))
    ]

    if not indices_validos:
        return None, 'Sin velas en ventana de trading'

    idx_ayer = min(indices_hoy) - 1
    if idx_ayer < 10:
        return None, 'Insuficientes datos historicos'

    ifvg_bull, ifvg_bear = detectar_ifvgs(highs, lows, closes, idx_ayer,
                                           lookback=IFVG_LOOKBACK)
    ob_bull, ob_bear = detectar_obs_para_ifvg(highs, lows, opens, closes, idx_ayer)

    if not ifvg_bull and not ifvg_bear:
        return None, 'Sin IFVGs activos'

    for k, i in enumerate(indices_validos):
        hora_et = fechas[i].astimezone(ET)
        if hora_et.hour > VENTANA_H or (hora_et.hour == VENTANA_H
                                         and hora_et.minute >= VENTANA_M):
            break

        high_i = highs[i]
        low_i  = lows[i]

        for ifvg in ifvg_bull:
            if not (low_i <= ifvg['zona_high'] and high_i >= ifvg['zona_low']):
                continue
            ob_confirmado = any(ob_coincide_con_ifvg(ob, ifvg) for ob in ob_bull)
            if not ob_confirmado:
                continue

            fvg_size = ifvg['fvg_size']
            if fvg_size <= 0:
                continue

            entrada   = ifvg['zona_high']
            sl        = entrada - fvg_size * IFVG_STOP_MULT
            tp        = entrada + fvg_size * IFVG_TARGET_MULT
            direccion = 'LONG'

            riesgo_usd      = capital * RIESGO_PCT
            riesgo_puntos   = abs(entrada - sl)
            riesgo_contrato = riesgo_puntos * MULT
            if riesgo_contrato <= 0 or riesgo_contrato > riesgo_usd:
                continue
            contratos = min(max(1, int(riesgo_usd / riesgo_contrato)), MAX_CONTRATOS)

            resto = [
                j for j in indices_validos[k + 1:]
                if not (fechas[j].astimezone(ET).hour > CIERRE_HORA
                        or (fechas[j].astimezone(ET).hour == CIERRE_HORA
                            and fechas[j].astimezone(ET).minute >= CIERRE_MIN))
            ]
            resultado, precio_salida = _simular_salida(
                direccion, resto, highs, lows, closes, sl, tp, n
            )
            puntos, ganancia = _calcular_trade(
                capital, entrada, sl, direccion, resultado, precio_salida, contratos
            )

            return {
                'estrategia': 'IFVG_OB',
                'direccion':  direccion,
                'entrada':    round(entrada, 2),
                'sl':         round(sl, 2),
                'tp':         round(tp, 2),
                'salida':     round(precio_salida, 2),
                'resultado':  resultado,
                'contratos':  contratos,
                'puntos':     puntos,
                'ganancia':   ganancia,
                'fvg_size':   round(fvg_size, 2),
                'adx':        round(adx_ayer, 1),
            }, None

        for ifvg in ifvg_bear:
            if not (low_i <= ifvg['zona_high'] and high_i >= ifvg['zona_low']):
                continue
            ob_confirmado = any(ob_coincide_con_ifvg(ob, ifvg) for ob in ob_bear)
            if not ob_confirmado:
                continue

            fvg_size = ifvg['fvg_size']
            if fvg_size <= 0:
                continue

            entrada   = ifvg['zona_low']
            sl        = entrada + fvg_size * IFVG_STOP_MULT
            tp        = entrada - fvg_size * IFVG_TARGET_MULT
            direccion = 'SHORT'

            riesgo_usd      = capital * RIESGO_PCT
            riesgo_puntos   = abs(entrada - sl)
            riesgo_contrato = riesgo_puntos * MULT
            if riesgo_contrato <= 0 or riesgo_contrato > riesgo_usd:
                continue
            contratos = min(max(1, int(riesgo_usd / riesgo_contrato)), MAX_CONTRATOS)

            resto = [
                j for j in indices_validos[k + 1:]
                if not (fechas[j].astimezone(ET).hour > CIERRE_HORA
                        or (fechas[j].astimezone(ET).hour == CIERRE_HORA
                            and fechas[j].astimezone(ET).minute >= CIERRE_MIN))
            ]
            resultado, precio_salida = _simular_salida(
                direccion, resto, highs, lows, closes, sl, tp, n
            )
            puntos, ganancia = _calcular_trade(
                capital, entrada, sl, direccion, resultado, precio_salida, contratos
            )

            return {
                'estrategia': 'IFVG_OB',
                'direccion':  direccion,
                'entrada':    round(entrada, 2),
                'sl':         round(sl, 2),
                'tp':         round(tp, 2),
                'salida':     round(precio_salida, 2),
                'resultado':  resultado,
                'contratos':  contratos,
                'puntos':     puntos,
                'ganancia':   ganancia,
                'fvg_size':   round(fvg_size, 2),
                'adx':        round(adx_ayer, 1),
            }, None

    return None, 'Sin senal IFVG+OB hoy'


# ══════════════════════════════════════════════════════════════════════════════
# AJUSTE DE REALISMO
# ══════════════════════════════════════════════════════════════════════════════

def ajustar_realismo(trade, rng):
    contratos = trade['contratos']
    resultado = trade['resultado']
    ganancia  = trade['ganancia']
    ajuste    = 0.0

    if rng.random() < 0.30:
        slip_pts = rng.uniform(0.25, 0.75)
        ajuste  -= slip_pts * MULT * contratos

    if resultado == 'stop_loss':
        gap_pts = rng.uniform(0.0, 0.50)
        ajuste -= gap_pts * MULT * contratos

    if resultado == 'take_profit' and rng.random() < 0.05:
        ajuste += ganancia * rng.uniform(0.3, 0.7) - ganancia

    if rng.random() < 0.02:
        ajuste -= abs(ganancia) * rng.uniform(0.20, 0.50)

    return round(ganancia + ajuste, 2)


# ══════════════════════════════════════════════════════════════════════════════
# MOTOR DE BACKTEST GENERICO
# ══════════════════════════════════════════════════════════════════════════════

def run_eval(estrategia, df, dias, start, rng):
    capital         = CAPITAL_0
    peak            = CAPITAL_0
    resultado_final = 'INCOMPLETA'
    trades          = 0
    dias_operados   = 0
    ultimo_dia      = start
    dias_activos    = [d for d in dias if d >= start]
    orb_sizes       = []

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

        trade = None
        if estrategia == 'ORB':
            trade, orb_sizes, _ = signal_orb(df_c, hoy, capital, orb_sizes)
        elif estrategia == 'IFVG_OB':
            trade, _ = signal_ifvg_ob(df_c, hoy, capital)

        if trade:
            ganancia_real  = ajustar_realismo(trade, rng)
            capital       += ganancia_real
            trades        += 1
            dias_operados += 1

        peak   = max(peak, capital)
        limite = MLL_FIJO if peak >= ITB else peak - DRAWDOWN

        pnl = capital - CAPITAL_0
        if pnl >= TARGET:
            resultado_final = 'PASADA'
            break
        if capital <= limite:
            resultado_final = 'EXPLOTADA'
            break

    duracion  = (ultimo_dia - start).days
    pnl_final = round(capital - CAPITAL_0, 0)
    return resultado_final, duracion, trades, pnl_final, dias_operados


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print("=" * 62)
    print("  BACKTEST COMPARACION — ORB+VWAP (1h) vs IFVG+OB (15m)")
    print("  MES=F — %d evals — Seed: %d" % (N_CUENTAS, SEED))
    print("=" * 62)
    print()

    # ── Descargar datos ───────────────────────────────────────────────────────
    print("Descargando MES=F 15m (~60 dias, limite yfinance)...")
    df_15m = yf.download('MES=F', period='59d', interval='15m',
                         progress=False, auto_adjust=True)
    if df_15m.empty:
        raise SystemExit("Error: no se pudo descargar MES=F 15m")
    if isinstance(df_15m.columns, pd.MultiIndex):
        df_15m.columns = df_15m.columns.droplevel(1)
    df_15m.index = (df_15m.index.tz_convert(ET) if df_15m.index.tzinfo
                    else df_15m.index.tz_localize('UTC').tz_convert(ET))

    print("Descargando MES=F 1h (mismo periodo para ORB)...")
    df_1h = yf.download('MES=F', period='59d', interval='1h',
                        progress=False, auto_adjust=True)
    if df_1h.empty:
        raise SystemExit("Error: no se pudo descargar MES=F 1h")
    if isinstance(df_1h.columns, pd.MultiIndex):
        df_1h.columns = df_1h.columns.droplevel(1)
    df_1h.index = (df_1h.index.tz_convert(ET) if df_1h.index.tzinfo
                   else df_1h.index.tz_localize('UTC').tz_convert(ET))

    # ── Dias comunes (desde la fecha mas reciente de ambos datasets) ──────────
    dias = sorted({
        ts.astimezone(ET).date()
        for ts in df_15m.index
        if ts.astimezone(ET).weekday() < 5
    })
    print("Datos: %s -> %s (%d dias de trading)" % (dias[0], dias[-1], len(dias)))

    dias_utiles = dias[WARMUP:]
    if len(dias_utiles) < N_CUENTAS:
        print("Advertencia: solo %d dias utiles, ajustando evals a %d" % (
            len(dias_utiles), len(dias_utiles)))
        n_eval = len(dias_utiles)
    else:
        n_eval = N_CUENTAS

    paso   = max(1, (len(dias_utiles) - 1) // (n_eval - 1)) if n_eval > 1 else 1
    starts = [dias_utiles[min(i * paso, len(dias_utiles) - 1)]
              for i in range(n_eval)]

    print("Warmup: %d dias | Espaciado: ~%d dia(s) entre cuentas" % (WARMUP, paso))
    print()

    rng_master = random.Random(SEED)
    semillas   = [rng_master.randint(0, 999_999) for _ in range(n_eval)]

    # ═══════════════════════════════════
    # BLOQUE 1 — ORB + VWAP (1h)
    # ═══════════════════════════════════
    print("Corriendo %d evals — ORB + VWAP (1h)..." % n_eval)
    res_orb = []
    for idx, (start, semilla) in enumerate(zip(starts, semillas)):
        rng = random.Random(semilla)
        rf, dur, trades, pnl, dias_op = run_eval('ORB', df_1h, dias, start, rng)
        res_orb.append((idx + 1, start, rf, dur, trades, pnl, dias_op))
        print("  [%2d] %s -> %-10s | %3d dias | %2d trades | $%+.0f" % (
            idx + 1, start, rf, dur, trades, pnl))

    # ═══════════════════════════════════
    # BLOQUE 2 — IFVG + OB (15m)
    # ═══════════════════════════════════
    print()
    print("Corriendo %d evals — IFVG + OB (15m)..." % n_eval)
    res_ifvg = []
    for idx, (start, semilla) in enumerate(zip(starts, semillas)):
        rng = random.Random(semilla)
        rf, dur, trades, pnl, dias_op = run_eval('IFVG_OB', df_15m, dias, start, rng)
        res_ifvg.append((idx + 1, start, rf, dur, trades, pnl, dias_op))
        print("  [%2d] %s -> %-10s | %3d dias | %2d trades | $%+.0f" % (
            idx + 1, start, rf, dur, trades, pnl))

    # ═══════════════════════════════════
    # RESUMEN COMPARATIVO
    # ═══════════════════════════════════

    def _stats(resultados):
        pas  = [r for r in resultados if r[2] == 'PASADA']
        exp  = [r for r in resultados if r[2] == 'EXPLOTADA']
        inc  = [r for r in resultados if r[2] == 'INCOMPLETA']
        dias_prom = np.mean([r[3] for r in pas]) if pas else 0

        FEE   = PLANES[CUENTA]['fee_evaluacion']
        SPLIT = 0.90
        total_fees     = n_eval * FEE
        ganancia_bruta = sum(r[5] for r in pas)
        ganancia_neta  = ganancia_bruta * SPLIT - total_fees
        roi = (ganancia_neta / total_fees * 100) if total_fees > 0 else 0

        return len(pas), len(exp), len(inc), dias_prom, roi, total_fees

    pas_orb,  exp_orb,  inc_orb,  dp_orb,  roi_orb,  fees_orb  = _stats(res_orb)
    pas_ifvg, exp_ifvg, inc_ifvg, dp_ifvg, roi_ifvg, fees_ifvg = _stats(res_ifvg)

    fmt_n = "/%d" % n_eval

    print()
    print("=" * 66)
    print("  COMPARACION — %d EVALS — MISMO PERIODO ~60 DIAS" % n_eval)
    print("=" * 66)
    print("  %-22s  %s  %s  %s  %8s  %6s" % (
        "Estrategia", "Pasadas", "Explotadas", "Incompletas", "Dias prom", "ROI"))
    print("  " + "-" * 62)
    print("  %-22s  %3d%-4s  %6d%-4s  %7d%-4s  %8.0f  %5.0f%%" % (
        "ORB + VWAP (1h)",
        pas_orb, fmt_n, exp_orb, fmt_n, inc_orb, fmt_n, dp_orb, roi_orb))
    print("  %-22s  %3d%-4s  %6d%-4s  %7d%-4s  %8.0f  %5.0f%%" % (
        "IFVG + OB (15m)",
        pas_ifvg, fmt_n, exp_ifvg, fmt_n, inc_ifvg, fmt_n, dp_ifvg, roi_ifvg))
    print("=" * 66)
    print()
    print("  Fee por eval: $%d | Profit split: 90%%" % PLANES[CUENTA]['fee_evaluacion'])
    print("  Total invertido (fees): $%.0f" % fees_orb)
    print()

    if pas_orb > 0 or pas_ifvg > 0:
        mejor = "ORB + VWAP (1h)" if roi_orb >= roi_ifvg else "IFVG + OB (15m)"
        print("  Estrategia con mayor ROI: %s" % mejor)
    else:
        print("  Ninguna estrategia paso en este periodo.")
    print("=" * 66)


if __name__ == '__main__':
    main()
