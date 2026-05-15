"""
LucidFlex Funded Bot — Motor Intraday
======================================
Corre cada hora durante el mercado (10:35am–4:35pm ET, vía GitHub Actions).
Entra en el cierre de cada vela 1h y gestiona posiciones abiertas en tiempo real.
Dos cuentas independientes: ORB+VWAP e ICT Order Blocks.
"""

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

from config import (CUENTA, PLANES, TICKER, ESTADO_FILE, REPORTS_DIR,
                    MAX_CONSEC_PERDIDAS, CIERRE_HORA, CIERRE_MIN)
from estrategias import signal_orb_entry, signal_ict_entry, gestionar_posicion, signal_actividad_minima
from filtro_noticias import check_noticia

# LIVE_MODE=true → ejecuta ordenes reales via Rithmic
LIVE_MODE = os.environ.get('LIVE_MODE', 'false').lower() == 'true'
if LIVE_MODE:
    try:
        from live_exec import submit_bracket_entry, get_open_position, flatten_position
    except Exception as _import_err:
        print('[BOT] ERROR importando live_exec: %s' % _import_err)
        print('[BOT] Continuando en modo simulacion.')
        LIVE_MODE = False

ET   = ZoneInfo("America/New_York")
PLAN = PLANES[CUENTA]


# ══════════════════════════════════════════════
# ESTADO
# ══════════════════════════════════════════════

def estado_inicial(estrategia):
    return {
        'estrategia':       estrategia,
        'cuenta':           CUENTA,
        'capital_inicial':  PLAN['capital_inicial'],
        'capital':          PLAN['capital_inicial'],
        'ganancia_total':   0.0,
        'ganancia_por_dia': {},
        'trades':           [],
        'estado':           'activa',
        'razon_fin':        '',
        'consecutivas_hoy': 0,
        'ultimo_dia':       '',
        'orb_sizes':        [],
        'obs_usados':       [],
        'posicion_abierta': None,   # posicion open intraday
        'ya_opero_hoy':     '',     # fecha del ultimo trade del dia
        'peak_capital':     PLAN['capital_inicial'],  # trailing drawdown EOD
    }


def cargar_estado():
    os.makedirs(os.path.dirname(ESTADO_FILE), exist_ok=True)
    if os.path.exists(ESTADO_FILE):
        with open(ESTADO_FILE) as f:
            data = json.load(f)
        # Completar campos faltantes — evita KeyError cuando se agregan campos nuevos al estado
        for est in ('ORB_LIVE', 'ORB_SIM', 'ICT_SIM'):
            if est not in data:
                data[est] = estado_inicial(est)
            else:
                defaults = estado_inicial(est)
                for key, val in defaults.items():
                    data[est].setdefault(key, val)
        return data
    return {
        'ORB_LIVE': estado_inicial('ORB_LIVE'),
        'ORB_SIM':  estado_inicial('ORB_SIM'),
        'ICT_SIM':  estado_inicial('ICT_SIM'),
    }


def guardar_estado(estado):
    os.makedirs(os.path.dirname(ESTADO_FILE), exist_ok=True)
    with open(ESTADO_FILE, 'w') as f:
        json.dump(estado, f, indent=2, default=str)


# ══════════════════════════════════════════════
# REGLAS LUCIDFLEX
# ══════════════════════════════════════════════

def aplicar_reglas(cuenta_estado, dia_str):
    capital        = cuenta_estado['capital']
    ganancia_total = cuenta_estado['ganancia_total']
    gan_por_dia    = cuenta_estado['ganancia_por_dia']
    trades         = cuenta_estado['trades']

    # Trailing drawdown EOD con ITB lock (regla real LucidFlex)
    # Fase 1: MLL sube con el peak EOD (trailing activo)
    # Fase 2: cuando capital supera ITB, MLL se congela en capital_inicial - mll_lock para siempre
    capital_inicial = PLAN['capital_inicial']
    itb             = capital_inicial + PLAN['profit_target']   # Initial Trail Balance
    mll_fijo        = capital_inicial - PLAN['mll_lock']        # $24,900 para 25k

    peak = max(cuenta_estado.get('peak_capital', capital_inicial), capital)
    cuenta_estado['peak_capital'] = peak

    if peak >= itb:
        limite_drawdown = mll_fijo   # MLL congelado
    else:
        limite_drawdown = peak - PLAN['max_drawdown']   # MLL trailing

    if capital <= limite_drawdown:
        return 'explotada', 'Drawdown alcanzado — capital: $%.0f | limite: $%.0f' % (capital, limite_drawdown)

    if ganancia_total >= PLAN['profit_target']:
        max_dia    = max(gan_por_dia.values()) if gan_por_dia else 0
        dias_op    = len(gan_por_dia)
        consistencia_ok = ganancia_total > 0 and max_dia / ganancia_total <= 0.50
        min_dias_ok     = dias_op >= 2

        if consistencia_ok and min_dias_ok:
            return 'pasada', 'Target alcanzado: $%+.0f en %d dias / %d trades' % (ganancia_total, dias_op, len(trades))
        if not min_dias_ok:
            return 'activa', 'Target alcanzado pero faltan dias de trading (%d/2 minimo)' % dias_op
        return 'activa', 'Target alcanzado pero consistencia viola 50%% — dia max: $%.0f / total: $%.0f' % (max_dia, ganancia_total)

    return 'activa', ''


