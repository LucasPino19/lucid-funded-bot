"""
Estrategias diarias — ORB+VWAP y ICT Order Blocks
Diseñado para correr una vez por día después del cierre del mercado.
Cada función recibe los datos del día y devuelve el trade ejecutado (o None).
"""

import numpy as np
from zoneinfo import ZoneInfo
from config import (MULT, RIESGO_PCT, COSTO_CONTRATO, ADX_MIN,
                    ORB_STOP_MULT, ORB_TARGET_MULT, ORB_VOLT_FILTRO,
                    ORB_HORA_INICIO, ORB_VENTANA_H, ORB_VENTANA_M,
                    ICT_IMPULSO, ICT_STOP_MULT, ICT_TARGET_MULT, ICT_LOOKBACK,
                    CIERRE_HORA, CIERRE_MIN, PLANES, CUENTA)

ET = ZoneInfo("America/New_York")


# ══════════════════════════════════════════════
# UTILIDADES
# ══════════════════════════════════════════════

def calcular_ema_diaria(df, period=20):
    """EMA(20) de cierres diarios. Devuelve (ema_ayer, close_ayer) sin lookahead."""
    df_d = df[['Close']].resample('D').last().dropna()
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
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    df = df.copy()
    df['_tp']    = tp
    df['_tpv']   = tp * df['Volume']
    df['_date']  = df.index.normalize()
    df['_ctpv']  = df.groupby('_date')['_tpv'].cumsum()
    df['_cvol']  = df.groupby('_date')['Volume'].cumsum()
    return (df['_ctpv'] / df['_cvol']).values


