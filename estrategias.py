"""
Estrategias diarias — ORB+VWAP y ICT Order Blocks
Diseñado para correr una vez por día después del cierre del mercado.
Cada función recibe los datos del día y devuelve el trade ejecutado (o None).
"""

import numpy as np
import pandas as pd
from datetime import timedelta
from zoneinfo import ZoneInfo
from config import (MULT, RIESGO_PCT, COSTO_CONTRATO, ADX_MIN,
                    ORB_STOP_MULT, ORB_TARGET_MULT, ORB_VOLT_FILTRO,
                    ORB_HORA_INICIO, ORB_MIN_INICIO, ORB_VENTANA_H, ORB_VENTANA_M,
                    ICT_IMPULSO, ICT_STOP_MULT, ICT_TARGET_MULT, ICT_LOOKBACK,
                    CIERRE_HORA, CIERRE_MIN, PLANES, CUENTA, GLOBEX_ALIGNED)


def _resample_daily(df_sub):
    """Resample a buckets diarios respetando GLOBEX_ALIGNED.

    GLOBEX_ALIGNED=False -> resample('D') con índice ET => buckets 00:00->00:00 ET.
    GLOBEX_ALIGNED=True  -> shift -18h y resample('D') => buckets que representan
        la sesión CME completa (18:00 ET inicio -> 17:00 ET settlement del día siguiente).
        El bucket queda etiquetado con la fecha de APERTURA de la sesión.
    """
    if not GLOBEX_ALIGNED:
        return df_sub.resample('D')
    df_s = df_sub.copy()
    df_s.index = df_s.index - pd.Timedelta(hours=18)
    return df_s.resample('D')

ET = ZoneInfo("America/New_York")


# ══════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════

def calcular_ema_diaria(df, period=20):
    """EMA(20) de cierres diarios. Devuelve (ema_ayer, close_ayer) sin lookahead."""
    df_d = _resample_daily(df[['Close']]).last().dropna()
    if len(df_d) < period + 2:
        return None, None
    closes = df_d['Close'].values
    k = 2 / (period + 1)
    ema = np.zeros(len(closes))
    ema[0] = closes[0]
    for i in range(1, len(closes)):
        ema[i] = closes[i] * k + ema[i-1] * (1 - k)
    return float(ema[-2]), float(closes[-2])


def calcular_vwap(df):
    """VWAP que resetea cada dia en la apertura de RTH (9:30 ET)."""
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    df = df.copy()
    df['_tp']  = tp
    df['_tpv'] = tp * df['Volume']

    et_idx = df.index.tz_convert(ET) if df.index.tz is not None else df.index
    sessions = []
    for ts in et_idx:
        if (ts.hour, ts.minute) < (9, 30):
            sessions.append((ts - timedelta(days=1)).normalize())
        else:
            sessions.append(ts.normalize())
    df['_session'] = sessions
    df['_ctpv']    = df.groupby('_session')['_tpv'].cumsum()
    df['_cvol']    = df.groupby('_session')['Volume'].cumsum()
    vwap = df['_ctpv'] / df['_cvol'].replace(0, float('nan'))
    return vwap.ffill().values


def calcular_adx_ayer(df, period=14):
    """ADX del día anterior al último día en df. Sin look-ahead."""
    df_d = _resample_daily(df[['Open', 'High', 'Low', 'Close']]).agg(
        {'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last'}
    ).dropna()

    n = len(df_d)
    if n < 2 * period + 2:
        return 0.0

    hi = df_d['High'].values
    lo = df_d['Low'].values
    cl = df_d['Close'].values

    tr = np.zeros(n)
    dp = np.zeros(n)
    dn = np.zeros(n)
    for i in range(1, n):
        tr[i] = max(hi[i] - lo[i], abs(hi[i] - cl[i-1]), abs(lo[i] - cl[i-1]))
        u = hi[i] - hi[i-1]
        d = lo[i-1] - lo[i]
        if u > d and u > 0: dp[i] = u
        if d > u and d > 0: dn[i] = d

    atr, sdp, sdn = np.zeros(n), np.zeros(n), np.zeros(n)
    atr[period] = tr[1:period+1].sum()
    sdp[period] = dp[1:period+1].sum()
    sdn[period] = dn[1:period+1].sum()
    for i in range(period + 1, n):
        atr[i] = atr[i-1] - atr[i-1] / period + tr[i]
        sdp[i] = sdp[i-1] - sdp[i-1] / period + dp[i]
        sdn[i] = sdn[i-1] - sdn[i-1] / period + dn[i]

    dip = np.where(atr > 0, 100 * sdp / atr, 0)
    din = np.where(atr > 0, 100 * sdn / atr, 0)
    dx  = np.where(dip + din > 0, 100 * np.abs(dip - din) / (dip + din), 0)

    adx = np.zeros(n)
    if n > 2 * period:
        adx[2 * period] = dx[period:2 * period].mean()
        for i in range(2 * period + 1, n):
            adx[i] = (adx[i-1] * (period - 1) + dx[i]) / period

    return float(adx[-2])  # ADX de ayer (penúltimo día)