# ══════════════════════════════════════════════
# PROCESAMIENTO DE TRADE CERRADO
# ══════════════════════════════════════════════

def procesar_trade(cuenta_estado, trade, dia_str):
    ganancia = trade['ganancia']

    cuenta_estado['capital']        += ganancia
    cuenta_estado['ganancia_total'] += ganancia

    prev = cuenta_estado['ganancia_por_dia'].get(dia_str, 0)
    cuenta_estado['ganancia_por_dia'][dia_str] = round(prev + ganancia, 2)

    if trade['resultado'] == 'stop_loss':
        cuenta_estado['consecutivas_hoy'] += 1
    else:
        cuenta_estado['consecutivas_hoy'] = 0

    cuenta_estado['ultimo_dia'] = dia_str

    cuenta_estado['trades'].append({
        'dia':        dia_str,
        'estrategia': trade['estrategia'],
        'direccion':  trade['direccion'],
        'resultado':  trade['resultado'],
        'puntos':     trade['puntos'],
        'contratos':  trade['contratos'],
        'ganancia':   round(ganancia, 2),
        'capital':    round(cuenta_estado['capital'], 2),
    })

    nuevo_estado, razon = aplicar_reglas(cuenta_estado, dia_str)
    cuenta_estado['estado']    = nuevo_estado
    cuenta_estado['razon_fin'] = razon

    return cuenta_estado


# ══════════════════════════════════════════════
# REPORTE
# ══════════════════════════════════════════════

