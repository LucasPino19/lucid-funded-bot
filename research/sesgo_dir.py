"""
Análisis del sesgo LONG vs SHORT en ORB.
Dimensiones a explorar:
  1. WR + P&L + expectancy por dirección (OFF y ON)
  2. Régimen del período (bull/bear/chop)
  3. Estabilidad temporal (semana a semana)
  4. ¿El patch GLOBEX_ALIGNED cambia el sesgo?
  5. Distribución por hora de entrada
  6. Tamaño ORB y ADX por dirección
"""
import json
import os
import numpy as np
import pandas as pd
from collections import defaultdict

REPO = '/sessions/sharp-tender-tesla/mnt/lucid-funded-bot'


def cargar_trades(path):
    with open(path) as f:
        data = json.load(f)
    rows = []
    for ev in data['resultados']:
        for tr in ev['detalle_trades']:
            rows.append({**tr, 'seed': ev['seed']})
    return pd.DataFrame(rows)


def dedupe_unicos(df):
    """Cada trade ORB queda definido por (día, hora_entrada, dirección).
    Como las 200 evals samplean días, el mismo trade aparece muchas veces.
    Dedupe para tener el universo único."""
    return df.drop_duplicates(['dia', 'hora_entrada', 'direccion'])


def stats_por_dir(df, label):
    print(f"\n--- {label} ---")
    print(f"  Total trades únicos: {len(df)}")
    for dir_ in ['LONG', 'SHORT']:
        sub = df[df['direccion'] == dir_]
        if len(sub) == 0:
            continue
        tps = (sub['resultado'] == 'take_profit').sum()
        sls = (sub['resultado'] == 'stop_loss').sum()
        tos = (sub['resultado'] == 'timeout').sum()
        wr = tps / len(sub) * 100
        avg_gan = sub['ganancia'].mean()
        med_gan = sub['ganancia'].median()
        tot_gan = sub['ganancia'].sum()
        # Expectancy = WR * avg_win + (1-WR) * avg_loss
        wins = sub[sub['ganancia'] > 0]['ganancia']
        losses = sub[sub['ganancia'] <= 0]['ganancia']
        ev = (len(wins)/len(sub)) * wins.mean() + (len(losses)/len(sub)) * losses.mean() if len(wins) and len(losses) else None
        print(f"  {dir_}: n={len(sub):>3} | WR={wr:5.1f}% | TP/SL/TO={tps}/{sls}/{tos} | "
              f"avg P&L=${avg_gan:>+7.2f} | total=${tot_gan:>+8.2f} | EV=${ev:>+6.2f}" if ev else
              f"  {dir_}: n={len(sub):>3} | WR={wr:5.1f}% | TP/SL/TO={tps}/{sls}/{tos} | avg=${avg_gan:.2f}")


def regimen_periodo():
    """Caracterizar el período: ¿bull, bear, chop?"""
    df = pd.read_csv(os.path.join(REPO, 'research/cache_mes_1h.csv'),
                     index_col=0, parse_dates=True)
    if df.index.tz is None:
        df.index = df.index.tz_localize('UTC')
    from zoneinfo import ZoneInfo
    df.index = df.index.tz_convert(ZoneInfo('America/New_York'))
    df = df.sort_index()
    primer = df['Close'].iloc[0]
    ultimo = df['Close'].iloc[-1]
    pct = (ultimo / primer - 1) * 100
    high = df['Close'].max()
    low = df['Close'].min()
    rango_pct = (high - low) / low * 100
    print(f"\n=== RÉGIMEN DEL PERÍODO ({df.index[0].date()} -> {df.index[-1].date()}) ===")
    print(f"  Primer close: ${primer:.2f}  Último close: ${ultimo:.2f}  Delta: {pct:+.1f}%")
    print(f"  Range high-low: ${low:.2f} -> ${high:.2f}  ({rango_pct:.1f}%)")

    # Días positivos vs negativos (en cierres diarios calendar ET)
    df_d = df['Close'].resample('D').last().dropna()
    daily_ret = df_d.pct_change().dropna()
    bull = (daily_ret > 0).sum()
    bear = (daily_ret < 0).sum()
    print(f"  Días con close > prev close: {bull}/{len(daily_ret)} ({bull/len(daily_ret)*100:.0f}%)")
    print(f"  Retorno diario medio: {daily_ret.mean()*100:+.2f}% | std: {daily_ret.std()*100:.2f}%")
    print(f"  Trend score (mean/std): {daily_ret.mean()/daily_ret.std():.2f}")
    return df_d


