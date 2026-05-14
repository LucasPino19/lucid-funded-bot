"""
Backtest INDEPENDIENTE — creado por otro agente, corrido con datos reales de MES=F.
Modificado para usar yfinance en lugar de datos sintéticos.
"""

import pandas as pd
import numpy as np
import random
import yfinance as yf
from datetime import datetime, timedelta, date, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

CAPITAL_INICIAL = 25000
PROFIT_TARGET = 1250
MAX_DRAWDOWN = 1000
MAX_CONTRATOS = 20
RIESGO_PCT = 0.01
COSTO_CONTRATO = 4.0
MULT = 5
TICK = 0.25

ORB_HORA_INICIO = 9
ORB_VENTANA_H = 13
ORB_VENTANA_M = 30
ORB_STOP_MULT = 1.5
ORB_TARGET_MULT = 1.5
ORB_VOLT_FILTRO = 1.5
ADX_MIN = 20

CIERRE_HORA = 16
CIERRE_MIN = 30
MAX_CONSEC_PERDIDAS = 2

NOTICIAS = set()
for fechas, _ in [
    ([(2025,9,5),(2025,10,3),(2025,11,7),(2025,12,5),
      (2026,1,9),(2026,2,6),(2026,3,6),(2026,4,3),
      (2026,5,1),(2026,6,5),(2026,7,2),(2026,8,7),
      (2026,9,4),(2026,10,2),(2026,11,6),(2026,12,4)], 'NFP'),
    ([(2025,9,10),(2025,10,15),(2025,11,12),(2025,12,10),
      (2026,1,15),(2026,2,12),(2026,3,12),(2026,4,10),
      (2026,5,13),(2026,6,10),(2026,7,15),(2026,8,12),
      (2026,9,9),(2026,10,14),(2026,11,12),(2026,12,9)], 'CPI'),
    ([(2025,9,11),(2025,10,16),(2025,11,13),(2025,12,11),
      (2026,1,16),(2026,2,13),(2026,3,13),(2026,4,11),
      (2026,5,14),(2026,6,11),(2026,7,16),(2026,8,13),
      (2026,9,10),(2026,10,15),(2026,11,13),(2026,12,10)], 'PPI'),
]:
    for y, m, d in fechas:
        NOTICIAS.add(date(y, m, d))


def calcular_vwap_dia(df_dia):
    tp = (df_dia['High'] + df_dia['Low'] + df_dia['Close']) / 3
    tpv_cum = (tp * df_dia['Volume']).cumsum()
    vol_cum = df_dia['Volume'].cumsum()
    return tpv_cum / vol_cum


def calcular_adx(df_diario, period=14):
    n = len(df_diario)
    if n < 2 * period + 2:
        return np.zeros(n)
    hi = df_diario['High'].values
    lo = df_diario['Low'].values
    cl = df_diario['Close'].values
    tr = np.zeros(n); dp = np.zeros(n); dn = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(hi[i]-lo[i], abs(hi[i]-cl[i-1]), abs(lo[i]-cl[i-1]))
        u = hi[i] - hi[i-1]; d = lo[i-1] - lo[i]
        if u > d and u > 0: dp[i] = u
        if d > u and d > 0: dn[i] = d
    atr = np.zeros(n); sdp = np.zeros(n); sdn = np.zeros(n)
    atr[period] = tr[1:period+1].sum()
    sdp[period] = dp[1:period+1].sum()
    sdn[period] = dn[1:period+1].sum()
    for i in range(period+1, n):
        atr[i] = atr[i-1] - atr[i-1]/period + tr[i]
        sdp[i] = sdp[i-1] - sdp[i-1]/period + dp[i]
        sdn[i] = sdn[i-1] - sdn[i-1]/period + dn[i]
    dip = np.where(atr > 0, 100 * sdp / atr, 0)
    din = np.where(atr > 0, 100 * sdn / atr, 0)
    dx = np.where(dip + din > 0, 100 * np.abs(dip - din) / (dip + din), 0)
    adx = np.zeros(n)
    if n > 2 * period:
        adx[2*period] = dx[period:2*period].mean()
        for i in range(2*period+1, n):
            adx[i] = (adx[i-1] * (period-1) + dx[i]) / period
    return adx


