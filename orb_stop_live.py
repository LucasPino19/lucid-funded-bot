"""
Variante C — orquestación LIVE de stops en reposo (OCO) sobre Rithmic.

AISLADO: bot.py solo llama orquestar_orb_stop_live(). Toda la máquina de estados vive acá.

Flujo (corre en cada run horario del bot, después de que el ORB cierra a las 11:00):
  1. Sin posición ni stops pendientes hoy + filtros OK → coloca DOS stops de entrada
     (buy-stop en orb_high, sell-stop en orb_low), cada uno con bracket SL/TP, y
     cancel_at = 15:00 (Rithmic auto-cancela el que no se llene).
  2. Si Rithmic muestra posición (un stop filleó) → registra la posición en estado y
     CANCELA el stop opuesto (evita doble fill). Los brackets SL/TP manejan la salida.
  3. cancel_at se encarga de cancelar el stop no llenado al cierre de ventana.

Las funciones de Rithmic (get_pos/submit/cancel) son inyectables para mock-testing.

Riesgo residual conocido: entre que un stop filleá y el siguiente run horario cancela
el opuesto, el otro stop sigue vivo (ventana ≤1h). Un whipsaw que cruce ambos niveles
en esa hora podría abrir la posición opuesta. Se mitiga cancelando en el primer run
post-fill; documentado para vigilar en el primer trade.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from config import (MULT, RIESGO_PCT, ADX_MIN, ORB_STOP_MULT, ORB_TARGET_MULT,
                    ORB_VOLT_FILTRO, ORB_HORA_INICIO, ORB_MIN_INICIO,
                    ORB_VENTANA_H, ORB_VENTANA_M, PLANES, CUENTA)
from estrategias import calcular_adx_ayer

ET = ZoneInfo("America/New_York")

# La vela ORB es la primera >= 9:30. Con velas horarias (:00) eso es la de las 10:00,
# que CIERRA a las 11:00. No se colocan stops antes de que el ORB cierre.
_ORB_START_HOUR = ORB_HORA_INICIO if ORB_MIN_INICIO == 0 else ORB_HORA_INICIO + 1
ORB_CLOSE_HOUR  = _ORB_START_HOUR + 1


def computar_oco_setup(df, fecha_hoy, capital, orb_sizes_hist, riesgo_pct=None):
    """
    Calcula niveles/SL/TP/contratos para colocar los DOS stops de entrada.
    Devuelve dict o None si el ORB no está formado / filtros no pasan.
    NO depende de un cruce (los stops esperan la ruptura). Filtros causales: ADX + volatilidad.
    """
    adx_ayer = calcular_adx_ayer(df)
    if adx_ayer != 0.0 and adx_ayer < ADX_MIN:
        return None, orb_sizes_hist, 'ADX %.1f < %d' % (adx_ayer, ADX_MIN)

    highs = df['High'].values; lows = df['Low'].values; fechas = df.index
    idx_hoy = [i for i, ts in enumerate(fechas) if ts.astimezone(ET).date() == fecha_hoy]
    if len(idx_hoy) < 2:
        return None, orb_sizes_hist, 'Necesito >= 2 velas del dia'

    i0 = next((i for i in idx_hoy
               if (fechas[i].astimezone(ET).hour, fechas[i].astimezone(ET).minute) >=
                  (ORB_HORA_INICIO, ORB_MIN_INICIO)), None)
    if i0 is None:
        return None, orb_sizes_hist, 'Sin vela de sesion regular aun'

    # ORB final solo después de que su vela cerró (debe existir una vela posterior)
    if not any(i > i0 for i in idx_hoy):
        return None, orb_sizes_hist, 'ORB aun no cerro'

    orb_high = highs[i0]; orb_low = lows[i0]; orb_size = orb_high - orb_low
    if orb_size <= 0:
        return None, orb_sizes_hist, 'ORB size = 0'

    sizes_nuevos = list(orb_sizes_hist)
    if len(orb_sizes_hist) >= 5 and orb_size > np.mean(orb_sizes_hist[-10:]) * ORB_VOLT_FILTRO:
        sizes_nuevos.append(orb_size)
        return None, sizes_nuevos, 'Volatilidad extrema'
    sizes_nuevos.append(orb_size)

    stop_dist = orb_size * ORB_STOP_MULT
    target_dist = orb_size * ORB_TARGET_MULT
    # mismo orb_size → mismo riesgo → mismo sizing para ambas direcciones
    pct = riesgo_pct if riesgo_pct is not None else RIESGO_PCT
    riesgo_usd = capital * pct
    riesgo_contrato = stop_dist * MULT
    max_c = PLANES[CUENTA]['max_contratos']
    if riesgo_contrato == 0:
        return None, sizes_nuevos, 'Riesgo fuera de rango'
    if riesgo_contrato > riesgo_usd:
        if riesgo_contrato > riesgo_usd * 1.5:
            return None, sizes_nuevos, 'Riesgo fuera de rango (ORB grande)'
        contratos = 1
    else:
        contratos = min(max(1, int(riesgo_usd / riesgo_contrato)), max_c)

    return {
        'orb_high': round(orb_high, 2), 'orb_low': round(orb_low, 2),
        'orb_size': round(orb_size, 2), 'contratos': contratos,
        'sl_long':  round(orb_high - stop_dist, 2), 'tp_long':  round(orb_high + target_dist, 2),
        'sl_short': round(orb_low + stop_dist, 2),  'tp_short': round(orb_low - target_dist, 2),
    }, sizes_nuevos, None


def orquestar_orb_stop_live(estado, df, hoy, dia_str, ahora_et, dry_run,
                            get_pos_fn, submit_fn, cancel_fn, log=print):
    """
    Máquina de estados OCO. Muta estado['ORB_LIVE'] in-place.
    Devuelve un string con la acción tomada (para logging/tests).
    """
    c = estado['ORB_LIVE']
    cap = c.get('capital', PLANES[CUENTA]['capital_inicial'])

    # 0. Si ya hay posición registrada → ya filleó; los brackets manejan la salida. Nada.
    if c.get('posicion_abierta'):
        return 'posicion_ya_abierta'

    sp = c.get('stops_pendientes')

    # 1. ¿Filleó algún stop? Consultar Rithmic.
    qty, dir_real, fill_price = get_pos_fn()
    if qty and qty > 0:
        setup_res = computar_oco_setup(df, hoy, cap, c.get('orb_sizes', []))
        setup = setup_res[0]
        if setup is None:
            # No se pudo reconstruir el setup — registrar con datos mínimos del fill
            entrada = float(fill_price) if fill_price else 0.0
            pos = {'estrategia': 'ORB', 'direccion': dir_real, 'entrada': round(entrada, 2),
                   'sl': 0.0, 'tp': 0.0, 'contratos': qty, 'orb_size': 0,
                   'hora_entrada': ahora_et.hour, 'dia': dia_str, '_entrada_stop': True}
        else:
            if dir_real == 'LONG':
                sl, tp, trig = setup['sl_long'], setup['tp_long'], setup['orb_high']
            else:
                sl, tp, trig = setup['sl_short'], setup['tp_short'], setup['orb_low']
            pos = {'estrategia': 'ORB', 'direccion': dir_real,
                   'entrada': round(float(fill_price), 2) if fill_price else trig,
                   'sl': sl, 'tp': tp, 'contratos': qty, 'orb_size': setup['orb_size'],
                   'hora_entrada': ahora_et.hour, 'dia': dia_str, '_entrada_stop': True}
        c['posicion_abierta'] = pos
        c['ya_opero_hoy'] = dia_str
        c['ultimo_dia'] = dia_str
        # Cancelar el stop OPUESTO (el que no filleó)
        if sp:
            otro = sp.get('SHORT' if dir_real == 'LONG' else 'LONG')
            cancel_fn([otro], dry_run)
        c['stops_pendientes'] = None
        log('[ORB-C] FILL %s @ %.2f x%d — posición registrada, stop opuesto cancelado.' % (
            dir_real, pos['entrada'], qty))
        return 'fill_%s' % dir_real

    # 2. Sin posición. ¿Ya colocamos stops hoy? entonces esperar (fill o auto-cancel a las 15:00).
    if sp and sp.get('dia') == dia_str:
        return 'stops_ya_colocados'

    # 3. Colocar stops solo dentro de ventana (ORB cierra 11:00; ventana hasta 15:00).
    if ahora_et.hour < ORB_CLOSE_HOUR:   # antes de que cierre la vela ORB (11:00)
        return 'orb_no_cerro_aun'
    if ahora_et.hour > ORB_VENTANA_H or (ahora_et.hour == ORB_VENTANA_H and ahora_et.minute >= ORB_VENTANA_M):
        return 'fuera_de_ventana'

    setup_res = computar_oco_setup(df, hoy, cap, c.get('orb_sizes', []))
    setup, sizes_nuevos, motivo = setup_res
    # actualizar orb_sizes una vez por día
    if sizes_nuevos is not None and c.get('orb_size_dia') != dia_str and len(sizes_nuevos) > len(c.get('orb_sizes', [])):
        c['orb_sizes'] = sizes_nuevos[-20:]
        c['orb_size_dia'] = dia_str
    if setup is None:
        return 'sin_setup:%s' % motivo

    cancel_at = ahora_et.replace(hour=ORB_VENTANA_H, minute=ORB_VENTANA_M, second=0, microsecond=0)
    id_long = submit_fn('LONG', setup['orb_high'], setup['sl_long'], setup['tp_long'],
                        setup['contratos'], cancel_at, dry_run)
    id_short = submit_fn('SHORT', setup['orb_low'], setup['sl_short'], setup['tp_short'],
                         setup['contratos'], cancel_at, dry_run)
    if id_long is None and id_short is None:
        try:
            import live_exec
            err = getattr(live_exec, 'ULTIMO_ERROR_C', None)
        except Exception:
            err = None
        c['orb_c_diag'] = {'dia': dia_str, 'error': err,
                           'long_trigger': setup['orb_high'], 'short_trigger': setup['orb_low'],
                           'sl_long': setup['sl_long'], 'tp_long': setup['tp_long'],
                           'contratos': setup['contratos']}
        log('[ORB-C] FALLO al colocar stops — error=%s (volcado a estado.orb_c_diag)' % err)
        return 'fallo_colocacion'
    c['stops_pendientes'] = {'LONG': id_long, 'SHORT': id_short, 'dia': dia_str,
                             'setup': setup}
    log('[ORB-C] Stops OCO colocados: LONG@%.2f / SHORT@%.2f x%d (cancel_at %02d:%02d)' % (
        setup['orb_high'], setup['orb_low'], setup['contratos'], ORB_VENTANA_H, ORB_VENTANA_M))
    return 'stops_colocados'
