"""
Eval Rapida — ORB + ICT Kill Zones (1%+0.5%)
=============================================
CUANDO USAR: cuando quieras pasar una eval mas rapido que con ORB solo.

Resultados backtest 2 anos (200 sims, seeds 42 y 123):
  - ORB solo:            96% pasadas | 73 dias prom | ROI 1434%
  - ORB + KZ (1%+0.5%): 94% pasadas | 42 dias prom | ROI 1455%

La ventaja: pasa casi el doble de rapido con riesgo practicamente igual.
El riesgo: 11 explosiones vs 5 de ORB solo (aun muy bajo).

Uso:
    python3 eval_rapida.py          # seed 42
    python3 eval_rapida.py 99       # seed custom (reproducible)
"""

import sys
import random
import warnings
import numpy as np
import pandas as pd
import yfinance as yf
from zoneinfo import ZoneInfo

from config import PLANES, CUENTA, MULT
from estrategias import signal_orb, signal_kz
from filtro_noticias import check_noticia

warnings.filterwarnings('ignore', category=RuntimeWarning)

ET        = ZoneInfo("America/New_York")
CAPITAL_0 = float(PLANES[CUENTA]['capital_inicial'])
TARGET    = PLANES[CUENTA]['profit_target']
DRAWDOWN  = PLANES[CUENTA]['max_drawdown']
ITB       = CAPITAL_0 + TARGET
MLL_FIJO  = CAPITAL_0 - PLANES[CUENTA]['mll_lock']

N_CUENTAS      = 100
WARMUP         = 30
RIESGO_ORB     = 0.01    # ORB: 1% de riesgo
RIESGO_KZ      = 0.005   # KZ:  0.5% de riesgo (conservador)
COSTO_CONTRATO = 4
SEED           = int(sys.argv[1]) if len(sys.argv) > 1 else 42


def ajustar_realismo(trade, rng):
    c = trade['contratos']; g = trade['ganancia']; r = trade['resultado']
    a = 0.0
    if rng.random() < 0.30:
        a -= rng.uniform(0.25, 0.75) * MULT * c
    if r == 'stop_loss':
        a -= rng.uniform(0.0, 0.50) * MULT * c
    if r == 'take_profit' and rng.random() < 0.05:
        a += g * rng.uniform(0.3, 0.7) - g
    if rng.random() < 0.02:
        a -= abs(g) * rng.uniform(0.20, 0.50)
    return round(g + a, 2)


def run_eval(df, dias, start, rng):
    capital     = CAPITAL_0
    peak        = CAPITAL_0
    resultado   = 'INCOMPLETA'
    trades      = 0
    ultimo      = start
    orb_sizes   = []
    gan_por_dia = {}

    for hoy in [d for d in dias if d >= start]:
        ultimo = hoy
        if rng.random() < 0.02:
            continue
        df_c = df[[ts.astimezone(ET).date() <= hoy for ts in df.index]]
        if len(df_c) < 50:
            continue
        hay_noticia, _ = check_noticia(hoy)
        if hay_noticia:
            continue

        ganancia_dia = 0.0

        # ── ICT Kill Zone (7am-12pm ET, 0.5% riesgo) ──────────────────────
        trade_kz = signal_kz(df_c, hoy, capital, riesgo_pct=RIESGO_KZ)
        if trade_kz:
            g = ajustar_realismo(trade_kz, rng)
            capital      += g
            ganancia_dia += g
            trades       += 1

        # ── ORB + VWAP (9am-1:30pm ET, 1% riesgo) ─────────────────────────
        trade_orb, orb_sizes, _ = signal_orb(df_c, hoy, capital, orb_sizes)
        if trade_orb:
            g = ajustar_realismo(trade_orb, rng)
            capital      += g
            ganancia_dia += g
            trades       += 1

        if ganancia_dia != 0.0:
            gan_por_dia[hoy] = gan_por_dia.get(hoy, 0.0) + ganancia_dia

        peak   = max(peak, capital)
        limite = MLL_FIJO if peak >= ITB else peak - DRAWDOWN
        pnl    = capital - CAPITAL_0

        if pnl >= TARGET:
            dias_op   = len(gan_por_dia)
            mejor_dia = max(gan_por_dia.values()) if gan_por_dia else 0.0
            if dias_op >= 2 and mejor_dia / pnl <= 0.50:
                resultado = 'PASADA'
            elif dias_op < 2:
                resultado = 'INVALIDA_DIAS'
            else:
                resultado = 'INVALIDA_CONSIST'
            break
        if capital <= limite:
            resultado = 'EXPLOTADA'
            break

    duracion  = (ultimo - start).days
    pnl_final = round(capital - CAPITAL_0, 0)
    return resultado, duracion, trades, pnl_final