def signal_orb_lucid(df_dia, adx_ayer, orb_sizes_hist):
    if adx_ayer < ADX_MIN and adx_ayer > 0:
        return None, orb_sizes_hist, 'ADX bajo'
    if len(df_dia) < 3:
        return None, orb_sizes_hist, 'Pocos datos'

    orb_idx = None
    for i, ts in enumerate(df_dia.index):
        if ts.hour >= ORB_HORA_INICIO:
            orb_idx = i
            break
    if orb_idx is None:
        return None, orb_sizes_hist, 'Sin vela sesion regular'

    orb_bar = df_dia.iloc[orb_idx]
    orb_high = orb_bar['High']
    orb_low  = orb_bar['Low']
    orb_size = orb_high - orb_low
    if orb_size <= 0:
        return None, orb_sizes_hist, 'ORB size = 0'

    sizes_nuevos = orb_sizes_hist.copy()
    if len(orb_sizes_hist) >= 5:
        promedio = np.mean(orb_sizes_hist[-10:])
        if orb_size > promedio * ORB_VOLT_FILTRO:
            sizes_nuevos.append(orb_size)
            return None, sizes_nuevos, 'Volatilidad extrema'
    sizes_nuevos.append(orb_size)

    stop_dist   = orb_size * ORB_STOP_MULT
    target_dist = orb_size * ORB_TARGET_MULT
    vwap = calcular_vwap_dia(df_dia)
    closes = df_dia['Close'].values
    highs  = df_dia['High'].values
    lows   = df_dia['Low'].values

    for k in range(orb_idx + 1, len(df_dia)):
        ts = df_dia.index[k]
        if ts.hour > ORB_VENTANA_H or (ts.hour == ORB_VENTANA_H and ts.minute >= ORB_VENTANA_M):
            break
        if ts.hour > CIERRE_HORA or (ts.hour == CIERRE_HORA and ts.minute >= CIERRE_MIN):
            break
        precio  = closes[k]
        vwap_k  = vwap.iloc[k]
        entrada = None
        if closes[k] > orb_high and precio > vwap_k:
            entrada = orb_high; sl = entrada - stop_dist; tp = entrada + target_dist; direccion = 'LONG'
        elif closes[k] < orb_low and precio < vwap_k:
            entrada = orb_low;  sl = entrada + stop_dist; tp = entrada - target_dist; direccion = 'SHORT'
        if entrada is None:
            continue
        return {'direccion': direccion, 'entrada': entrada, 'sl': sl, 'tp': tp,
                'orb_size': orb_size, 'k_entry': k}, sizes_nuevos, None

    return None, sizes_nuevos, 'Sin señal'


def simular_trade(signal, df_dia, capital):
    entrada   = signal['entrada']
    sl        = signal['sl']
    tp        = signal['tp']
    direccion = signal['direccion']
    k_entry   = signal['k_entry']

    riesgo_usd      = capital * RIESGO_PCT
    riesgo_pts      = abs(entrada - sl)
    riesgo_contrato = riesgo_pts * MULT
    if riesgo_contrato == 0 or riesgo_contrato > riesgo_usd:
        return None
    contratos = min(max(1, int(riesgo_usd / riesgo_contrato)), MAX_CONTRATOS)

    highs  = df_dia['High'].values
    lows   = df_dia['Low'].values
    closes = df_dia['Close'].values

    resultado     = 'timeout'
    precio_salida = closes[-1]

    for m in range(k_entry, len(df_dia)):
        ts = df_dia.index[m]
        if ts.hour > CIERRE_HORA or (ts.hour == CIERRE_HORA and ts.minute >= CIERRE_MIN):
            resultado = 'timeout'; precio_salida = closes[m]; break
        if direccion == 'LONG':
            if lows[m] <= sl:   resultado = 'stop_loss';   precio_salida = sl;  break
            if highs[m] >= tp:  resultado = 'take_profit'; precio_salida = tp;  break
        else:
            if highs[m] >= sl:  resultado = 'stop_loss';   precio_salida = sl;  break
            if lows[m] <= tp:   resultado = 'take_profit'; precio_salida = tp;  break

    puntos  = (precio_salida - entrada) if direccion == 'LONG' else (entrada - precio_salida)
    ganancia = puntos * MULT * contratos - COSTO_CONTRATO * contratos
    return {**signal, 'contratos': contratos, 'salida': precio_salida,
            'resultado': resultado, 'puntos': puntos, 'ganancia': ganancia}


def ajustar_realismo(trade, rng):
    contratos = trade['contratos']
    resultado = trade['resultado']
    ganancia  = trade['ganancia']
    ajuste = 0.0
    if rng.random() < 0.30:
        ajuste -= rng.uniform(0.25, 0.75) * MULT * contratos
    if resultado == 'stop_loss':
        ajuste -= rng.uniform(0.0, 0.50) * MULT * contratos
    if resultado == 'take_profit' and rng.random() < 0.05:
        ajuste += ganancia * rng.uniform(0.3, 0.7) - ganancia
    if rng.random() < 0.02:
        ajuste -= abs(ganancia) * rng.uniform(0.20, 0.50)
    return ganancia + ajuste


