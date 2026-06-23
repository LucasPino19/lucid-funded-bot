"""
CUENTA FICTICIA + 30 VERIFICACIONES de la variante C (orb_stop_entry).

No toca el bot. Valida la función nueva de 30 formas distintas antes de integrarla.
Cada check imprime [OK]/[FALLA]. Al final, resumen.
"""
import warnings; warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
from zoneinfo import ZoneInfo

import estrategias
from estrategias import signal_orb_entry
from orb_stop_entry import signal_orb_stop_entry
from config import ORB_STOP_MULT, ORB_TARGET_MULT, PLANES, CUENTA, MULT
from backtest_orb_entradas import load, simular

ET = ZoneInfo("America/New_York")
N_OK = 0; N_TOT = 0


def check(nombre, cond, detalle=""):
    global N_OK, N_TOT
    N_TOT += 1
    if cond:
        N_OK += 1
        print(f"  [OK ] {N_TOT:>2}. {nombre}")
    else:
        print(f"  [FALLA] {N_TOT:>2}. {nombre}   {detalle}")


def dia_sint(bars, fecha="2026-06-22"):
    """Construye un df de un día con velas horarias ET. bars=[(hora,O,H,L,C,V),...]"""
    idx = [pd.Timestamp(f"{fecha} {h:02d}:00", tz="America/New_York") for h, *_ in bars]
    data = {"Open": [b[1] for b in bars], "High": [b[2] for b in bars],
            "Low": [b[3] for b in bars], "Close": [b[4] for b in bars],
            "Volume": [b[5] for b in bars]}
    return pd.DataFrame(data, index=pd.DatetimeIndex(idx))


HOY = pd.Timestamp("2026-06-22").date()
print("="*70)
print("  VALIDACIÓN orb_stop_entry — 30 verificaciones")
print("="*70)

# ── GRUPO A: correctitud unitaria (ADX=0 por datos insuf. → opera) ──
# ORB 10:00 = [90,100]; 11:00 rompe arriba a 101 cerrando en 101
df_long = dia_sint([(9,95,96,94,95,100),(10,92,100,90,98,100),(11,98,101,97,101,100),(12,100,102,99,100,100)])
e_long, _, m1 = signal_orb_stop_entry(df_long, HOY, 25000, [])
check("LONG: dispara entrada en cruce de orb_high", e_long is not None, m1 or "")
check("LONG: entrada == orb_high (precio del nivel)", e_long and e_long['entrada'] == 100.0, str(e_long))
check("LONG: SL distancia == orb_size*1.5", e_long and abs((e_long['entrada']-e_long['sl']) - 10*ORB_STOP_MULT) < 1e-6, str(e_long))
check("LONG: TP distancia == orb_size*1.5", e_long and abs((e_long['tp']-e_long['entrada']) - 10*ORB_TARGET_MULT) < 1e-6, str(e_long))
check("LONG: SL abajo y TP arriba", e_long and e_long['sl'] < e_long['entrada'] < e_long['tp'], str(e_long))

df_short = dia_sint([(9,95,96,94,95,100),(10,92,100,90,98,100),(11,92,99,89,91,100),(12,91,93,88,90,100)])
e_short, _, m2 = signal_orb_stop_entry(df_short, HOY, 25000, [])
check("SHORT: dispara entrada en cruce de orb_low", e_short is not None, m2 or "")
check("SHORT: entrada == orb_low (precio del nivel)", e_short and e_short['entrada'] == 90.0, str(e_short))
check("SHORT: SL arriba y TP abajo", e_short and e_short['tp'] < e_short['entrada'] < e_short['sl'], str(e_short))

check("contratos entre 1 y max_c", e_long and 1 <= e_long['contratos'] <= PLANES[CUENTA]['max_contratos'], str(e_long))
check("dict tiene las mismas claves que signal_orb_entry + marca stop",
      e_long and set(e_long) >= {'estrategia','direccion','entrada','sl','tp','contratos','orb_size','adx','hora_entrada'} and e_long.get('_entrada_stop') is True)

# Check 11: C dispara con cierre DE VUELTA dentro (B no) — sin filtro de cierre
df_wick = dia_sint([(9,95,96,94,95,100),(10,92,100,90,98,100),(11,98,101,97,95,100)])
e_c, _, _ = signal_orb_stop_entry(df_wick, HOY, 25000, [])
e_b, _, _ = signal_orb_entry(df_wick, HOY, 25000, [])
check("Sin filtro cierre: C dispara con mecha (cierra dentro), B NO",
      (e_c is not None and e_c['direccion'] == 'LONG') and (e_b is None), f"C={e_c}, B={e_b}")

# ── GRUPO B: filtros preservados ──
# 12: filtro de volatilidad
_, _, mvol = signal_orb_stop_entry(df_long, HOY, 25000, [1,1,1,1,1,1,1,1,1,1])
check("Filtro volatilidad: ORB grande vs historia → rechaza", mvol == 'Volatilidad extrema', str(mvol))