def main():
    print("=" * 60)
    print("  EVAL RAPIDA — ORB + ICT Kill Zones (1%+0.5%)")
    print("  Seed: %d" % SEED)
    print("=" * 60)
    print("Descargando MES=F 1h (2 anos)...")

    df = yf.download('MES=F', period='730d', interval='1h',
                     progress=False, auto_adjust=True)
    if df.empty:
        raise SystemExit("Error: no se pudo descargar MES=F")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    df.index = (df.index.tz_convert(ET) if df.index.tzinfo
                else df.index.tz_localize('UTC').tz_convert(ET))

    dias = sorted({
        ts.astimezone(ET).date()
        for ts in df.index if ts.astimezone(ET).weekday() < 5
    })
    print("Datos: %s -> %s (%d dias de trading)" % (dias[0], dias[-1], len(dias)))

    dias_utiles = dias[WARMUP:]
    paso   = max(1, (len(dias_utiles) - 1) // (N_CUENTAS - 1))
    starts = [dias_utiles[min(i * paso, len(dias_utiles) - 1)]
              for i in range(N_CUENTAS)]

    rng_master = random.Random(SEED)
    semillas   = [rng_master.randint(0, 999_999) for _ in range(N_CUENTAS)]

    print("Corriendo %d evals...\n" % N_CUENTAS)
    resultados = []
    for idx, (start, semilla) in enumerate(zip(starts, semillas)):
        rng = random.Random(semilla)
        rf, dur, trades, pnl = run_eval(df, dias, start, rng)
        resultados.append((rf, dur, trades, pnl))
        print("  [%3d] %s -> %-16s | %3d dias | %2d trades | $%+.0f" % (
            idx + 1, start, rf, dur, trades, pnl))

    pasadas  = [r for r in resultados if r[0] == 'PASADA']
    invalidas= [r for r in resultados if r[0].startswith('INVALIDA')]
    explotadas=[r for r in resultados if r[0] == 'EXPLOTADA']
    incomp   = [r for r in resultados if r[0] == 'INCOMPLETA']

    FEE   = PLANES[CUENTA]['fee_evaluacion']
    SPLIT = 0.90
    total_fees    = N_CUENTAS * FEE
    ganancia_neta = sum(r[3] for r in pasadas) * SPLIT - total_fees
    roi = ganancia_neta / total_fees * 100 if total_fees > 0 else 0
    dias_prom = np.mean([r[1] for r in pasadas]) if pasadas else 0

    print()
    print("=" * 60)
    print("  RESUMEN — ORB + ICT Kill Zones (1%%+0.5%%)")
    print("=" * 60)
    print("  Pasadas:     %d/%d  (%.0f%%)" % (len(pasadas), N_CUENTAS, len(pasadas)))
    print("  Invalidas:   %d/%d  (target OK pero viola regla LucidFlex)" % (len(invalidas), N_CUENTAS))
    print("  Explotadas:  %d/%d" % (len(explotadas), N_CUENTAS))
    print("  Incompletas: %d/%d" % (len(incomp), N_CUENTAS))
    if pasadas:
        print("  Dias prom:   %.0f dias corridos" % dias_prom)
    print()
    print("  Total fees:      -$%.0f" % total_fees)
    print("  Ganancia neta:    $%.0f" % ganancia_neta)
    print("  ROI:              %.0f%%" % roi)
    print("=" * 60)


if __name__ == '__main__':
    main()
