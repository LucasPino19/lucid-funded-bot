# ══════════════════════════════════════════════
# CONFIGURACION — LucidFlex Funded Bot
# ══════════════════════════════════════════════
# Para escalar: cambiá CUENTA = '25k'  →  CUENTA = '50k'

CUENTA = '25k'

PLANES = {
    '25k': {
        'capital_inicial': 25_000,
        'profit_target':    1_250,   # +5% para pasar (LucidFlex 25K Flex Eval)
        'max_drawdown':     1_000,   # -4% → explota
        'max_contratos':       20,   # 20 MES (equivale a 2 ES mini)
        'fee_evaluacion':      75,   # precio sin descuento
    },
    '50k': {
        'capital_inicial': 50_000,
        'profit_target':    3_000,
        'max_drawdown':     2_000,
        'max_contratos':       40,   # 40 MES (equivale a 4 ES mini)
        'fee_evaluacion':     175,
    },
}

# ── Futuros ──
TICKER = 'MES=F'   # Micro E-mini S&P 500 (yfinance)
MULT   = 5         # 1 punto MES = $5

# ── Riesgo por trade ──
RIESGO_PCT     = 0.01   # 1% del capital — eval
# FUNDED: cambiar a 0.0075 (0.75%) cuando pase a funded.
# Backtest 50 sims × 12m: 0% blowup vs 10% con 1%. Income mediano $500/mes vs $735.
# Tradeoff aceptado: menos plata, cero riesgo de explotar antes del primer pago.
COSTO_CONTRATO = 4      # slippage $2.50 + comisión $1.50 (SOLO backtest)

# ── Comisión LIVE (se descuenta del P&L real de cada trade en procesar_trade) ──
# Calibrada contra el dashboard Rithmic: drift de -$53.25 / 19 round-turns = $2.80/RT
# (= $1.40/lado). La variante C ya resuelve el slippage, así que el residual es
# pura comisión. Se descuenta COMISION_RT * contratos por trade cerrado.
# Env-overridable si Lucid cambia la tarifa: COMISION_RT=2.80
import os as _os_com
COMISION_RT = float(_os_com.environ.get('COMISION_RT', '2.80'))

# ── Estrategia de payouts (funded) ──────────────────────────────────────────
# MLL NO resetea tras payout (confirmado proptradingvibes.com).
# Retiro agresivo (todo el profit) → 24% blowup en 12m (backtest_payouts.py).
# Estrategia correcta: retirar solo lo que supera PAYOUT_FLOOR.
# Con floor $26,500 → 0% blowup, $410/mes income, $1,500 buffer permanente sobre MLL.
PAYOUT_FLOOR = 26_500   # nunca retirar por debajo de este balance

# ── ORB + VWAP ──
ORB_STOP_MULT        = 1.5   # stop = 1.5x el rango ORB
ORB_TARGET_MULT      = 1.5   # target = 1.5x el rango ORB
ORB_VOLT_FILTRO      = 1.5   # skip si ORB > 1.5x promedio 10 días
ORB_MOMENTUM_UMBRAL  = 0.35  # close_pos SHORT ≤ 35%, LONG ≥ 65% del rango de la vela
ORB_HORA_INICIO = 9     # primera vela de sesión regular (9:30 ET)
ORB_MIN_INICIO  = 30
ORB_VENTANA_H   = 15    # solo entrar antes de las 3:00pm ET (variante B — backtest 500 evals)
ORB_VENTANA_M   = 0
ADX_MIN         = int(_os_com.environ.get('ADX_MIN', '18'))  # 18: sweep 2y OOS → +12 días op, mismo edge/DD que 20. Env-overridable (rollback: ADX_MIN=20)