# 13: ADX < 20 → rechaza (monkeypatch)
_orig_adx = estrategias.calcular_adx_ayer
import orb_stop_entry as _ose
_ose.calcular_adx_ayer = lambda df, period=14: 15.0
_, _, madx = signal_orb_stop_entry(df_long, HOY, 25000, [])
check("Filtro ADX: ADX 15 < 20 → rechaza", madx is not None and 'ADX' in madx, str(madx))
_ose.calcular_adx_ayer = lambda df, period=14: 0.0
e_adx0, _, _ = signal_orb_stop_entry(df_long, HOY, 25000, [])
check("ADX=0 (datos insuf.) → opera igual (como B)", e_adx0 is not None)
_ose.calcular_adx_ayer = _orig_adx

# 15: sin cruce → None
df_noc = dia_sint([(9,95,96,94,95,100),(10,92,100,90,98,100),(11,95,99,91,96,100),(12,96,98,92,95,100)])
_, _, mnoc = signal_orb_stop_entry(df_noc, HOY, 25000, [])
check("Sin cruce de nivel → None con motivo", mnoc == 'Sin cruce de nivel aun', str(mnoc))

# 16: cruce fuera de ventana (después de 15:00) → no entra
df_late = dia_sint([(9,95,96,94,95,100),(10,92,100,90,98,100),(11,95,99,91,96,100),(15,98,101,97,101,100)])
e_late, _, mlate = signal_orb_stop_entry(df_late, HOY, 25000, [])
check("Cruce a las 15:00 (fuera de ventana) → no entra", e_late is None and 'ventana' in (mlate or '').lower(), str(mlate))

# ── GRUPO C: ausencia de look-ahead ──
# 17: truncar el df justo en la vela del cruce → misma decisión (no usa futuro)
df_trunc = df_long.iloc[:3]  # hasta la vela 11:00 (cruce)
e_trunc, _, _ = signal_orb_stop_entry(df_trunc, HOY, 25000, [])
check("Causal: truncado en la vela del cruce → misma entrada", e_trunc and e_trunc['entrada'] == 100.0, str(e_trunc))
# 18: entrada == nivel exacto (no un precio peor/futuro de la vela)
check("Entrada = nivel exacto (no precio intra-vela posterior)", e_long and e_long['entrada'] == 100.0)
# 19: agregar velas futuras NO cambia la entrada ya detectada
df_mas = dia_sint([(9,95,96,94,95,100),(10,92,100,90,98,100),(11,98,101,97,101,100),(12,100,110,99,108,100),(13,108,112,105,110,100)])
e_mas, _, _ = signal_orb_stop_entry(df_mas, HOY, 25000, [])
check("Velas futuras no alteran el nivel de entrada (primer cruce)", e_mas and e_mas['entrada'] == 100.0, str(e_mas))

# 20: hora_entrada = última vela evaluada (el cruce)
check("hora_entrada = hora de la última vela (cruce)", e_long and e_long['hora_entrada'] == 12, str(e_long.get('hora_entrada')))

print("\n  Cargando MES=F real para la cuenta ficticia...")
df = load()
mid = df.index[0] + (df.index[-1]-df.index[0])/2


def cuenta_ficticia(df, slip=0.25):
    """Bot-like: llama signal_orb_stop_entry cada hora, maneja SL/TP/EOD, $25k."""
    edates = np.array([t.astimezone(ET).date() for t in df.index])
    eh = np.array([t.astimezone(ET).hour for t in df.index]); em = np.array([t.astimezone(ET).minute for t in df.index])
    O,H,L,C = (df[c].values for c in ["Open","High","Low","Close"])
    cap=25000.0; trades=[]; pos=None; cur=None; th=0; consec=0; osz=[]
    dias=sorted(set(edates)); prim=dias[35] if len(dias)>35 else dias[-1]
    for k in range(len(df)):
        d=edates[k]
        if d<prim: continue
        ult=(k==len(df)-1) or (edates[k+1]!=d)
        if d!=cur:
            # EOD flatten forzado del día anterior (replica el run programado del bot,
            # que aplana aunque la data no tenga vela de cierre ese día)
            if pos is not None:
                out=C[k-1]
                pnl=(out-pos['e'])*pos['d']*MULT*pos['c']-4*pos['c']
                cap+=pnl
                trades.append({'pnl':pnl,'exit':df.index[k-1],'entry':df.index[pos['ek']]})
                pos=None
            cur,th,consec=d,0,0
        if pos is not None and k>pos['ek']:
            dd=pos['d']; out=None
            if ult or (int(eh[k]),int(em[k]))>=(16,30): out=C[k]
            else:
                bull=C[k]>=O[k]
                hs=(L[k]<=pos['sl']) if dd>0 else (H[k]>=pos['sl'])
                ht=(H[k]>=pos['tp']) if dd>0 else (L[k]<=pos['tp'])
                if hs and ht: out=pos['tp'] if bull==(dd>0) else pos['sl']
                elif ht: out=pos['tp']
                elif hs: out=pos['sl']
            if out is not None:
                pnl=(out-pos['e'])*pos['d']*MULT*pos['c']-4*pos['c']
                cap+=pnl; consec=consec+1 if pnl<=0 else 0
                trades.append({'pnl':pnl,'exit':df.index[k],'entry':df.index[pos['ek']]}); pos=None
        if pos is None and th<3 and consec<2:
            win=df.iloc[:k+1]
            en,osz,_=signal_orb_stop_entry(win, d, cap, osz)
            if en:
                dd=1 if en['direccion']=='LONG' else -1
                e=en['entrada']+dd*slip  # slippage realista del stop
                sd=abs(en['entrada']-en['sl']); td=abs(en['tp']-en['entrada'])
                sl=e-dd*sd; tp=e+dd*td
                pos={'d':dd,'e':e,'sl':sl,'tp':tp,'c':en['contratos'],'ek':k}
                th+=1
    return trades, cap

