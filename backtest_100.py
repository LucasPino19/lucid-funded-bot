"""
Backtest 100 funded accounts — LucidFlex 25K Flex Eval
=======================================================
Simula 100 cuentas espaciadas uniformemente en los ultimos 2 anos.
Aplica ajustes realistas: slippage variable, SL gap, TP miss,
eventos inesperados y fallas de conexion.

Uso:
    python3 backtest_100.py           # seed aleatorio
    python3 backtest_100.py 42        # seed fijo (reproducible)
"""

import sys
import time
import yfinance as yf
import pandas as pd
import numpy as np
import random
from datetime import date
from zoneinfo import ZoneInfo
from config import PLANES, CUENTA, MULT
from estrategias import signal_orb
from filtro_noticias import check_noticia

ET        = ZoneInfo("America/New_York")
CAPITAL_0 = float(PLANES[CUENTA]['capital_inicial'])
TARGET    = PLANES[CUENTA]['profit_target']
DRAWDOWN  = PLANES[CUENTA]['max_drawdown']
ITB       = CAPITAL_0 + TARGET                     # Initial Trail Balance
MLL_FIJO  = CAPITAL_0 - PLANES[CUENTA]['mll_lock'] # MLL congelado una vez que supera ITB
N_CUENTAS = 100
WARMUP    = 30   # dias de warmup para ADX
SEED      = int(sys.argv[1]) if len(sys.argv) > 1 else int(time.time())


def ajustar_realismo(trade, rng):
    """
    Aplica slippage y factores reales al P&L de un trade.

    Ajustes:
    - Entry slippage variable (30% dias con breakout agresivo: 1-3 ticks extra)
    - SL gap slippage (el stop puede gapear 0-2 ticks al activarse)
    - TP miss 5%: la orden limite toca el precio pero no llena -> cierra con 30-70% de la ganancia
    - Evento inesperado 2%: noticia fuera del calendario, empeora el resultado 20-50%
    """
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


def main():
    print("Seed: %d  (para reproducir: python3 backtest_100.py %d)" % (SEED, SEED))
    print("Descargando datos MES=F (2 anos de datos 1h)...")
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
        for ts in df.index
        if ts.astimezone(ET).weekday() < 5
    })
    print("Datos: %s -> %s (%d dias de trading)" % (dias[0], dias[-1], len(dias)))

    dias_utiles = dias[WARMUP:]
    paso        = max(1, (len(dias_utiles) - 1) // (N_CUENTAS - 1))
    starts      = [dias_utiles[min(i * paso, len(dias_utiles) - 1)]
                   for i in range(N_CUENTAS)]

    print("Espaciado: ~%d dias de trading entre cuentas" % paso)
    print("Corriendo %d backtests...\n" % N_CUENTAS)

    resultados  = []
    rng_global  = random.Random(SEED)

    for idx, start in enumerate(starts):
        rng       = random.Random(rng_global.randint(0, 999999))
        capital   = CAPITAL_0
        peak      = CAPITAL_0   # trailing drawdown EOD
        orb_sizes = []
        resultado_final = 'INCOMPLETA'
        trades    = 0
        ultimo_dia = start
        dias_activos = [d for d in dias if d >= start]

        for hoy in dias_activos:
            ultimo_dia = hoy

            # GitHub Actions falla ~2% de los dias
            if rng.random() < 0.02:
                continue

            df_c = df[[ts.astimezone(ET).date() <= hoy for ts in df.index]]
            if len(df_c) < 50:
                continue

            hay_noticia, _ = check_noticia(hoy)
            if hay_noticia:
                continue

            trade, orb_sizes, _ = signal_orb(df_c, hoy, capital, orb_sizes)
            if trade:
                ganancia_real = ajustar_realismo(trade, rng)
                capital      += ganancia_real
                trades       += 1

            # Trailing drawdown EOD con ITB lock (regla real LucidFlex)
            peak = max(peak, capital)
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
        resultados.append((idx + 1, start, resultado_final, duracion, trades, pnl_final))
        print("  [%3d] %s -> %-10s | %3d dias | %2d trades | $%+.0f" % (
            idx + 1, start, resultado_final, duracion, trades, pnl_final))

    # ── Resumen ───────────────────────────────────────────────────────────────
    pasadas    = [r for r in resultados if r[2] == 'PASADA']
    explotadas = [r for r in resultados if r[2] == 'EXPLOTADA']
    incompletas= [r for r in resultados if r[2] == 'INCOMPLETA']

    FEE   = PLANES[CUENTA]['fee_evaluacion']
    SPLIT = 0.90

    total_fees      = N_CUENTAS * FEE
    ganancia_bruta  = sum(r[5] for r in pasadas)
    ganancia_trader = ganancia_bruta * SPLIT
    ganancia_neta   = ganancia_trader - total_fees

    print("\n" + "=" * 60)
    print("  RESUMEN REALISTA — %d FUNDED ACCOUNTS" % N_CUENTAS)
    print("=" * 60)
    print("  Ajustes aplicados:")
    print("    - Entry slippage variable (30%% dias agresivos)")
    print("    - SL gap slippage (0-2 ticks extra)")
    print("    - TP miss 5%% (limite no llena)")
    print("    - Eventos inesperados 2%% por dia")
    print("    - Falla de conexion 2%% dias")
    print()
    print("  RESULTADOS")
    print("  Pasadas:     %d / %d  (%.0f%%)" % (len(pasadas), N_CUENTAS, len(pasadas)))
    print("  Explotadas:  %d / %d  (%.0f%%)" % (len(explotadas), N_CUENTAS, len(explotadas)))
    print("  Incompletas: %d / %d  (sin datos suficientes)" % (len(incompletas), N_CUENTAS))
    if pasadas:
        print("  Dias prom. para pasar: %.0f dias corridos" % np.mean([r[3] for r in pasadas]))
    if explotadas:
        print("  P&L prom. al explotar: $%+.0f" % np.mean([r[5] for r in explotadas]))
    print()
    print("  FINANCIERO (%.0f%% profit split)" % (SPLIT * 100))
    print("  Total gastado:    -$%.0f" % total_fees)
    print("  Ganancia bruta:    $%.0f" % ganancia_bruta)
    print("  Tu parte:          $%.0f" % ganancia_trader)
    print("  Ganancia neta:     $%.0f" % ganancia_neta)
    if total_fees > 0:
        print("  ROI:               %.0f%%" % (ganancia_neta / total_fees * 100))
    print("=" * 60)


if __name__ == '__main__':
    main()