def _calcular_trade(capital, entrada, sl, direccion, resultado, precio_salida, contratos):
    puntos         = (precio_salida - entrada) if direccion == 'LONG' else (entrada - precio_salida)
    ganancia_bruta = puntos * MULT * contratos
    costo          = COSTO_CONTRATO * contratos
    return round(puntos, 2), round(ganancia_bruta - costo, 2)


# ══════════════════════════════════════════════
# ESTRATEGIA 2 — ICT ORDER BLOCKS (detección)
# ══════════════════════════════════════════════

def detectar_obs(df, impulso_minimo=ICT_IMPULSO):
    opens  = df['Open'].values
    closes = df['Close'].values
    highs  = df['High'].values
    lows   = df['Low'].values

    ob_bull = []
    ob_bear = []

    for i in range(1, len(df) - impulso_minimo - 1):
        alc = sum(1 for k in range(i+1, min(i+1+impulso_minimo, len(df)))
                  if closes[k] > opens[k] and closes[k] > closes[k-1])
        if closes[i] < opens[i] and alc >= impulso_minimo:
            ob_bull.append({
                'indice':  i,
                'ob_high': float(max(opens[i], closes[i])),
                'ob_low':  float(lows[i]),
            })

        baj = sum(1 for k in range(i+1, min(i+1+impulso_minimo, len(df)))
                  if closes[k] < opens[k] and closes[k] < closes[k-1])
        if closes[i] > opens[i] and baj >= impulso_minimo:
            ob_bear.append({
                'indice':  i,
                'ob_high': float(highs[i]),
                'ob_low':  float(min(opens[i], closes[i])),
            })

    return ob_bull, ob_bear


# ══════════════════════════════════════════════
# MODO INTRADAY — entry en tiempo real
# ══════════════════════════════════════════════