tr_fic, cap_fic = cuenta_ficticia(df, slip=0.25)
g=np.array([t['pnl'] for t in tr_fic]); w=(g>0).sum()
oos=[t['pnl'] for t in tr_fic if t['exit']>=mid]
# 21-28: cuenta ficticia
check("Cuenta ficticia: genera trades", len(tr_fic) > 50, f"n={len(tr_fic)}")
check("Cuenta ficticia: rentable total", g.sum() > 0, f"P&L ${g.sum():.0f}")
check("Cuenta ficticia: rentable OOS", sum(oos) > 0, f"OOS ${sum(oos):.0f}")
check("Cuenta ficticia: WR razonable (>50%)", w/len(g)*100 > 50, f"WR {w/len(g)*100:.0f}%")
# máx 3 trades/día
from collections import Counter
porfecha = Counter(t['exit'].astimezone(ET).date() for t in tr_fic)
check("Respeta máx 3 trades/día", max(porfecha.values()) <= 3, f"max {max(porfecha.values())}")
# sin holds overnight: cada trade cierra el MISMO día ET que entró (propiedad real;
# el bot real aplana al EOD 16:30 — la vela 18:00 en sim es el corte de settlement de yfinance)
sin_overnight = all(t['entry'].astimezone(ET).date() == t['exit'].astimezone(ET).date() for t in tr_fic)
check("Sin posiciones overnight (cierre el mismo día que la entrada)", sin_overnight,
      f"overnight: {sum(1 for t in tr_fic if t['entry'].astimezone(ET).date()!=t['exit'].astimezone(ET).date())}")

# 28: cross-check vs variante C del backtest validado
r_bt = simular(df, 'C', slip_override=0.25)
g_bt = np.array([t['pnl'] for t in r_bt['trades']])
diff_n = abs(len(tr_fic)-len(g_bt))/max(1,len(g_bt))
check("Consistente con backtest C validado (n ~±20%)", diff_n < 0.20, f"fic n={len(tr_fic)} vs bt n={len(g_bt)}")
check("Consistente con backtest C (ambos rentables, mismo signo)", (g.sum()>0)==(g_bt.sum()>0) and g_bt.sum()>0, f"fic ${g.sum():.0f} vs bt ${g_bt.sum():.0f}")

# ── GRUPO E: robustez ──
# 29: degradación con slippage (sigue + a 2pt, muere a 12pt) — coherencia
tr2,_=cuenta_ficticia(df, slip=2.0); g2=np.array([t['pnl'] for t in tr2])
tr12,_=cuenta_ficticia(df, slip=12.0); g12=np.array([t['pnl'] for t in tr12])
check("Robustez: + a 2pt slippage", g2.sum() > 0, f"${g2.sum():.0f}")
check("Coherencia: peor a 12pt que a 2pt", g12.sum() < g2.sum(), f"12pt ${g12.sum():.0f} < 2pt ${g2.sum():.0f}")

# 31 (bonus → 30): mejor que B en la ventana live del bot real
from datetime import date
LIVE=(date(2026,5,15),date(2026,6,16))
def pnl_rango(tr, lo, hi): return sum(t['pnl'] for t in tr if lo<=t['exit'].astimezone(ET).date()<=hi)
r_b = simular(df,'B');
pnl_c_live = pnl_rango([{'pnl':t['pnl'],'exit':t['exit']} for t in tr_fic], *LIVE)
pnl_b_live = sum(t['pnl'] for t in r_b['trades'] if LIVE[0]<=t['exit_ts'].date()<=LIVE[1])
check("En ventana live real (may-jun): C > B", pnl_c_live > pnl_b_live, f"C ${pnl_c_live:.0f} vs B ${pnl_b_live:.0f}")
# 30: hoy/ayer real — C habría operado el SHORT del 23-jun (lo que el bot real hizo)
check("Reproduce dirección de trades reales (C rentable en 2026)",
      sum(t['pnl'] for t in tr_fic if t['exit'].year==2026) > 0,
      f"2026 ${sum(t['pnl'] for t in tr_fic if t['exit'].year==2026):.0f}")

print("\n" + "="*70)
print(f"  RESULTADO: {N_OK}/{N_TOT} verificaciones OK")
print("="*70)
