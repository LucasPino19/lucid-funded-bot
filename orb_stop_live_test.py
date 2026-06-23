"""
Mock-test de la máquina de estados OCO (orquestar_orb_stop_live) SIN Rithmic.
Inyecta get_pos/submit/cancel simulados y verifica cada transición.
"""
import warnings; warnings.filterwarnings("ignore")
import pandas as pd
from zoneinfo import ZoneInfo
from orb_stop_live import orquestar_orb_stop_live

ET = ZoneInfo("America/New_York")
N_OK = 0; N_TOT = 0
def check(n, cond, det=""):
    global N_OK, N_TOT; N_TOT += 1
    print(f"  [{'OK ' if cond else 'FALLA'}] {N_TOT:>2}. {n}" + ("" if cond else f"   {det}"))
    if cond: N_OK += 1

def dia(bars, fecha="2026-06-22"):
    idx=[pd.Timestamp(f"{fecha} {h:02d}:00", tz="America/New_York") for h,*_ in bars]
    return pd.DataFrame({"Open":[b[1] for b in bars],"High":[b[2] for b in bars],
        "Low":[b[3] for b in bars],"Close":[b[4] for b in bars],"Volume":[b[5] for b in bars]},
        index=pd.DatetimeIndex(idx))

HOY = pd.Timestamp("2026-06-22").date()
DIA_STR = "2026-06-22"
# ORB 10:00 = [90,100]; vela 11:00 posterior (ORB cerrado)
DF = dia([(9,95,96,94,95,100),(10,92,100,90,98,100),(11,99,101,97,99,100)])
NOON = pd.Timestamp("2026-06-22 12:00", tz="America/New_York")
EARLY = pd.Timestamp("2026-06-22 10:00", tz="America/New_York")
LATE  = pd.Timestamp("2026-06-22 15:30", tz="America/New_York")

class Mock:
    def __init__(self, pos=(0,None,0)):
        self.pos=pos; self.submits=[]; self.cancels=[]
    def get_pos(self): return self.pos
    def submit(self,d,trig,sl,tp,c,ca,dr): self.submits.append((d,trig,sl,tp,c)); return f"ID_{d}"
    def cancel(self,ids,dr): self.cancels.extend([i for i in ids if i]); return ids

def nuevo_estado():
    return {'ORB_LIVE':{'capital':25000,'orb_sizes':[],'posicion_abierta':None,'stops_pendientes':None}}

print("="*68); print("  MOCK-TEST máquina de estados OCO (orb_stop_live)"); print("="*68)

# ── Escenario 1: coloca ambos stops ──
est=nuevo_estado(); m=Mock()
r=orquestar_orb_stop_live(est,DF,HOY,DIA_STR,NOON,True,m.get_pos,m.submit,m.cancel,log=lambda*a:None)
check("Coloca stops cuando ORB cerrado + en ventana", r=='stops_colocados', r)
check("Envía DOS stops (LONG y SHORT)", len(m.submits)==2, m.submits)
dirs={s[0] for s in m.submits}
check("Un buy-stop en orb_high (100) y un sell-stop en orb_low (90)",
      dirs=={'LONG','SHORT'} and any(s[1]==100.0 for s in m.submits) and any(s[1]==90.0 for s in m.submits), m.submits)
check("SL/TP correctos (LONG: sl=85,tp=115)", any(s[0]=='LONG' and s[2]==85.0 and s[3]==115.0 for s in m.submits), m.submits)
check("Guarda stops_pendientes con dia", est['ORB_LIVE']['stops_pendientes'] and est['ORB_LIVE']['stops_pendientes']['dia']==DIA_STR)

# ── Escenario 2: re-run sin fill → no re-coloca ──
r2=orquestar_orb_stop_live(est,DF,HOY,DIA_STR,NOON,True,m.get_pos,m.submit,m.cancel,log=lambda*a:None)
check("Re-run sin fill → no re-coloca (idempotente)", r2=='stops_ya_colocados' and len(m.submits)==2, r2)