def signal_orb_entry(df_completo, fecha_hoy, capital, orb_sizes_hist, force_contrato=False, riesgo_pct=None):
    """
    Verifica si la última vela completa de hoy genera entrada ORB.
    Solo mira la vela más reciente (no simula el día entero).
    force_contrato=True: usa 1 contrato aunque el riesgo supere el 1% (para 2do trade).
    """
    max_c = PLANES[CUENTA]['max_contratos']

    adx_ayer = calcular_adx_ayer(df_completo)
    if adx_ayer == 0.0:
        pass  # datos insuficientes para ADX — se opera sin filtro de tendencia (intencional)
    elif adx_ayer < ADX_MIN:
        return None, orb_sizes_hist, 'ADX %.1f < %d' % (adx_ayer, ADX_MIN)

    vwap_vals = calcular_vwap(df_completo)
    closes    = df_completo['Close'].values
    highs     = df_completo['High'].values
    lows      = df_completo['Low'].values
    fechas    = df_completo.index

    indices_hoy = [i for i, ts in enumerate(fechas)
                   if ts.astimezone(ET).date() == fecha_hoy]

    if len(indices_hoy) < 2:
        return None, orb_sizes_hist, 'Necesito >= 2 velas del dia'

    # Primera vela de sesion regular: la primera que empieza >= 9:30 ET.
    # Con velas 1h los timestamps son horas exactas (9:00, 10:00...) — nunca hay
    # una vela con minute=30, por lo que se necesita >= y no ==.
    # Para datos 1h: la vela 9:00 tiene (9,0) < (9,30) → se descarta;
    # la vela 10:00 tiene (10,0) >= (9,30) → es i0 (el ORB bar correcto).
    i0 = next((i for i in indices_hoy
               if (fechas[i].astimezone(ET).hour, fechas[i].astimezone(ET).minute) >=
                  (ORB_HORA_INICIO, ORB_MIN_INICIO)), None)
    if i0 is None:
        return None, orb_sizes_hist, 'Sin vela de sesion regular aun'
    orb_high = highs[i0]
    orb_low  = lows[i0]
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

    indices_post_orb = [i for i in indices_hoy if i > i0]
    if not indices_post_orb:
        return None, sizes_nuevos, 'Sin velas post-ORB aun'
    i       = indices_post_orb[-1]
    hora_et = fechas[i].astimezone(ET)

    if hora_et.hour > ORB_VENTANA_H or (hora_et.hour == ORB_VENTANA_H and hora_et.minute >= ORB_VENTANA_M):
        return None, sizes_nuevos, 'Fuera de ventana ORB (%02d:%02d ET)' % (ORB_VENTANA_H, ORB_VENTANA_M)
    if hora_et.hour > CIERRE_HORA or (hora_et.hour == CIERRE_HORA and hora_et.minute >= CIERRE_MIN):
        return None, sizes_nuevos, 'Mercado cerrado'

    precio  = closes[i]
    vwap_i  = vwap_vals[i]
    entrada = None

    if closes[i] > orb_high and precio > vwap_i:
        entrada   = orb_high
        sl        = entrada - stop_dist
        tp        = entrada + target_dist
        direccion = 'LONG'
    elif closes[i] < orb_low and precio < vwap_i:
        entrada   = orb_low
        sl        = entrada + stop_dist
        tp        = entrada - target_dist
        direccion = 'SHORT'

    if entrada is None:
        return None, sizes_nuevos, 'Sin senal en ultima vela'

    pct_efectivo    = riesgo_pct if riesgo_pct is not None else RIESGO_PCT
    riesgo_usd      = capital * pct_efectivo
    riesgo_puntos   = abs(entrada - sl)
    riesgo_contrato = riesgo_puntos * MULT
    if riesgo_contrato == 0:
        return None, sizes_nuevos, 'Riesgo fuera de rango'
    if riesgo_contrato > riesgo_usd:
        # Flex sizing: permite 1 contrato si el riesgo no supera 1.5× el presupuesto.
        # Backtest 200 sims: mismo blowup (8%), +2% pass rate, -3 días mediana.
        if not force_contrato and riesgo_contrato > riesgo_usd * 1.5:
            return None, sizes_nuevos, 'Riesgo fuera de rango (ORB grande)'
        contratos = 1
    else:
        contratos = min(max(1, int(riesgo_usd / riesgo_contrato)), max_c)

    return {
        'estrategia':  'ORB',
        'direccion':   direccion,
        'entrada':     round(entrada, 2),
        'sl':          round(sl, 2),
        'tp':          round(tp, 2),
        'contratos':   contratos,
        'orb_size':    round(orb_size, 2),
        'adx':         round(adx_ayer, 1),
        'hora_entrada': hora_et.hour,
    }, sizes_nuevos, None