def simular_cuenta(df, dias_trading, start_idx, adx_list, rng):
    capital = CAPITAL_INICIAL
    orb_sizes = []
    trades = []
    consec_losses = 0

    for day_idx in range(start_idx, len(dias_trading)):
        hoy = dias_trading[day_idx]
        if hoy in NOTICIAS:
            continue
        if rng.random() < 0.02:
            continue

        adx_ayer = adx_list[day_idx - 1] if day_idx > 0 else 0

        # Filtrar velas del día (usando fecha ET)
        df_dia = df[[ts.date() == hoy for ts in df.index]]
        if len(df_dia) < 3:
            continue

        signal, orb_sizes, _ = signal_orb_lucid(df_dia, adx_ayer, orb_sizes)
        if signal is None:
            continue
        if consec_losses >= MAX_CONSEC_PERDIDAS:
            continue

        trade = simular_trade(signal, df_dia, capital)
        if trade is None:
            continue

        ganancia_real = ajustar_realismo(trade, rng)
        capital += ganancia_real

        if ganancia_real < 0:
            consec_losses += 1
        else:
            consec_losses = 0

        trades.append({'fecha': hoy, 'resultado': trade['resultado'], 'ganancia': round(ganancia_real, 2)})

        pnl = capital - CAPITAL_INICIAL
        if pnl >= PROFIT_TARGET:
            return 'PASADA', len(trades), capital
        if pnl <= -MAX_DRAWDOWN:
            return 'EXPLOTADA', len(trades), capital

    return 'INCOMPLETA', len(trades), capital


def main():
    print("=" * 70)
    print("BACKTEST INDEPENDIENTE — lógica del otro agente, datos reales MES=F")
    print("=" * 70)

    print("Descargando datos reales de MES=F (2 años, 1h)...")
    df = yf.download('MES=F', period='730d', interval='1h', progress=False, auto_adjust=True)
    if df.empty:
        raise SystemExit("No se pudieron descargar datos")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    df.index = (df.index.tz_convert(ET) if df.index.tzinfo
                else df.index.tz_localize('UTC').tz_convert(ET))
    print(f"Datos: {len(df)} velas 1h, {df.index[0].date()} → {df.index[-1].date()}")

    # ADX diario
    df_diario = df[['Open', 'High', 'Low', 'Close']].resample('D').agg(
        {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}
    ).dropna()
    adx_series = calcular_adx(df_diario, period=14)
    adx_por_fecha = {d.date(): adx_series[i] for i, d in enumerate(df_diario.index)}

    dias_trading = sorted({ts.date() for ts in df.index if ts.weekday() < 5})
    adx_list = [adx_por_fecha.get(d, 0) for d in dias_trading]
    print(f"Días de trading: {len(dias_trading)}")

    WARMUP = 30
    dias_utiles = dias_trading[WARMUP:]
    N = 100
    paso = max(1, (len(dias_utiles) - 1) // (N - 1))
    starts_idx = [WARMUP + min(i * paso, len(dias_utiles) - 1) for i in range(N)]
    print(f"Espaciado: ~{paso} días de trading entre cuentas")
    print(f"Corriendo {N} backtests...\n")

    SEED = 42
    rng_global = random.Random(SEED)
    resultados = []

    for idx, start_idx in enumerate(starts_idx):
        rng = random.Random(rng_global.randint(0, 999999))
        estado, n_trades, capital = simular_cuenta(df, dias_trading, start_idx, adx_list, rng)
        pnl = round(capital - CAPITAL_INICIAL, 0)
        start_date = dias_trading[start_idx]
        resultados.append((idx+1, start_date, estado, n_trades, pnl))
        print("  [%3d] %s -> %-10s | %2d trades | $%+.0f" % (idx+1, start_date, estado, n_trades, pnl))

    pasadas    = [r for r in resultados if r[2] == 'PASADA']
    explotadas = [r for r in resultados if r[2] == 'EXPLOTADA']
    incompletas= [r for r in resultados if r[2] == 'INCOMPLETA']

    FEE = 75; SPLIT = 0.90
    total_fees     = N * FEE
    ganancia_bruta = sum(r[4] for r in pasadas)
    ganancia_neta  = ganancia_bruta * SPLIT - total_fees

    print("\n" + "=" * 70)
    print("RESULTADO — backtest independiente con datos reales")
    print("=" * 70)
    print("Pasadas:     %d / %d  (%d%%)" % (len(pasadas), N, len(pasadas)))
    print("Explotadas:  %d / %d  (%d%%)" % (len(explotadas), N, len(explotadas)))
    print("Incompletas: %d / %d" % (len(incompletas), N))
    print("Ganancia neta: $%.0f | ROI: %.0f%%" % (ganancia_neta, ganancia_neta / total_fees * 100 if total_fees else 0))
    print("=" * 70)


if __name__ == '__main__':
    main()