# ── Escenario 3: fill LONG → registra + cancela el SHORT ──
m.pos=(2,'LONG',100.25)
r3=orquestar_orb_stop_live(est,DF,HOY,DIA_STR,NOON,True,m.get_pos,m.submit,m.cancel,log=lambda*a:None)
pos=est['ORB_LIVE']['posicion_abierta']
check("Fill LONG → registra posición abierta", pos and pos['direccion']=='LONG' and pos['contratos']==2, pos)
check("Posición usa el fill real como entrada (100.25)", pos and pos['entrada']==100.25, pos)
check("Cancela el stop OPUESTO (SHORT)", 'ID_SHORT' in m.cancels and 'ID_LONG' not in m.cancels, m.cancels)
check("Limpia stops_pendientes tras el fill", est['ORB_LIVE']['stops_pendientes'] is None)

# ── Escenario 4: con posición abierta → no hace nada ──
r4=orquestar_orb_stop_live(est,DF,HOY,DIA_STR,NOON,True,m.get_pos,m.submit,m.cancel,log=lambda*a:None)
check("Con posición abierta → no coloca nada", r4=='posicion_ya_abierta' and len(m.submits)==2, r4)

# ── Escenario 5: fill SHORT (espejo) ──
est5=nuevo_estado(); m5=Mock()
orquestar_orb_stop_live(est5,DF,HOY,DIA_STR,NOON,True,m5.get_pos,m5.submit,m5.cancel,log=lambda*a:None)
m5.pos=(1,'SHORT',89.75)
orquestar_orb_stop_live(est5,DF,HOY,DIA_STR,NOON,True,m5.get_pos,m5.submit,m5.cancel,log=lambda*a:None)
check("Fill SHORT → registra SHORT y cancela el LONG",
      est5['ORB_LIVE']['posicion_abierta']['direccion']=='SHORT' and 'ID_LONG' in m5.cancels and 'ID_SHORT' not in m5.cancels)

# ── Escenario 6: antes de que cierre el ORB (10:00) → no coloca ──
est6=nuevo_estado(); m6=Mock()
r6=orquestar_orb_stop_live(est6,DF,HOY,DIA_STR,EARLY,True,m6.get_pos,m6.submit,m6.cancel,log=lambda*a:None)
check("Antes de cerrar el ORB (10:00) → no coloca", r6=='orb_no_cerro_aun' and len(m6.submits)==0, r6)

# ── Escenario 7: fuera de ventana (15:30) → no coloca ──
est7=nuevo_estado(); m7=Mock()
r7=orquestar_orb_stop_live(est7,DF,HOY,DIA_STR,LATE,True,m7.get_pos,m7.submit,m7.cancel,log=lambda*a:None)
check("Fuera de ventana (15:30) → no coloca", r7=='fuera_de_ventana' and len(m7.submits)==0, r7)

# ── Escenario 8: ADX < 20 → no coloca (filtro preservado) ──
import orb_stop_live as _osl
_orig=_osl.calcular_adx_ayer
_osl.calcular_adx_ayer=lambda df,period=14:12.0
est8=nuevo_estado(); m8=Mock()
r8=orquestar_orb_stop_live(est8,DF,HOY,DIA_STR,NOON,True,m8.get_pos,m8.submit,m8.cancel,log=lambda*a:None)
check("ADX 12 < 20 → no coloca (filtro ADX)", r8.startswith('sin_setup') and len(m8.submits)==0, r8)
_osl.calcular_adx_ayer=_orig

# ── Escenario 9: nunca coloca ambas direcciones después de un fill (anti doble-posición) ──
check("Anti doble-fill: tras fill, los re-runs no envían nuevos stops",
      len(m.submits)==2)

print("\n"+"="*68); print(f"  RESULTADO: {N_OK}/{N_TOT} OK"); print("="*68)
