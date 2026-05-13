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
        'mll_lock':           100,   # MLL se congela en capital_inicial - 100 cuando supera ITB
        'max_contratos':       20,   # 20 MES (equivale a 2 ES mini)
        'fee_evaluacion':      75,   # precio sin descuento
    },
    '50k': {
        'capital_inicial': 50_000,
        'profit_target':    3_000,
        'max_drawdown':     2_000,
        'mll_lock':           100,   # MLL se congela en capital_inicial - 100 cuando supera ITB
        'max_contratos':       40,   # 40 MES (equivale a 4 ES mini)
        'fee_evaluacion':     175,
    },
}

# ── Futuros ──
TICKER = 'MES=F'   # Micro E-mini S&P 500 (yfinance)
MULT   = 5         # 1 punto MES = $5

# ── Riesgo por trade ──
RIESGO_PCT     = 0.01   # 1% del capital
COSTO_CONTRATO = 4      # slippage $2.50 + comisión $1.50

# ── ORB + VWAP ──
ORB_STOP_MULT   = 1.5   # stop = 1.5x el rango ORB
ORB_TARGET_MULT = 1.5   # target = 1.5x el rango ORB
ORB_VOLT_FILTRO = 1.5   # skip si ORB > 1.5x promedio 10 días
ORB_HORA_INICIO = 9     # primera vela de sesión regular (9am ET)
ORB_VENTANA_H   = 13    # solo entrar antes de la 1:30pm ET
ORB_VENTANA_M   = 30
ADX_MIN         = 20    # mercado en tendencia si ADX > 20

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
# Actualizar cada vez que ruede el contrato (Mar/Jun/Sep/Dec)
SYMBOL_LIVE    = 'MESM6'      # Micro ES contrato activo en Rithmic (actualizar cada trimestre)
EXCHANGE_LIVE  = 'CME'
RITHMIC_SYSTEM = 'Lucid Trading'
TICK_SIZE      = 0.25         # 1 tick MES = $1.25

# ── Paths ──
ESTADO_FILE = 'data/estado.json'
REPORTS_DIR = 'reports'
