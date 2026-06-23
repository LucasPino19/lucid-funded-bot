"""
ORB — comparación de MÉTODOS DE ENTRADA (mismas reglas, solo cambia la ejecución).

Capital real $25.000, 1% riesgo, costos $4/contrato + slippage, split OOS.
Manejo de posición idéntico en todas (SL/TP/EOD) → aísla el efecto de la entrada.

Variantes:
  A  Optimista     — confirma con CIERRE (breakout+momentum+ATH) y entra al NIVEL.
                     [LOOK-AHEAD: precio del nivel + selección por el cierre. Techo irreal.]
  B  Realista/hoy  — mismas confirmaciones, pero entra al CIERRE (market). Lo que hace el bot.
  C  Stop al nivel — sell/buy-stop en el nivel; llena al cruzar (sin filtro de cierre).
                     Mejor precio, pero toma TODAS las rupturas (más falsas). ADX+vol como pre-filtro.
  D  Stop-limit    — como C pero descarta si la vela se escapó > CAP del nivel (proxy de no-fill).

(El 5m-confirm queda afuera: yfinance solo da 60d de 5m → muestra insuficiente para comparar.)
"""
from __future__ import annotations
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd, yfinance as yf
from zoneinfo import ZoneInfo
from config import MULT, PLANES
from estrategias import signal_orb_entry, calcular_adx_ayer

ET = ZoneInfo("America/New_York")
CAP0   = 25_000.0
RISK   = 0.01
COSTO  = 4.0
MAXC   = PLANES["25k"]["max_contratos"]
STOPM  = 1.5
TGTM   = 1.5
VOLF   = 1.5
CIERRE = (16, 30)
STOPCAP_D = 6.0   # variante D: descarta si la vela cerró > 6pts del nivel