def signal_ict_entry(df_completo, fecha_hoy, capital, obs_usados):
    """
    Verifica si la última vela completa de hoy genera entrada ICT.
    Filtro EMA(20): solo LONG si precio > EMA, solo SHORT si precio < EMA.
    """
    max_c = PLANES[CUENTA]['max_contratos']

    # Filtro EMA diaria
    ema_ayer, close_ayer = calcular_ema_diaria(df_completo)
    tendencia = None
    if ema_ayer is not None:
        tendencia = 'LONG' if close_ayer > ema_ayer else 'SHORT'

    vwap_vals = calcular_vwap(df_completo)
    closes    = df_completo['Close'].values
    highs     = df_completo['High'].values
    lows      = df_completo['Low'].values
    fechas    = df_completo.index

    ob_bull, ob_bear = detectar_obs(df_completo)

    indices_hoy = [i for i, ts in enumerate(fechas)
                   if ts.astimezone(ET).date() == fecha_hoy
                   and not (ts.astimezone(ET).hour > CIERRE_HORA or
                            (ts.astimezone(ET).hour == CIERRE_HORA and
                             ts.astimezone(ET).minute >= CIERRE_MIN))]

    if not indices_hoy:
        return None, obs_usados, 'Sin datos para hoy'

    j      = indices_hoy[-1]
    vwap_j = vwap_vals[j]

    for ob_list, tipo in [(ob_bull, 'bull'), (ob_bear, 'bear')]:
        for ob in ob_list:
            idx     = ob['indice']
            ob_high = ob['ob_high']
            ob_low  = ob['ob_low']
            ob_size = ob_high - ob_low
            # DISEÑO: clave basada en timestamp del bar, NO en indice posicional.
            # El indice cambia cada dia porque la ventana de 60d descarta barras antiguas
            # del inicio del dataset — el mismo OB apareceria con un indice distinto y
            # pasaria el filtro de deduplicacion, causando re-entradas al mismo bloque.
            clave   = '%s_%s' % (str(fechas[idx]), tipo)

            if clave in obs_usados or ob_size <= 0 or j <= idx + 3:
                continue

            # Filtro EMA: saltear OBs contra la tendencia diaria
            if tendencia == 'LONG' and tipo == 'bear':
                continue
            if tendencia == 'SHORT' and tipo == 'bull':
                continue

            entrada = None

            if (tipo == 'bull'
                    and lows[j] <= ob_high and highs[j] >= ob_low
                    and closes[j] > vwap_j):
                entrada   = ob_high
                sl        = ob_low - ob_size * ICT_STOP_MULT
                tp        = ob_high + ob_size * ICT_TARGET_MULT
                direccion = 'LONG'

            elif (tipo == 'bear'
                    and highs[j] >= ob_low and lows[j] <= ob_high
                    and closes[j] < vwap_j):
                entrada   = ob_low
                sl        = ob_high + ob_size * ICT_STOP_MULT
                tp        = ob_low  - ob_size * ICT_TARGET_MULT
                direccion = 'SHORT'

            if entrada is None:
                continue

            riesgo_usd      = capital * RIESGO_PCT
            riesgo_puntos   = abs(entrada - sl)
            riesgo_contrato = riesgo_puntos * MULT
            if riesgo_contrato == 0 or riesgo_contrato > riesgo_usd:
                continue
            contratos = min(max(1, int(riesgo_usd / riesgo_contrato)), max_c)

            obs_usados_nuevos = obs_usados | {clave}
            return {
                'estrategia':  'ICT',
                'ob_clave':    clave,
                'tipo_ob':     tipo,
                'direccion':   direccion,
                'entrada':     round(entrada, 2),
                'sl':          round(sl, 2),
                'tp':          round(tp, 2),
                'contratos':   contratos,
                'ob_size':     round(ob_size, 2),
                'hora_entrada': fechas[j].astimezone(ET).hour,
            }, obs_usados_nuevos, None

    return None, obs_usados, 'Sin OB tocado en ultima vela'


def gestionar_posicion(posicion, df_completo, fecha_hoy, force_eod=False):
    """
    Revisa todas las velas de hoy en orden para ver si la posición abierta
    tocó stop o target. Devuelve el trade cerrado o None si sigue abierto.
    force_eod=True: si ninguna vela dispara cierre, fuerza timeout al último close disponible.
    """
    fechas = df_completo.index
    highs  = df_completo['High'].values
    lows   = df_completo['Low'].values
    closes = df_completo['Close'].values

    direccion = posicion['direccion']
    sl        = posicion['sl']
    tp        = posicion['tp']
    entrada   = posicion['entrada']
    contratos = posicion['contratos']

    indices_hoy = [i for i, ts in enumerate(fechas)
                   if ts.astimezone(ET).date() == fecha_hoy]

    if not indices_hoy:
        if force_eod:
            # yfinance aun no tiene velas de hoy — usar el ultimo close disponible
            precio_salida = closes[-1]
            puntos, ganancia = _calcular_trade(
                None, entrada, sl, direccion, 'timeout', precio_salida, contratos)
            return {
                **posicion,
                'salida':    round(precio_salida, 2),
                'resultado': 'timeout',
                'puntos':    puntos,
                'ganancia':  ganancia,
            }
        return None

    hora_entrada = posicion.get('hora_entrada')

    for i in indices_hoy:
        hora_et     = fechas[i].astimezone(ET)
        if hora_entrada is not None and hora_et.hour <= hora_entrada:
            continue
        force_close = (hora_et.hour > CIERRE_HORA or
                       (hora_et.hour == CIERRE_HORA and hora_et.minute >= CIERRE_MIN))

        resultado     = None
        precio_salida = None

        if force_close:
            resultado     = 'timeout'
            precio_salida = closes[i]
        elif direccion == 'LONG':
            if lows[i] <= sl:
                resultado     = 'stop_loss'
                precio_salida = sl
            elif highs[i] >= tp:
                resultado     = 'take_profit'
                precio_salida = tp
        else:
            if highs[i] >= sl:
                resultado     = 'stop_loss'
                precio_salida = sl
            elif lows[i] <= tp:
                resultado     = 'take_profit'
                precio_salida = tp

        if resultado is None:
            continue

        puntos, ganancia = _calcular_trade(
            None, entrada, sl, direccion, resultado, precio_salida, contratos
        )

        return {
            **posicion,
            'salida':    round(precio_salida, 2),
            'resultado': resultado,
            'puntos':    puntos,
            'ganancia':  ganancia,
        }

    # EOD run: barra de 4:30pm puede no estar en yfinance aún — forzar timeout con último close
    if force_eod and indices_hoy:
        precio_salida = closes[indices_hoy[-1]]
        puntos, ganancia = _calcular_trade(
            None, entrada, sl, direccion, 'timeout', precio_salida, contratos
        )
        return {
            **posicion,
            'salida':    round(precio_salida, 2),
            'resultado': 'timeout',
            'puntos':    puntos,
            'ganancia':  ganancia,
        }

    return None