def calcular_adx_ayer(df, period=14):
    """ADX del día anterior al último día en df. Sin look-ahead."""
    df_d = df[['Open', 'High', 'Low', 'Close']].resample('D').agg(
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


def _simular_salida(direccion, indices_resto, highs, lows, closes, sl, tp, n_total):
    """Simula stop/target en el resto del día. Cierra antes de las 4:30pm."""
    if not indices_resto:
        return 'timeout', closes[n_total - 1]  # sin velas restantes: cierra al último close
    resultado     = 'timeout'
    precio_salida = closes[indices_resto[-1]]

    for m in indices_resto:
        if direccion == 'LONG':
            if lows[m] <= sl:
                return 'stop_loss', sl
            if highs[m] >= tp:
                return 'take_profit', tp
        else:
            if highs[m] >= sl:
                return 'stop_loss', sl
            if lows[m] <= tp:
                return 'take_profit', tp

    return resultado, precio_salida


def _calcular_trade(capital, entrada, sl, direccion, resultado, precio_salida, contratos):
    puntos         = (precio_salida - entrada) if direccion == 'LONG' else (entrada - precio_salida)
    ganancia_bruta = puntos * MULT * contratos
    costo          = COSTO_CONTRATO * contratos
    return round(puntos, 2), round(ganancia_bruta - costo, 2)


# ══════════════════════════════════════════════
# ESTRATEGIA 1 — ORB + VWAP (con filtro ADX)
# ══════════════════════════════════════════════

def signal_orb(df_completo, fecha_hoy, capital, orb_sizes_hist):
    """
    Busca señal ORB para fecha_hoy en df_completo.
    - df_completo: últimos 30+ días de datos 1h
    - fecha_hoy:   date() del día a evaluar
    - orb_sizes_hist: lista de últimos tamaños ORB (para filtro de volatilidad)
    Devuelve dict con el trade o None.
    """
    max_c = PLANES[CUENTA]['max_contratos']

    # Filtro ADX — mercado tendencial?
    adx_ayer = calcular_adx_ayer(df_completo)
    if adx_ayer < ADX_MIN and adx_ayer > 0:
        return None, orb_sizes_hist, 'ADX %.1f < %d — mercado lateral, skip' % (adx_ayer, ADX_MIN)

    vwap_vals = calcular_vwap(df_completo)
    closes    = df_completo['Close'].values
    highs     = df_completo['High'].values
    lows      = df_completo['Low'].values
    fechas    = df_completo.index

    # Índices del día de hoy
    indices_hoy = [i for i, ts in enumerate(fechas)
                   if ts.astimezone(ET).date() == fecha_hoy]

    if len(indices_hoy) < 3:
        return None, orb_sizes_hist, 'Pocos datos para hoy'

    # Primera vela de sesion regular (>= 9am ET) = rango ORB
    i0 = next((i for i in indices_hoy
               if fechas[i].astimezone(ET).hour >= ORB_HORA_INICIO), None)
    if i0 is None:
        return None, orb_sizes_hist, 'Sin vela de sesion regular'
    orb_high = highs[i0]
    orb_low  = lows[i0]
    orb_size = orb_high - orb_low

    if orb_size <= 0:
        return None, orb_sizes_hist, 'ORB size = 0'

    # Filtro de volatilidad extrema
    sizes_nuevos = orb_sizes_hist.copy()
    if len(orb_sizes_hist) >= 5:
        promedio = np.mean(orb_sizes_hist[-10:])
        if orb_size > promedio * ORB_VOLT_FILTRO:
            sizes_nuevos.append(orb_size)
            return None, sizes_nuevos, 'ORB %.1f > %.1fx promedio — volatilidad extrema, skip' % (orb_size, ORB_VOLT_FILTRO)
    sizes_nuevos.append(orb_size)

    stop_dist   = orb_size * ORB_STOP_MULT
    target_dist = orb_size * ORB_TARGET_MULT

    # Buscar breakout en las velas POSTERIORES a la vela ORB
    indices_post_orb = [i for i in indices_hoy if i > i0]
    for k, i in enumerate(indices_post_orb, 1):
        hora_et = fechas[i].astimezone(ET)
        if hora_et.hour > CIERRE_HORA or (hora_et.hour == CIERRE_HORA and hora_et.minute >= CIERRE_MIN):
            break
        if hora_et.hour > ORB_VENTANA_H or (hora_et.hour == ORB_VENTANA_H and hora_et.minute >= ORB_VENTANA_M):
            break

        precio = closes[i]
        vwap_i = vwap_vals[i]
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
            continue

        riesgo_usd      = capital * RIESGO_PCT
        riesgo_puntos   = abs(entrada - sl)
        riesgo_contrato = riesgo_puntos * MULT
        if riesgo_contrato == 0 or riesgo_contrato > riesgo_usd:
            continue
        contratos = min(max(1, int(riesgo_usd / riesgo_contrato)), max_c)

        # Simular el resto del día
        resto = [j for j in indices_post_orb[k:]
                 if not (fechas[j].astimezone(ET).hour > CIERRE_HORA or
                         (fechas[j].astimezone(ET).hour == CIERRE_HORA and
                          fechas[j].astimezone(ET).minute >= CIERRE_MIN))]
        resultado, precio_salida = _simular_salida(
            direccion, resto, highs, lows, closes, sl, tp, len(closes)
        )

        puntos, ganancia = _calcular_trade(capital, entrada, sl, direccion, resultado, precio_salida, contratos)

        return {
            'estrategia':  'ORB',
            'direccion':   direccion,
            'entrada':     round(entrada, 2),
            'sl':          round(sl, 2),
            'tp':          round(tp, 2),
            'salida':      round(precio_salida, 2),
            'resultado':   resultado,
            'contratos':   contratos,
            'puntos':      puntos,
            'ganancia':    ganancia,
            'orb_size':    round(orb_size, 2),
            'adx':         round(adx_ayer, 1),
            'hora_entrada': fechas[i].astimezone(ET).hour,
        }, sizes_nuevos, None

    return None, sizes_nuevos, 'Sin señal ORB hoy'


# ══════════════════════════════════════════════
# ESTRATEGIA 2 — ICT ORDER BLOCKS
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


def signal_ict(df_completo, fecha_hoy, capital, obs_usados):
    """
    Busca señal ICT para fecha_hoy en df_completo (últimos 90 días).
    - obs_usados: set de strings 'indice_tipo' ya utilizados
    Devuelve dict con el trade o None.
    """
    max_c = PLANES[CUENTA]['max_contratos']

    # Filtro EMA(20): solo LONG si precio > EMA, solo SHORT si precio < EMA
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

    # Índices del día de hoy con horario válido
    indices_hoy = [i for i, ts in enumerate(fechas)
                   if ts.astimezone(ET).date() == fecha_hoy
                   and not (ts.astimezone(ET).hour > CIERRE_HORA or
                            (ts.astimezone(ET).hour == CIERRE_HORA and
                             ts.astimezone(ET).minute >= CIERRE_MIN))]

    if not indices_hoy:
        return None, obs_usados, 'Sin datos para hoy'

    for ob_list, tipo in [(ob_bull, 'bull'), (ob_bear, 'bear')]:
        for ob in ob_list:
            idx     = ob['indice']
            ob_high = ob['ob_high']
            ob_low  = ob['ob_low']
            ob_size = ob_high - ob_low
            clave   = '%d_%s' % (idx, tipo)

            if clave in obs_usados or ob_size <= 0:
                continue

            # Filtro EMA: saltear OBs contra la tendencia diaria
            if tendencia == 'LONG' and tipo == 'bear':
                continue
            if tendencia == 'SHORT' and tipo == 'bull':
                continue

            for k, j in enumerate(indices_hoy):
                if j <= idx + 3:
                    continue

                vwap_j  = vwap_vals[j]
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

                resto = indices_hoy[k+1:]
                if not resto:
                    continue  # sin tiempo restante para gestionar, skip
                resultado, precio_salida = _simular_salida(
                    direccion, resto, highs, lows, closes, sl, tp, len(closes)
                )

                puntos, ganancia = _calcular_trade(
                    capital, entrada, sl, direccion, resultado, precio_salida, contratos
                )

                obs_usados_nuevos = obs_usados | {clave}
                return {
                    'estrategia': 'ICT',
                    'ob_clave':   clave,
                    'tipo_ob':    tipo,
                    'direccion':  direccion,
                    'entrada':    round(entrada, 2),
                    'sl':         round(sl, 2),
                    'tp':         round(tp, 2),
                    'salida':     round(precio_salida, 2),
                    'resultado':  resultado,
                    'contratos':  contratos,
                    'puntos':     puntos,
                    'ganancia':   ganancia,
                    'ob_size':    round(ob_size, 2),
                }, obs_usados_nuevos, None

    return None, obs_usados, 'Sin OB válido tocado hoy'


# ══════════════════════════════════════════════
# MODO INTRADAY — entry en tiempo real
# ══════════════════════════════════════════════

def signal_orb_entry(df_completo, fecha_hoy, capital, orb_sizes_hist):
    """
    Verifica si la última vela completa de hoy genera entrada ORB.
    Solo mira la vela más reciente (no simula el día entero).
    """
    max_c = PLANES[CUENTA]['max_contratos']

    adx_ayer = calcular_adx_ayer(df_completo)
    if adx_ayer < ADX_MIN and adx_ayer > 0:
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

    i0 = next((i for i in indices_hoy
               if fechas[i].astimezone(ET).hour >= ORB_HORA_INICIO), None)
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
        return None, sizes_nuevos, 'Fuera de ventana ORB (1:30pm ET)'
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

    riesgo_usd      = capital * RIESGO_PCT
    riesgo_puntos   = abs(entrada - sl)
    riesgo_contrato = riesgo_puntos * MULT
    if riesgo_contrato == 0 or riesgo_contrato > riesgo_usd:
        return None, sizes_nuevos, 'Riesgo fuera de rango'
    contratos = min(max(1, int(riesgo_usd / riesgo_contrato)), max_c)

    return {
        'estrategia': 'ORB',
        'direccion':  direccion,
        'entrada':    round(entrada, 2),
        'sl':         round(sl, 2),
        'tp':         round(tp, 2),
        'contratos':  contratos,
        'orb_size':   round(orb_size, 2),
        'adx':        round(adx_ayer, 1),
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
            clave   = '%d_%s' % (idx, tipo)

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
                'estrategia': 'ICT',
                'ob_clave':   clave,
                'tipo_ob':    tipo,
                'direccion':  direccion,
                'entrada':    round(entrada, 2),
                'sl':         round(sl, 2),
                'tp':         round(tp, 2),
                'contratos':  contratos,
                'ob_size':    round(ob_size, 2),
            }, obs_usados_nuevos, None

    return None, obs_usados, 'Sin OB tocado en ultima vela'


def gestionar_posicion(posicion, df_completo, fecha_hoy):
    """
    Revisa todas las velas de hoy en orden para ver si la posición abierta
    tocó stop o target. Devuelve el trade cerrado o None si sigue abierto.
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
        return None

    for i in indices_hoy:
        hora_et     = fechas[i].astimezone(ET)
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

    return None


# ══════════════════════════════════════════════
# ESTRATEGIA 3 — ICT KILL ZONES (1h)
# ══════════════════════════════════════════════
# Resultados backtest 2 años (500 sims, seeds 42/123/456/789/999):
#   - 1% riesgo:  64% pasadas, 30% explosiones, ROI 1002%, ~33 dias
#   - 0.5% riesgo: 84% pasadas,  5% explosiones, ROI 1265%, ~75 dias
# Combinado con ORB (ORB 1% + KZ 0.5%): 94% pasadas, 6% explosiones, ROI 1464%
# Para activar: llamar desde bot.py con riesgo_pct=0.005

def signal_kz(df_completo, fecha_hoy, capital, riesgo_pct=0.005):
    """
    ICT Kill Zone — NY Open (7am-12pm ET) en velas 1h.
    Detecta breakout de swing H/L de las 3 velas previas, confirmado por VWAP.
    Por defecto usa 0.5% de riesgo (version conservadora recomendada).
    """
    max_c = PLANES[CUENTA]['max_contratos']

    KZ_HORA_INICIO = 7
    KZ_HORA_FIN    = 12
    KZ_SWING_BARS  = 3
    KZ_SL_MULT     = 1.0
    KZ_TP_MULT     = 2.0

    adx_ayer = calcular_adx_ayer(df_completo)
    if adx_ayer > 0 and adx_ayer < ADX_MIN:
        return None

    vwap_vals = calcular_vwap(df_completo)
    closes    = df_completo['Close'].values
    highs     = df_completo['High'].values
    lows      = df_completo['Low'].values
    fechas    = df_completo.index
    n_total   = len(fechas)

    indices_kz = [
        i for i, ts in enumerate(fechas)
        if ts.astimezone(ET).date() == fecha_hoy
        and KZ_HORA_INICIO <= ts.astimezone(ET).hour < KZ_HORA_FIN
    ]
    if len(indices_kz) < KZ_SWING_BARS + 1:
        return None

    indices_dia = [
        i for i, ts in enumerate(fechas)
        if ts.astimezone(ET).date() == fecha_hoy
        and ts.astimezone(ET).hour >= KZ_HORA_INICIO
        and not (ts.astimezone(ET).hour > CIERRE_HORA or
                 (ts.astimezone(ET).hour == CIERRE_HORA and
                  ts.astimezone(ET).minute >= CIERRE_MIN))
    ]

    for i in indices_kz:
        if i < KZ_SWING_BARS:
            continue

        swing_high  = float(np.max(highs[i - KZ_SWING_BARS:i]))
        swing_low   = float(np.min(lows[i - KZ_SWING_BARS:i]))
        swing_range = swing_high - swing_low
        if swing_range <= 0:
            continue

        close_i = closes[i]
        vwap_i  = vwap_vals[i]

        if close_i > swing_high and close_i > vwap_i:
            direccion = 'LONG'
            entrada   = swing_high
            sl        = entrada - KZ_SL_MULT * swing_range
            tp        = entrada + KZ_TP_MULT * swing_range
        elif close_i < swing_low and close_i < vwap_i:
            direccion = 'SHORT'
            entrada   = swing_low
            sl        = entrada + KZ_SL_MULT * swing_range
            tp        = entrada - KZ_TP_MULT * swing_range
        else:
            continue

        riesgo_puntos = abs(entrada - sl)
        if riesgo_puntos <= 0:
            continue
        contratos = min(max(1, int(capital * riesgo_pct / (riesgo_puntos * MULT))), max_c)

        resto = [j for j in indices_dia if j > i]
        resultado, precio_salida = _simular_salida(
            direccion, resto, highs, lows, closes, sl, tp, n_total
        )
        puntos, ganancia = _calcular_trade(
            capital, entrada, sl, direccion, resultado, precio_salida, contratos
        )

        return {
            'estrategia': 'KZ',
            'direccion':  direccion,
            'entrada':    round(entrada, 2),
            'sl':         round(sl, 2),
            'tp':         round(tp, 2),
            'salida':     round(precio_salida, 2),
            'resultado':  resultado,
            'contratos':  contratos,
            'puntos':     puntos,
            'ganancia':   ganancia,
            'swing_range': round(swing_range, 2),
            'adx':        round(adx_ayer, 1),
        }

    return None


def signal_actividad_minima(df_completo, fecha_hoy):
    """
    Genera una entrada mínima de 1 contrato para mantener la cuenta activa.
    Solo se llama si pasaron >= 28 días sin ningún trade (caso extremo).
    Saltea todos los filtros. Entra LONG al precio actual con SL/TP de 4 puntos.
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

    entrada = round(closes[indices_hoy[-1]], 2)
    return {
        'estrategia': 'ORB',
        'direccion':  'LONG',
        'entrada':    entrada,
        'sl':         round(entrada - 4.0, 2),
        'tp':         round(entrada + 4.0, 2),
        'contratos':  1,
        'orb_size':   0,
        'adx':        0,
        '_actividad': True,
    }