# ── Variante C: entrada por STOP en el nivel (resuelve el slippage de market) ──
# Backtest validado: B (market post-cierre) OOS -0.7% vs C (stop al nivel) OOS +59%.
# Default OFF → el bot se comporta EXACTAMENTE como hoy. Activar via env var.
#   ORB_STOP_ENTRY=1  → usa entrada por stop en el nivel (variante C).
#   ORB_STOP_DRYRUN=1 → (solo afecta si C está ON) imprime las órdenes Rithmic SIN
#                       enviarlas. Default ON la primera vez por seguridad: validar
#                       params contra MotiveWave antes de poner DRYRUN=0 para operar real.
import os as _os_orb
ORB_STOP_ENTRY  = _os_orb.environ.get('ORB_STOP_ENTRY', '0') == '1'
ORB_STOP_DRYRUN = _os_orb.environ.get('ORB_STOP_DRYRUN', '1') == '1'

# ── Días de NO operar (feriados / cierre temprano de CME) ──
# El bot cierra EOD a las 16:30 ET; en días de cierre temprano no puede manejar la
# posición a tiempo (Lucid exige cerrar antes del early close). En estas fechas NO
# se colocan stops ni se entra. Formato 'YYYY-MM-DD' (ET). Agregar feriados futuros.
HOLIDAYS_NO_TRADE = {
    '2026-07-03',  # Independence Day early close (CME cierra 13:00 ET, Lucid 12:45)
}

# ── Alineación de buckets diarios (EMA/ADX) ──
# False (default): buckets 00:00 -> 00:00 ET — comportamiento histórico.
# True: buckets 18:00 ET -> 17:00 ET — alineado a la sesión CME (globex open
# -> daily settlement). Más correcto, pero hay que validar con backtest antes
# de flippear el default. Override via env var: GLOBEX_ALIGNED=1
import os as _os
GLOBEX_ALIGNED = _os.environ.get('GLOBEX_ALIGNED', '0') == '1'

# ── ICT Kill Zones — VALIDADO PARA 50k, NO ACTIVAR EN 25k ──
# Backtest (50 evals): 25k → 96% pasadas / 4% explosiones (peor que sin KZ)
#                      50k → 100% pasadas / 0% exp / 34 dias prom (vs 39 sin KZ)
# Para activar cuando se tenga cuenta 50k: llamar signal_kz() en bot.py con riesgo_pct=0.005
# Ver memory/project_kz_50k.md para instrucciones de integración.

# ── ICT Order Blocks ──
ICT_IMPULSO     = 4     # velas consecutivas mínimas para OB válido
ICT_STOP_MULT   = 0.25  # stop = 0.25x el tamaño del bloque
ICT_TARGET_MULT = 3.0   # target = 3x el tamaño del bloque
ICT_LOOKBACK    = 80    # velas hacia atrás para buscar reentrada al OB

# ── LucidFlex ──
CIERRE_HORA = 16        # cerrar todo antes de las 4:30pm ET (límite real: 4:45pm)
CIERRE_MIN  = 30
MAX_CONSEC_PERDIDAS = 2 # circuit breaker: parar si 2 pérdidas seguidas en el día

# ── Live execution ──
def _front_month_mes():
    """Calcula el contrato front-month de MES segun la fecha actual.
    Rola al siguiente trimestre ~10 dias calendario antes del 3er viernes."""
    from datetime import date, timedelta
    today = date.today()
    quarters = [(3, 'H'), (6, 'M'), (9, 'U'), (12, 'Z')]
    for year_offset in (0, 1):
        year = today.year + year_offset
        for month_num, code in quarters:
            first_day    = date(year, month_num, 1)
            first_friday = first_day + timedelta(days=(4 - first_day.weekday()) % 7)
            third_friday = first_friday + timedelta(days=14)
            roll_date    = third_friday - timedelta(days=15)
            if today < roll_date:
                year_digit = str(year)[-1]
                return 'MES%s%s' % (code, year_digit)
    return 'MESH%s' % str(today.year + 1)[-1]  # fallback

SYMBOL_LIVE = _front_month_mes()
EXCHANGE_LIVE  = 'CME'
RITHMIC_SYSTEM = 'LucidTrading'
TICK_SIZE      = 0.25         # 1 tick MES = $1.25

# ── Paths ──
ESTADO_FILE = 'data/estado.json'
REPORTS_DIR = 'reports'