def cerrar_posicion_forzada(posicion, df_completo, fecha_cierre, motivo):
    """
    Cierra forzosamente una posicion al ultimo close disponible de `fecha_cierre`.
    Devuelve un trade dict con resultado='timeout', o None si no hay datos para esa fecha.

    Uso: registrar trades cuya gestion normal se perdio
      - Orfana de dia anterior (run EOD nunca completo).
      - Cierre invisible en Rithmic entre runs (SL/TP del bracket disparo y el bot no lo detecto).

    P&L resultante es APROXIMADO — usa el ultimo close conocido del dia de entrada como salida.
    El usuario deberia conciliar manualmente contra el statement de Rithmic.
    """
    if df_completo is None or len(df_completo) == 0:
        return None

    fechas = df_completo.index
    closes = df_completo['Close'].values

    indices_dia = [i for i, ts in enumerate(fechas)
                   if ts.astimezone(ET).date() == fecha_cierre]
    if not indices_dia:
        # Sin datos del dia (rare: posicion de hace muchos dias fuera del df de 60d)
        return None

    precio_salida = float(closes[indices_dia[-1]])
    puntos, ganancia = _calcular_trade(
        None, posicion['entrada'], posicion['sl'],
        posicion['direccion'], 'timeout', precio_salida, posicion['contratos']
    )
    return {
        **posicion,
        'salida':        round(precio_salida, 2),
        'resultado':     'timeout',
        'puntos':        puntos,
        'ganancia':      ganancia,
        'motivo_cierre': motivo,
    }


def signal_actividad_minima(df_completo, fecha_hoy):
    """
    Genera una entrada mínima de 1 contrato para mantener la cuenta activa.
    Solo se llama si pasaron >= 28 días sin ningún trade (caso extremo).
    Saltea todos los filtros excepto el sesgo direccional por EMA.
    """
    fechas = df_completo.index
    closes = df_completo['Close'].values
    indices_hoy = [i for i, ts in enumerate(fechas)
                   if ts.astimezone(ET).date() == fecha_hoy
                   and ts.astimezone(ET).hour >= ORB_HORA_INICIO
                   and not (ts.astimezone(ET).hour > CIERRE_HORA or
                            (ts.astimezone(ET).hour == CIERRE_HORA and
                             ts.astimezone(ET).minute >= CIERRE_MIN))]
    if not indices_hoy:
        return None
    i_last  = indices_hoy[-1]
    entrada = round(closes[i_last], 2)

    # Sesgo direccional por EMA diaria
    ema_ayer, close_ayer = calcular_ema_diaria(df_completo)
    if ema_ayer is not None and close_ayer < ema_ayer:
        direccion = 'SHORT'
        sl        = round(entrada + 4.0, 2)
        tp        = round(entrada - 4.0, 2)
    else:
        direccion = 'LONG'
        sl        = round(entrada - 4.0, 2)
        tp        = round(entrada + 4.0, 2)

    return {
        'estrategia':   'ORB',
        'direccion':    direccion,
        'entrada':      entrada,
        'sl':           sl,
        'tp':           tp,
        'contratos':    1,
        'orb_size':     0,
        'adx':          0,
        '_actividad':   True,
        'hora_entrada': fechas[i_last].astimezone(ET).hour,
    }