def por_semana(df_trades):
    """¿El sesgo es estable semana a semana?"""
    df_trades['dia_dt'] = pd.to_datetime(df_trades['dia'])
    df_trades['semana'] = df_trades['dia_dt'].dt.isocalendar().week
    print("\n--- WR por semana y dirección (trades únicos) ---")
    semanas = sorted(df_trades['semana'].unique())
    print(f"{'sem':>3} {'fecha':>10} {'LONG':>15} {'SHORT':>15}")
    for sem in semanas:
        sub = df_trades[df_trades['semana'] == sem]
        fecha = sub['dia_dt'].min().date()
        long = sub[sub['direccion'] == 'LONG']
        shrt = sub[sub['direccion'] == 'SHORT']
        l_wr = (long['resultado'] == 'take_profit').sum() / len(long) * 100 if len(long) else float('nan')
        s_wr = (shrt['resultado'] == 'take_profit').sum() / len(shrt) * 100 if len(shrt) else float('nan')
        l_str = f"{l_wr:4.0f}%(n={len(long):>2})" if len(long) else f"  -- (n= 0)"
        s_str = f"{s_wr:4.0f}%(n={len(shrt):>2})" if len(shrt) else f"  -- (n= 0)"
        print(f"{sem:>3} {fecha} {l_str:>15} {s_str:>15}")


def hora_y_adx_orb(df):
    print("\n--- Distribución por hora de entrada (trades únicos) ---")
    for dir_ in ['LONG', 'SHORT']:
        sub = df[df['direccion'] == dir_]
        print(f"  {dir_}:")
        print(f"    horas: {sub['hora_entrada'].value_counts().sort_index().to_dict()}")
        print(f"    ORB size mediano: {sub['orb_size'].median():.1f}  ADX mediano: {sub['adx'].median():.1f}")


# =========================================================================
print("="*70)
print(" ANÁLISIS DEL SESGO LONG vs SHORT — ORB en cache 2026-03-06 a 05-15")
print("="*70)

# Datos OFF (actual)
df_off = cargar_trades(os.path.join(REPO, 'research/results_orb_b_ventana15_200evals_OFF.json'))
df_off_uniq = dedupe_unicos(df_off)
stats_por_dir(df_off_uniq, "OFF — trades únicos (universo histórico)")

# Datos ON (globex)
df_on = cargar_trades(os.path.join(REPO, 'research/results_orb_b_ventana15_200evals_ON.json'))
df_on_uniq = dedupe_unicos(df_on)
stats_por_dir(df_on_uniq, "ON — trades únicos (universo histórico)")

# Régimen del período
df_d = regimen_periodo()

# Estabilidad temporal
por_semana(df_off_uniq)

# Hora y ADX por dirección
hora_y_adx_orb(df_off_uniq)

# Resumen final
print("\n" + "="*70)
print(" RESUMEN")
print("="*70)
for label, df_u in [('OFF', df_off_uniq), ('ON', df_on_uniq)]:
    longs = df_u[df_u['direccion'] == 'LONG']
    shrts = df_u[df_u['direccion'] == 'SHORT']
    l_wr = (longs['resultado'] == 'take_profit').sum() / len(longs) * 100 if len(longs) else 0
    s_wr = (shrts['resultado'] == 'take_profit').sum() / len(shrts) * 100 if len(shrts) else 0
    print(f"  {label}: WR LONG {l_wr:.1f}% (n={len(longs)}) vs SHORT {s_wr:.1f}% (n={len(shrts)}) — gap {s_wr-l_wr:+.1f}pp")
    print(f"     P&L total: LONG ${longs['ganancia'].sum():+.2f} vs SHORT ${shrts['ganancia'].sum():+.2f}")
