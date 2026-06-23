"""
Variante C — entrada ORB por STOP en el nivel (resuelve el slippage).

AISLADO: no toca estrategias.py ni bot.py. Se integra solo cuando esté validado.

Diferencia vs signal_orb_entry (B, actual):
  B: espera que la vela CIERRE más allá del nivel + momentum/ATH/VWAP → entra a mercado
     (ya ~12pt pasado el nivel → slippage).
  C: detecta el CRUCE del nivel (high>=orb_high / low<=orb_low) y entra AL NIVEL,
     como una orden stop en reposo. Sin filtro de cierre (incompatible con un stop
     en reposo: hay que colocarlo ANTES de la ruptura).

Se CONSERVAN los filtros causales (no usan el cierre de la vela del fill):
  - ADX(ayer) > 20  (régimen en tendencia)
  - Filtro de volatilidad del ORB (size vs promedio de los últimos 10)
Se SUELTAN los filtros basados en el cierre de la vela del fill:
  - momentum close_pos, filtro ATH, confirmación VWAP

Devuelve EXACTAMENTE la misma estructura de dict que signal_orb_entry → drop-in.
"""
from zoneinfo import ZoneInfo

import numpy as np

from config import (MULT, RIESGO_PCT, ADX_MIN,
                    ORB_STOP_MULT, ORB_TARGET_MULT, ORB_VOLT_FILTRO,
                    ORB_HORA_INICIO, ORB_MIN_INICIO, ORB_VENTANA_H, ORB_VENTANA_M,
                    CIERRE_HORA, CIERRE_MIN, PLANES, CUENTA)
from estrategias import calcular_adx_ayer

ET = ZoneInfo("America/New_York")


def signal_orb_stop_entry(df_completo, fecha_hoy, capital, orb_sizes_hist,
                          force_contrato=False, riesgo_pct=None):
    """
    Verifica si, desde que se formó el ORB, el precio cruzó un nivel → entrada por stop.
    Misma firma y mismo dict de salida que signal_orb_entry (drop-in).
    """
    max_c = PLANES[CUENTA]['max_contratos']

    # ── Filtro ADX (causal, día anterior) ──
    adx_ayer = calcular_adx_ayer(df_completo)
    if adx_ayer == 0.0:
        pass  # datos insuficientes — opera sin filtro de tendencia (igual que B, intencional)
    elif adx_ayer < ADX_MIN:
        return None, orb_sizes_hist, 'ADX %.1f < %d' % (adx_ayer, ADX_MIN)

    closes = df_completo['Close'].values
    highs  = df_completo['High'].values
    lows   = df_completo['Low'].values
    fechas = df_completo.index

    indices_hoy = [i for i, ts in enumerate(fechas)
                   if ts.astimezone(ET).date() == fecha_hoy]
    if len(indices_hoy) < 2:
        return None, orb_sizes_hist, 'Necesito >= 2 velas del dia'

    # Primera vela de sesión regular (>= 9:30 ET) — idéntico a signal_orb_entry
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

    # ── Filtro de volatilidad (idéntico a signal_orb_entry) ──
    sizes_nuevos = orb_sizes_hist.copy()
    if len(orb_sizes_hist) >= 5:
        promedio = np.mean(orb_sizes_hist[-10:])
        if orb_size > promedio * ORB_VOLT_FILTRO:
            sizes_nuevos.append(orb_size)
            return None, sizes_nuevos, 'Volatilidad extrema'
    sizes_nuevos.append(orb_size)

    # ── Cruce del nivel en la ÚLTIMA vela (la que el bot evalúa en este run) ──
    # No se escanea el primer cruce histórico: un stop en reposo llena UNA vez;
    # re-entrar requiere un cruce nuevo. Evaluar solo la última vela replica eso
    # y coincide con el backtest validado (variante C).
    indices_post_orb = [i for i in indices_hoy if i > i0]
    if not indices_post_orb:
        return None, sizes_nuevos, 'Sin velas post-ORB aun'

    i = indices_post_orb[-1]
    hora_et = fechas[i].astimezone(ET)
    # fuera de ventana de entrada → no se activan stops
    if hora_et.hour > ORB_VENTANA_H or (hora_et.hour == ORB_VENTANA_H and hora_et.minute >= ORB_VENTANA_M):
        return None, sizes_nuevos, 'Fuera de ventana ORB (%02d:%02d ET)' % (ORB_VENTANA_H, ORB_VENTANA_M)
    if hora_et.hour > CIERRE_HORA or (hora_et.hour == CIERRE_HORA and hora_et.minute >= CIERRE_MIN):
        return None, sizes_nuevos, 'Mercado cerrado'

    rompe_arriba = highs[i] >= orb_high
    rompe_abajo  = lows[i]  <= orb_low
    if rompe_arriba and not rompe_abajo:
        cruce = ('LONG', orb_high, i)
    elif rompe_abajo and not rompe_arriba:
        cruce = ('SHORT', orb_low, i)
    else:
        # ni cruza, o cruza ambos lados en la misma vela (ambiguo) → sin señal
        return None, sizes_nuevos, 'Sin cruce de nivel aun'

    direccion, entrada, i_cross = cruce
    stop_dist   = orb_size * ORB_STOP_MULT
    target_dist = orb_size * ORB_TARGET_MULT
    if direccion == 'LONG':
        sl = entrada - stop_dist
        tp = entrada + target_dist
    else:
        sl = entrada + stop_dist
        tp = entrada - target_dist

    # ── Sizing (idéntico a signal_orb_entry, incluido flex-sizing) ──
    pct_efectivo    = riesgo_pct if riesgo_pct is not None else RIESGO_PCT
    riesgo_usd      = capital * pct_efectivo
    riesgo_puntos   = abs(entrada - sl)
    riesgo_contrato = riesgo_puntos * MULT
    if riesgo_contrato == 0:
        return None, sizes_nuevos, 'Riesgo fuera de rango'
    if riesgo_contrato > riesgo_usd:
        if not force_contrato and riesgo_contrato > riesgo_usd * 1.5:
            return None, sizes_nuevos, 'Riesgo fuera de rango (ORB grande)'
        contratos = 1
    else:
        contratos = min(max(1, int(riesgo_usd / riesgo_contrato)), max_c)

    return {
        'estrategia':   'ORB',
        'direccion':    direccion,
        'entrada':      round(entrada, 2),
        'sl':           round(sl, 2),
        'tp':           round(tp, 2),
        'contratos':    contratos,
        'orb_size':     round(orb_size, 2),
        'adx':          round(adx_ayer, 1),
        'hora_entrada': fechas[i_cross].astimezone(ET).hour,
        '_entrada_stop': True,   # marca: esta entrada va por orden STOP en el nivel
    }, sizes_nuevos, None