def load():
    df = yf.download("MES=F", period="730d", interval="1h", progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    df.index = (df.index.tz_convert(ET) if df.index.tzinfo else df.index.tz_localize("UTC").tz_convert(ET))
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()


def precompute_dias(df, et_dates, et_h, et_m):
    """ORB high/low, índice i0, ADX y filtro de volatilidad por día."""
    H, L = df["High"].values, df["Low"].values
    dias = sorted(set(et_dates))
    info = {}
    orb_sizes = []
    for d in dias:
        idx = np.where(et_dates == d)[0]
        i0 = next((i for i in idx if (int(et_h[i]), int(et_m[i])) >= (9, 30)), None)
        if i0 is None:
            continue
        oh, ol = H[i0], L[i0]; osz = oh - ol
        if osz <= 0:
            continue
        vol_ok = True
        if len(orb_sizes) >= 5 and osz > np.mean(orb_sizes[-10:]) * VOLF:
            vol_ok = False
        orb_sizes.append(osz)
        sub = df.iloc[:i0 + 1]
        adx = calcular_adx_ayer(sub)
        adx_ok = (adx == 0.0) or (adx >= 20)   # =0 → datos insuf., opera igual (como el bot)
        info[d] = {"i0": i0, "oh": oh, "ol": ol, "osz": osz, "adx_ok": adx_ok, "vol_ok": vol_ok}
    return info


def simular(df, variant, estricto=False, slip_override=None):
    et_dates = np.array([ts.astimezone(ET).date() for ts in df.index])
    et_h = np.array([ts.astimezone(ET).hour for ts in df.index])
    et_m = np.array([ts.astimezone(ET).minute for ts in df.index])
    O, H, L, C = (df[c].values for c in ["Open", "High", "Low", "Close"])
    n = len(df)
    dinfo = precompute_dias(df, et_dates, et_h, et_m) if variant in ("C", "D") else None

    slip_e = (slip_override if slip_override is not None
              else {"A": 0.0, "B": 0.5, "C": 0.25, "D": 0.25}[variant] * (2 if estricto else 1))
    slip_x = 0.5 if estricto else 0.0   # slippage extra en salidas (estricto)

    cap = CAP0; trades = []
    pos = None; cur = None; th = 0; consec = 0
    orb_sizes = []  # para A/B (signal_orb_entry)
    dias = sorted(set(et_dates)); primer = dias[35] if len(dias) > 35 else dias[-1]

    def cerrar(px, k):
        nonlocal pos, cap
        d = pos["d"]; px_eff = px - d * slip_x
        pts = (px_eff - pos["e"]) * d
        pnl = pts * MULT * pos["c"] - COSTO * pos["c"]
        cap += pnl
        trades.append({"pnl": pnl, "exit_ts": df.index[k]})
        return pnl

    for k in range(n):
        d_k = et_dates[k]
        if d_k < primer:
            continue
        ult = (k == n - 1) or (et_dates[k + 1] != d_k)
        if d_k != cur:
            cur, th, consec = d_k, 0, 0

        # gestionar
        if pos is not None and k > pos["entry_k"]:
            d, e, sl, tp = pos["d"], pos["e"], pos["sl"], pos["tp"]
            out = None
            if ult or (int(et_h[k]), int(et_m[k])) >= CIERRE:
                out = C[k]
            else:
                bull = C[k] >= O[k]
                hit_sl = (L[k] <= sl) if d > 0 else (H[k] >= sl)
                hit_tp = (H[k] >= tp) if d > 0 else (L[k] <= tp)
                if hit_sl and hit_tp:
                    out = tp if bull == (d > 0) else sl   # heurística por dirección de vela
                elif hit_tp:
                    out = tp
                elif hit_sl:
                    out = sl
            if out is not None:
                pnl = cerrar(out, k)
                consec = consec + 1 if pnl <= 0 else 0
                pos = None

        # entrada
        if pos is None and th < 3 and consec < 2:
            hh, mm = int(et_h[k]), int(et_m[k])
            en_ventana = (9 <= hh) and ((hh, mm) < (15, 0))
            if not en_ventana:
                continue
            entry = sl = tp = None; d = 0
            if variant in ("A", "B"):
                win = df.iloc[:k + 1]
                tr, orb_sizes, _ = signal_orb_entry(win, d_k, cap, list(orb_sizes), force_contrato=(th >= 1))
                # nota: orb_sizes se reasigna (replica backtest_critico)
                if tr is not None:
                    d = 1 if tr["direccion"] == "LONG" else -1
                    sd = abs(tr["entrada"] - tr["sl"]); td = abs(tr["tp"] - tr["entrada"])
                    e0 = tr["entrada"] if variant == "A" else C[k]
                    entry = e0 + d * slip_e
                    sl = entry - d * sd; tp = entry + d * td
            else:  # C / D — stop en el nivel
                inf = dinfo.get(d_k)
                if inf is None or k <= inf["i0"] or not inf["adx_ok"] or not inf["vol_ok"]:
                    continue
                oh, ol, osz = inf["oh"], inf["ol"], inf["osz"]
                if H[k] >= oh and not (L[k] <= ol):       # rompe arriba
                    if variant == "D" and C[k] - oh > STOPCAP_D:
                        continue
                    d = 1; entry = oh + slip_e
                elif L[k] <= ol and not (H[k] >= oh):     # rompe abajo
                    if variant == "D" and ol - C[k] > STOPCAP_D:
                        continue
                    d = -1; entry = ol - slip_e
                else:
                    continue
                sd = osz * STOPM; td = osz * TGTM
                sl = entry - d * sd; tp = entry + d * td

            if entry is None:
                continue
            risk_usd = cap * RISK; risk_c = abs(entry - sl) * MULT
            if risk_c <= 0:
                continue
            if risk_c > risk_usd:
                if risk_c > risk_usd * 1.5:
                    continue
                c = 1
            else:
                c = min(max(1, int(risk_usd / risk_c)), MAXC)
            cap -= COSTO * c   # comisión de entrada
            pos = {"d": d, "e": entry, "sl": sl, "tp": tp, "c": c, "entry_k": k}
            th += 1

    return {"trades": trades, "cap": cap}


def met(trades, cap):
    if not trades:
        return None
    g = np.array([t["pnl"] for t in trades])
    w = g[g > 0]; l = g[g <= 0]
    pk = CAP0; mdd = 0.0; eq = CAP0
    for x in g:
        eq += x; pk = max(pk, eq); mdd = min(mdd, eq - pk)
    return {"n": len(g), "wr": len(w) / len(g) * 100, "pf": w.sum() / abs(l.sum()) if l.sum() else float("inf"),
            "exp": g.mean(), "fin": CAP0 + g.sum(), "ret": g.sum() / CAP0 * 100, "dd": mdd}


def fmt(m):
    if m is None:
        return "  (sin trades)"
    pf = f"{m['pf']:.2f}" if m['pf'] != float("inf") else "inf"
    return (f"  n {m['n']:>4} | WR {m['wr']:>4.0f}% | PF {pf:>5} | exp ${m['exp']:>+6.1f} | "
            f"final ${m['fin']:>11,.0f} | ret {m['ret']:>+7.1f}% | DD ${m['dd']:>+9,.0f}")


def main():
    print("Descargando MES=F 1h (730d)...")
    df = load()
    mid = df.index[0] + (df.index[-1] - df.index[0]) / 2
    print(f"Datos: {df.index[0].date()} → {df.index[-1].date()} | OOS desde {mid.date()} | capital ${CAP0:,.0f}\n")
    nombres = {"A": "Optimista (nivel + filtro cierre) [look-ahead]",
               "B": "Realista/actual (confirma cierre → market)",
               "C": "Stop en el nivel (sin filtro cierre)",
               "D": "Stop-limit (nivel, descarta si se escapa)"}
    for est in [False, True]:
        print(f"\n{'#'*86}\n#  ESCENARIO: {'ESTRICTO (slippage doble)' if est else 'BASE'}\n{'#'*86}")
        for v in ["A", "B", "C", "D"]:
            r = simular(df, v, estricto=est)
            tr = r["trades"]
            print(f"\n  {v} — {nombres[v]}")
            print("  TOTAL:" + fmt(met(tr, r["cap"])))
            print("  IS:   " + fmt(met([t for t in tr if t['exit_ts'] < mid], 0)))
            print("  OOS:  " + fmt(met([t for t in tr if t['exit_ts'] >= mid], 0)))


if __name__ == "__main__":
    main()