def generar_reporte(estado, dia_str, trades_cerrados_hoy):
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, 'reporte_%s.md' % dia_str)

    lineas = [
        '# Reporte LucidFlex — %s' % dia_str,
        '**Cuenta:** %s | **Plan:** $%s' % (CUENTA, PLAN['capital_inicial']),
        '',
    ]

    estrategias_reporte = ['ORB_LIVE'] if LIVE_MODE else ['ORB_SIM', 'ICT_SIM']
    for estrategia in estrategias_reporte:
        c = estado[estrategia]
        ganancia_total = c['ganancia_total']
        capital        = c['capital']
        n_trades       = len(c['trades'])
        estado_cuenta  = c['estado']
        target         = PLAN['profit_target']
        progreso       = ganancia_total / target * 100 if target else 0

        tp = sum(1 for t in c['trades'] if t['resultado'] == 'take_profit')
        sl = sum(1 for t in c['trades'] if t['resultado'] == 'stop_loss')
        wr = tp / n_trades * 100 if n_trades else 0

        icono = {'activa': '🟡', 'pasada': '✅', 'explotada': '❌'}.get(estado_cuenta, '❓')

        lineas += [
            '## %s Cuenta %s %s' % (icono, estrategia, estado_cuenta.upper()),
            '| | |',
            '|---|---|',
            '| Capital | $%.2f |' % capital,
            '| Ganancia total | $%+.2f |' % ganancia_total,
            '| Progreso target | %.1f%% ($%+.0f / $%+.0f) |' % (progreso, ganancia_total, target),
            '| Trades | %d (WR %.0f%%) |' % (n_trades, wr),
            '| TP / SL | %d / %d |' % (tp, sl),
        ]

        if c['razon_fin']:
            lineas.append('| Estado | %s |' % c['razon_fin'])

        pos = c.get('posicion_abierta')
        if pos:
            lineas += [
                '',
                '**Posicion abierta:**',
                '`%s %s | Entrada %.2f | SL %.2f | TP %.2f | %d contratos`' % (
                    pos['direccion'], pos['estrategia'],
                    pos['entrada'], pos['sl'], pos['tp'], pos['contratos']
                ),
            ]

        trade_hoy = trades_cerrados_hoy.get(estrategia)
        if trade_hoy:
            r = trade_hoy
            icono_r = {'take_profit': '✅', 'stop_loss': '❌', 'timeout': '⏱️'}.get(r['resultado'], '❓')
            lineas += [
                '',
                '**Trade cerrado hoy:**',
                '`%s %s | Entrada %.2f -> Salida %.2f | %d contratos | %+.1f pts | $%+.0f` %s' % (
                    r['direccion'], r['estrategia'],
                    r['entrada'], r['salida'],
                    r['contratos'], r['puntos'], r['ganancia'],
                    icono_r
                ),
            ]
        elif not pos:
            lineas += ['', '*Sin trade hoy.*']

        lineas.append('')

    claves = ['ORB_LIVE'] if LIVE_MODE else ['ORB_SIM', 'ICT_SIM']
    total_capital = sum(estado[k]['capital']        for k in claves if k in estado)
    total_gan     = sum(estado[k]['ganancia_total'] for k in claves if k in estado)

    lineas += [
        '---',
        '## Resumen combinado',
        '| Cuenta | Capital | Ganancia | Estado |',
        '|---|---|---|---|',
    ]
    for k in claves:
        if k in estado:
            lineas.append('| %s | $%.0f | $%+.0f | %s |' % (
                k, estado[k]['capital'], estado[k]['ganancia_total'], estado[k]['estado']))
    lineas += [
        '| **Total** | **$%.0f** | **$%+.0f** | |' % (total_capital, total_gan),
        '',
        '*Actualizado — %s*' % datetime.now(ET).strftime('%Y-%m-%d %H:%M ET'),
    ]

    with open(path, 'w') as f:
        f.write('\n'.join(lineas))

    print('Reporte guardado: %s' % path)
    return path


# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════

def main():
    ahora_et = datetime.now(ET)
    hoy      = ahora_et.date()
    dia_str  = str(hoy)

    print('\n' + '=' * 60)
    print('  LucidFlex Bot — %s %s ET — Cuenta %s' % (
        dia_str, ahora_et.strftime('%H:%M'), CUENTA))
    print('=' * 60)

    if hoy.weekday() >= 5:
        print('Fin de semana — sin operaciones.')
        return

    estado = cargar_estado()

    # Cerrar posiciones huerfanas de dias anteriores
    for est in list(estado.keys()):
        if (estado[est].get('posicion_abierta') and
                estado[est].get('ya_opero_hoy', '') < dia_str):
            print('[%s] Posicion de dia anterior detectada — limpiando.' % est)
            if LIVE_MODE and est == 'ORB_LIVE':
                try:
                    qty_real, dir_real = get_open_position()
                    if qty_real > 0:
                        print('[%s] Cerrando posicion real huerfana: %s x%d' % (est, dir_real, qty_real))
                        flatten_position(qty_real, dir_real)
                except Exception as _e:
                    print('[%s] Error al cerrar huerfana en Rithmic: %s' % (est, _e))
            estado[est]['posicion_abierta'] = None

    # Verificar posicion real en Rithmic aunque el estado local diga "sin posicion"
    # Protege contra crash entre submit_order y guardar_estado (evita doble entrada)
    if LIVE_MODE and not estado['ORB_LIVE'].get('posicion_abierta'):
        try:
            qty_real, dir_real = get_open_position()
            if qty_real > 0:
                print('[BOT] ALERTA: Rithmic tiene posicion abierta no registrada — bloqueando entradas hoy.')
                estado['ORB_LIVE']['ya_opero_hoy'] = dia_str
        except Exception as _e:
            print('[BOT] No se pudo verificar posicion en Rithmic al inicio: %s' % _e)

    # Descargar datos hasta ahora (solo velas completas)
    # 60d: ADX(14) necesita 2*14+2=30 dias de trading; 30d da ~29, insuficiente
    print('Descargando %s (60d, 1h)...' % TICKER)
    df = yf.download(TICKER, period='60d', interval='1h',
                     auto_adjust=True, progress=False)
    if hasattr(df.columns, 'levels'):
        df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
    df = df.dropna()
    if df.empty:
        print('Sin datos de yfinance — error de conexion o mercado cerrado.')
        guardar_estado(estado)
        return

    # Solo velas que cerraron hace al menos 30 min (velas completas)
    if df.index.tz is None:
        df.index = df.index.tz_localize('UTC')
    cutoff = ahora_et - timedelta(minutes=30)
    df_hasta_ahora = df[df.index.tz_convert(ET) <= cutoff]

    if df_hasta_ahora.empty:
        print('Sin datos completos aun (mercado recien abrio).')
        guardar_estado(estado)
        return

    print('  %d velas | ultima: %s ET' % (
        len(df_hasta_ahora),
        df_hasta_ahora.index[-1].tz_convert(ET).strftime('%H:%M')
    ))

    trades_cerrados_hoy = {}

    estrategias_activas = ['ORB_LIVE'] if LIVE_MODE else ['ORB_SIM', 'ICT_SIM']

    for estrategia in estrategias_activas:
        c = estado[estrategia]

        print('\n[%s] Estado: %s | Capital: $%.0f | Ganancia: $%+.0f' % (
            estrategia, c['estado'], c['capital'], c['ganancia_total']))

        if c['estado'] != 'activa':
            print('[%s] %s' % (estrategia, c['razon_fin']))
            continue

        # Resetear circuit breaker al inicio de cada nuevo dia
        if c.get('ya_opero_hoy', '') < dia_str:
            c['consecutivas_hoy'] = 0

        # Circuit breaker — para si hubo 2 perdidas consecutivas HOY
        if c['consecutivas_hoy'] >= MAX_CONSEC_PERDIDAS:
            print('[%s] Circuit breaker — %d perdidas consecutivas hoy.' % (estrategia, MAX_CONSEC_PERDIDAS))
            continue

        # ── EOD: cerrar posicion real si es tarde ──
        es_eod = (ahora_et.hour > CIERRE_HORA or
                  (ahora_et.hour == CIERRE_HORA and ahora_et.minute >= CIERRE_MIN))
        if LIVE_MODE and c.get('posicion_abierta') and es_eod:
            pos = c['posicion_abierta']
            qty_real, dir_real = get_open_position()
            if qty_real > 0:
                print('[%s] EOD — cerrando posicion real %s x%d' % (estrategia, dir_real, qty_real))
                flatten_position(qty_real, dir_real)

        # ── Gestionar posicion abierta ──
        if c.get('posicion_abierta'):
            trade_cerrado = gestionar_posicion(c['posicion_abierta'], df_hasta_ahora, hoy, force_eod=es_eod)
            if trade_cerrado:
                print('[%s] CERRADO: %s | %+.1f pts | $%+.0f' % (
                    estrategia, trade_cerrado['resultado'],
                    trade_cerrado['puntos'], trade_cerrado['ganancia']))
                estado[estrategia] = procesar_trade(c, trade_cerrado, dia_str)
                estado[estrategia]['posicion_abierta'] = None
                trades_cerrados_hoy[estrategia] = trade_cerrado
                continue  # buscar 2do trade en el proximo run (siguiente barra), no en esta misma vela
            else:
                pos = c['posicion_abierta']
                print('[%s] Posicion abierta — %s desde %.2f | SL %.2f | TP %.2f' % (
                    estrategia, pos['direccion'], pos['entrada'],
                    pos['sl'], pos['tp']))
                continue  # posicion todavia abierta — no buscar nueva entrada

        # ── Filtro de noticias (se chequea antes de cualquier entrada, incluso actividad forzada) ──
        hay_noticia, nombre_ev = check_noticia(hoy)
        if hay_noticia:
            print('[%s] Noticia alto impacto hoy (%s) — sin operaciones.' % (estrategia, nombre_ev))
            continue

        # ── Actividad mínima: si pasaron >= 28 días sin trade ──
        ultimo_trade = c.get('ultimo_dia', '')
        dias_sin_trade = (hoy - __import__('datetime').date.fromisoformat(ultimo_trade)).days if ultimo_trade else 0
        forzar_actividad = dias_sin_trade >= 28 and c.get('ya_opero_hoy') != dia_str

        if forzar_actividad:
            print('[%s] ACTIVIDAD FORZADA — %d dias sin trade — entrando con 1 contrato minimo.' % (estrategia, dias_sin_trade))
            entry = signal_actividad_minima(df_hasta_ahora, hoy)
            if entry:
                if LIVE_MODE:
                    order_id = submit_bracket_entry(entry)
                    if order_id is None:
                        print('[%s] Orden de actividad rechazada — no se reintenta hoy.' % estrategia)
                        estado[estrategia]['ya_opero_hoy'] = dia_str
                        estado[estrategia]['ultimo_dia']   = dia_str
                    else:
                        entry['order_id'] = order_id
                        estado[estrategia]['posicion_abierta'] = entry
                        estado[estrategia]['ya_opero_hoy']     = dia_str
                        estado[estrategia]['ultimo_dia']        = dia_str
                else:
                    estado[estrategia]['posicion_abierta'] = entry
                    estado[estrategia]['ya_opero_hoy']     = dia_str
                    estado[estrategia]['ultimo_dia']        = dia_str
            continue

        # ── Buscar entrada nueva (hasta 2 trades/día si el 1ro ya cerró) ──
        trades_hoy      = sum(1 for t in c['trades'] if t['dia'] == dia_str)
        puede_entrar    = (c.get('ya_opero_hoy') != dia_str or
                           (trades_hoy == 1 and c.get('posicion_abierta') is None))

        if puede_entrar:
            if estrategia in ('ORB_LIVE', 'ORB_SIM'):
                es_segundo = (trades_hoy == 1)
                entry, orb_sizes_nuevos, motivo = signal_orb_entry(
                    df_hasta_ahora, hoy, c['capital'], c['orb_sizes'],
                    force_contrato=es_segundo)
                # Solo actualizar orb_sizes una vez por dia — evita duplicados por multiples runs
                if c.get('orb_size_dia') != dia_str:
                    estado[estrategia]['orb_sizes']   = orb_sizes_nuevos[-20:]
                    estado[estrategia]['orb_size_dia'] = dia_str
                if entry:
                    if LIVE_MODE:
                        order_id = submit_bracket_entry(entry)
                        if order_id is None:
                            print('[ORB] Orden rechazada — no se registra posicion.')
                            continue
                        entry['order_id'] = order_id
                    estado[estrategia]['posicion_abierta'] = entry
                    estado[estrategia]['ya_opero_hoy']     = dia_str
                    estado[estrategia]['ultimo_dia']        = dia_str
                    print('[ORB] ENTRADA #%d: %s | %.2f | SL %.2f | TP %.2f | %d contratos' % (
                        trades_hoy + 1, entry['direccion'], entry['entrada'],
                        entry['sl'], entry['tp'], entry['contratos']))
                else:
                    print('[ORB] Sin senal — %s' % (motivo or 'sin setup'))

            else:
                obs_usados_set = set(c['obs_usados'])
                entry, obs_nuevos, motivo = signal_ict_entry(
                    df_hasta_ahora, hoy, c['capital'], obs_usados_set)
                estado[estrategia]['obs_usados'] = list(obs_nuevos)
                if entry:
                    estado[estrategia]['posicion_abierta'] = entry
                    estado[estrategia]['ya_opero_hoy']     = dia_str
                    estado[estrategia]['ultimo_dia']        = dia_str
                    print('[ICT] ENTRADA: %s | %.2f | SL %.2f | TP %.2f | %d contratos' % (
                        entry['direccion'], entry['entrada'],
                        entry['sl'], entry['tp'], entry['contratos']))
                else:
                    print('[ICT] Sin senal — %s' % (motivo or 'sin setup'))

        else:
            if trades_hoy >= 2:
                print('[%s] 2 trades hoy — esperando manana.' % estrategia)
            else:
                print('[%s] Ya opero hoy — esperando manana.' % estrategia)

    guardar_estado(estado)
    generar_reporte(estado, dia_str, trades_cerrados_hoy)

    print('\n' + '=' * 60)
    estrategias_resumen = ['ORB_LIVE'] if LIVE_MODE else ['ORB_SIM', 'ICT_SIM']
    total_gan_resumen = sum(estado[k]['ganancia_total'] for k in estrategias_resumen if k in estado)
    for k in estrategias_resumen:
        if k in estado:
            print('  %s: $%+.0f' % (k, estado[k]['ganancia_total']))
    print('  TOTAL: $%+.0f' % total_gan_resumen)
    print('=' * 60)


if __name__ == '__main__':
    import traceback
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        print('\n[BOT] Excepcion no manejada — guardando estado de emergencia.')
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            _et = ZoneInfo("America/New_York")
            _estado = cargar_estado()
            guardar_estado(_estado)
        except Exception:
            pass
