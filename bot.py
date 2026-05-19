"""
LucidFlex Funded Bot — Motor Intraday
======================================
Corre cada hora durante el mercado (10:35am–4:35pm ET, vía GitHub Actions).
Entra en el cierre de cada vela 1h y gestiona posiciones abiertas en tiempo real.
Dos cuentas independientes: ORB+VWAP e ICT Order Blocks.
"""

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

from config import (CUENTA, PLANES, TICKER, SYMBOL_LIVE, ESTADO_FILE, REPORTS_DIR,
                    MAX_CONSEC_PERDIDAS, CIERRE_HORA, CIERRE_MIN)
from estrategias import signal_orb_entry, signal_ict_entry, gestionar_posicion, signal_actividad_minima
from filtro_noticias import check_noticia

# LIVE_MODE=true → ejecuta ordenes reales via Rithmic
LIVE_MODE = os.environ.get('LIVE_MODE', 'false').lower() == 'true'
if LIVE_MODE:
    try:
        from live_exec import submit_bracket_entry, get_open_position, flatten_position, fetch_historical_bars
    except Exception as _import_err:
        print('[BOT] ERROR importando live_exec: %s' % _import_err)
        print('[BOT] Continuando en modo simulacion.')
        LIVE_MODE = False

ET   = ZoneInfo("America/New_York")
PLAN = PLANES[CUENTA]

_estado_emergencia = None  # referencia al estado en memoria para el handler de crash


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
        'posicion_abierta':   None,  # posicion open intraday
        'ya_opero_hoy':       '',   # fecha del ultimo trade del dia
        'peak_capital':       PLAN['capital_inicial'],  # trailing drawdown EOD
        'wins_seguidos':      0,    # anti-martingale: wins consecutivos globales
        'entradas_bloqueadas': '',  # fecha en que se bloquearon entradas (Rithmic safety)
        'orb_size_dia':        '',  # fecha de la ultima actualizacion de orb_sizes
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
    dir_path = os.path.dirname(ESTADO_FILE)
    os.makedirs(dir_path, exist_ok=True)
    # Write atomico: si el proceso muere durante json.dump el archivo original queda intacto.
    # os.replace() es atomico en todos los SO modernos — evita JSON corrupto que paraliza el bot.
    with tempfile.NamedTemporaryFile('w', dir=dir_path, suffix='.tmp', delete=False) as tmp:
        json.dump(estado, tmp, indent=2, default=str)
        tmp_path = tmp.name
    os.replace(tmp_path, ESTADO_FILE)


# ══════════════════════════════════════════════
# REGLAS LUCIDFLEX
# ══════════════════════════════════════════════

def aplicar_reglas(cuenta_estado, dia_str):
    capital        = cuenta_estado['capital']
    ganancia_total = cuenta_estado['ganancia_total']
    gan_por_dia    = cuenta_estado['ganancia_por_dia']
    trades         = cuenta_estado['trades']

    # Trailing drawdown EOD — regla LucidFlex Flex Eval
    # Fase 1 (trailing): MLL = peak_EOD - max_drawdown. Sube con cada nuevo maximo EOD.
    # Fase 2 (lock):     cuando peak >= capital_inicial + max_drawdown ($26,000),
    #                    MLL se congela en capital_inicial ($25,000) para siempre.
    capital_inicial = PLAN['capital_inicial']
    itb             = capital_inicial + PLAN['max_drawdown']   # $26,000: lock trigger
    mll_fijo        = capital_inicial                          # $25,000: MLL congelado

    peak = cuenta_estado.get('peak_capital', capital_inicial)  # actualizado EOD en bot.py

    if peak >= itb:
        limite_drawdown = mll_fijo   # MLL congelado en capital_inicial
    else:
        limite_drawdown = peak - PLAN['max_drawdown']   # MLL trailing

    if capital <= limite_drawdown:
        return 'explotada', 'Drawdown alcanzado — capital: $%.0f | limite: $%.0f' % (capital, limite_drawdown)

    if ganancia_total >= PLAN['profit_target']:
        max_dia         = max(gan_por_dia.values()) if gan_por_dia else 0
        dias_op         = len(gan_por_dia)
        # Usar suma del dict para evitar drift de float entre ganancia_total y gan_por_dia
        total_check     = sum(gan_por_dia.values()) if gan_por_dia else ganancia_total
        consistencia_ok = total_check > 0 and max_dia / total_check <= 0.50

        if consistencia_ok:
            return 'pasada', 'Target alcanzado: $%+.0f en %d dias / %d trades' % (ganancia_total, dias_op, len(trades))
        return 'activa', 'Target alcanzado pero consistencia viola 50%% — dia max: $%.0f / total: $%.0f' % (max_dia, total_check)

    return 'activa', ''


# ══════════════════════════════════════════════
# PROCESAMIENTO DE TRADE CERRADO
# ══════════════════════════════════════════════

def procesar_trade(cuenta_estado, trade, dia_str):
    ganancia = trade['ganancia']

    cuenta_estado['capital']        = round(cuenta_estado['capital']        + ganancia, 2)
    cuenta_estado['ganancia_total'] = round(cuenta_estado['ganancia_total'] + ganancia, 2)

    prev = cuenta_estado['ganancia_por_dia'].get(dia_str, 0)
    cuenta_estado['ganancia_por_dia'][dia_str] = round(prev + ganancia, 2)

    if trade['resultado'] == 'take_profit':
        cuenta_estado['consecutivas_hoy']  = 0
        cuenta_estado['wins_seguidos']      = cuenta_estado.get('wins_seguidos', 0) + 1
    elif trade['resultado'] == 'stop_loss':
        cuenta_estado['consecutivas_hoy'] += 1
        cuenta_estado['wins_seguidos']     = 0
    else:  # timeout
        cuenta_estado['consecutivas_hoy']  = 0
        cuenta_estado['wins_seguidos']     = 0

    cuenta_estado['ultimo_dia'] = dia_str

    # Trailing intraday: actualizar peak despues de cada trade (solo sube — mas conservador)
    cuenta_estado['peak_capital'] = max(
        cuenta_estado.get('peak_capital', PLAN['capital_inicial']),
        cuenta_estado['capital']
    )

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

    global _estado_emergencia
    estado = cargar_estado()
    _estado_emergencia = estado  # mismo objeto — mutations visibles desde el handler

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
                        ok = flatten_position(qty_real, dir_real)
                        if not ok:
                            print('[%s] FATAL: no se pudo cerrar huerfana — bloqueando entradas hoy.' % est)
                            estado[est]['entradas_bloqueadas'] = dia_str
                            continue
                    else:
                        # DISEÑO: Rithmic flat + local tiene posicion = bracket cerro entre runs
                        # (tipicamente SL/TP disparado el fin de semana o durante la noche).
                        # No limpiar posicion_abierta aqui — el reconciliador (abajo) detecta
                        # qty_real==0/local_pos!=None y setea _force_gestionar=True para que
                        # gestionar_posicion registre el P&L aproximado con el ultimo close.
                        # Si limpiamos aqui, el capital queda incorrecto sin P&L registrado.
                        print('[%s] Bracket cerro en Rithmic — reconciliador registrara P&L aproximado.' % est)
                        continue
                except Exception as _e:
                    print('[%s] Error al cerrar huerfana en Rithmic: %s' % (est, _e))
                    estado[est]['entradas_bloqueadas'] = dia_str
                    continue
            estado[est]['posicion_abierta'] = None

    # Reconciliacion completa Rithmic <-> estado local
    if LIVE_MODE:
        try:
            qty_real, dir_real = get_open_position()
            local_pos = estado['ORB_LIVE'].get('posicion_abierta')

            if qty_real > 0 and not local_pos:
                # Rithmic tiene una posicion que el estado local desconoce.
                # No sabemos su SL/TP (puede ser un bracket huerfano sin gestion) — la cerramos
                # de inmediato y bloqueamos entradas hoy para no operar en estado inconsistente.
                print('[BOT] ALERTA: Rithmic tiene posicion no registrada (%s x%d) — cerrando ahora.' % (
                    dir_real, qty_real))
                try:
                    ok_close = flatten_position(qty_real, dir_real)
                    if ok_close:
                        print('[BOT] Posicion desconocida cerrada — bloqueando entradas hoy por seguridad.')
                    else:
                        print('[BOT] FATAL: no se pudo cerrar posicion desconocida — revisar manualmente en Rithmic.')
                except Exception as _e_close:
                    print('[BOT] Error al intentar cerrar posicion desconocida: %s' % _e_close)
                estado['ORB_LIVE']['ya_opero_hoy']        = dia_str
                estado['ORB_LIVE']['entradas_bloqueadas'] = dia_str
            elif qty_real > 0 and local_pos:
                if (local_pos.get('direccion') != dir_real or
                        local_pos.get('contratos') != qty_real):
                    # Local y Rithmic no coinciden en direccion/cantidad — estado ambiguo.
                    # Cerramos la posicion remota (la "real") para evitar exposicion sin gestion correcta.
                    print('[BOT] ALERTA: desincronizado — local %s x%d vs Rithmic %s x%d — cerrando posicion remota.' % (
                        local_pos.get('direccion'), local_pos.get('contratos'),
                        dir_real, qty_real))
                    try:
                        ok_close = flatten_position(qty_real, dir_real)
                        if ok_close:
                            estado['ORB_LIVE']['posicion_abierta'] = None
                            print('[BOT] Cierre de desincronizada confirmado — estado local limpiado.')
                        else:
                            print('[BOT] FATAL: no se pudo cerrar posicion desincronizada.')
                    except Exception as _e_close:
                        print('[BOT] Error al intentar cerrar posicion desincronizada: %s' % _e_close)
                    estado['ORB_LIVE']['entradas_bloqueadas'] = dia_str
                    # DISEÑO: ya_opero_hoy NO se setea aqui a proposito — no hubo trade real
                    # (la posicion desincronizada se cerro, no se proceso P&L).
                    # El reset del circuit breaker (L431) se ejecuta de todas formas si
                    # ya_opero_hoy < dia_str, pero es inofensivo: entradas_bloqueadas impide entrar.
            elif qty_real == 0 and local_pos:
                # Rithmic flat pero estado local tiene posicion — el bracket cerro entre runs.
                # Marcar para cerrar inmediatamente en el loop de estrategias con force_eod=True:
                # el SL/TP real de Rithmic puede diferir del calculado (slippage en fill MKT),
                # por lo que gestionar_posicion podria no detectarlo con bar data normal.
                # force_eod garantiza el cierre y registro del P&L en este mismo run.
                print('[BOT] ALERTA: posicion local existe pero Rithmic flat — cerrando localmente ahora.')
                estado['ORB_LIVE']['_force_gestionar'] = True
        except Exception as _e:
            print('[BOT] No se pudo verificar posicion en Rithmic al inicio: %s' % _e)

    # Descargar datos hasta ahora (solo velas completas)
    # 60d: ADX(14) necesita 2*14+2=30 dias de trading; 30d da ~29, insuficiente
    cutoff = ahora_et - timedelta(minutes=30)
    if LIVE_MODE:
        print('Descargando %s (60d, 1h) desde Rithmic...' % SYMBOL_LIVE)
        df_hasta_ahora = fetch_historical_bars(num_trading_days=60)
        if df_hasta_ahora is None or df_hasta_ahora.empty:
            print('Sin datos de Rithmic — abortando.')
            guardar_estado(estado)
            return
        df_hasta_ahora = df_hasta_ahora[df_hasta_ahora.index <= cutoff]
    else:
        print('Descargando %s (60d, 1h) desde yfinance...' % TICKER)
        df = yf.download(TICKER, period='60d', interval='1h',
                         auto_adjust=True, progress=False)
        if hasattr(df.columns, 'levels'):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df = df.dropna()
        if df.empty:
            print('Sin datos de yfinance — error de conexion o mercado cerrado.')
            guardar_estado(estado)
            return
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        df_hasta_ahora = df[df.index.tz_convert(ET) <= cutoff].tz_convert(ET)

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
        # Pop temprano para que no se persista en estado.json aunque el loop salga por continue
        _force_gestionar = estado[estrategia].pop('_force_gestionar', False)

        print('\n[%s] Estado: %s | Capital: $%.0f | Ganancia: $%+.0f' % (
            estrategia, c['estado'], c['capital'], c['ganancia_total']))

        if c['estado'] != 'activa':
            print('[%s] %s' % (estrategia, c['razon_fin']))
            continue

        # Resetear circuit breaker y actualizar peak EOD al inicio de cada nuevo dia
        if c.get('ya_opero_hoy', '') < dia_str:
            c['consecutivas_hoy'] = 0
            # Peak trail actualiza EOD: una vez por dia, con el balance de cierre del dia anterior
            c['peak_capital'] = max(c.get('peak_capital', PLAN['capital_inicial']), c['capital'])

        # Circuit breaker — para si hubo 2 perdidas consecutivas HOY
        if c['consecutivas_hoy'] >= MAX_CONSEC_PERDIDAS:
            print('[%s] Circuit breaker — %d perdidas consecutivas hoy.' % (estrategia, MAX_CONSEC_PERDIDAS))
            continue

        # ── EOD: cerrar posicion real si es tarde ──
        es_eod = (ahora_et.hour > CIERRE_HORA or
                  (ahora_et.hour == CIERRE_HORA and ahora_et.minute >= CIERRE_MIN))
        if LIVE_MODE and c.get('posicion_abierta') and es_eod:
            cierre_eod_ok = True
            try:
                qty_real, dir_real = get_open_position()
                if qty_real > 0:
                    print('[%s] EOD — cerrando posicion real %s x%d' % (estrategia, dir_real, qty_real))
                    cierre_eod_ok = flatten_position(qty_real, dir_real)
                    if not cierre_eod_ok:
                        print('[%s] FATAL: cierre EOD no confirmado en Rithmic — NO se procesa trade local.' % estrategia)
                        estado[estrategia]['entradas_bloqueadas'] = dia_str
                        guardar_estado(estado)
                        continue
            except Exception as _e_eod:
                print('[%s] Error al cerrar posicion EOD en Rithmic: %s' % (estrategia, _e_eod))
                estado[estrategia]['entradas_bloqueadas'] = dia_str
                guardar_estado(estado)
                continue

        # ── Gestionar posicion abierta ──
        if c.get('posicion_abierta'):
            trade_cerrado = gestionar_posicion(c['posicion_abierta'], df_hasta_ahora, hoy, force_eod=(es_eod or _force_gestionar))
            if trade_cerrado:
                print('[%s] CERRADO: %s | %+.1f pts | $%+.0f' % (
                    estrategia, trade_cerrado['resultado'],
                    trade_cerrado['puntos'], trade_cerrado['ganancia']))
                estado[estrategia] = procesar_trade(c, trade_cerrado, dia_str)
                estado[estrategia]['posicion_abierta'] = None
                estado[estrategia]['ya_opero_hoy']     = dia_str   # evita reset del circuit breaker en runs siguientes
                trades_cerrados_hoy[estrategia] = trade_cerrado
                continue  # buscar 2do trade en el proximo run (siguiente barra), no en esta misma vela
            else:
                _pos = c['posicion_abierta']
                print('[%s] Posicion abierta — %s desde %.2f | SL %.2f | TP %.2f' % (
                    estrategia, _pos['direccion'], _pos['entrada'],
                    _pos['sl'], _pos['tp']))
                continue  # posicion todavia abierta — no buscar nueva entrada

        # ── Actividad mínima: si pasaron >= 28 días sin trade ──
        # Cuenta nueva (ultimo_dia vacio) -> dias_sin_trade=0 para que NO dispare
        # forced activity en el primer run. Si fuera 999, una cuenta recien creada
        # dispararia un trade artificial el primer dia (bug reproducido el 15-may-2026,
        # commit 6fbb33e fixeo, b383d8f lo regreso, este es el re-fix).
        ultimo_trade = c.get('ultimo_dia', '')
        dias_sin_trade = (hoy - date.fromisoformat(ultimo_trade)).days if ultimo_trade else 0
        forzar_actividad = dias_sin_trade >= 28 and c.get('ya_opero_hoy') != dia_str

        if not forzar_actividad:
            hay_noticia, nombre_ev = check_noticia(hoy)
            if hay_noticia:
                print('[%s] Noticia alto impacto hoy (%s) — sin operaciones.' % (estrategia, nombre_ev))
                continue
        else:
            print('[%s] ACTIVIDAD FORZADA prioritaria — ignorando filtro de noticias.' % estrategia)

        if forzar_actividad:
            if es_eod:
                # DISEÑO: no abrir posicion pasado el EOD (16:30 ET).
                # signal_actividad_minima solo filtra barras >= 16:30, pero el bot
                # igual mandaria la orden a Rithmic y la posicion quedaria abierta
                # hasta el run de las 17:35 ET — riesgo innecesario.
                # El run siguiente (manana) vuelve a intentar si sigue activo el flag.
                print('[%s] ACTIVIDAD FORZADA — bloqueada porque es EOD (>= 16:30 ET).' % estrategia)
                continue
            print('[%s] ACTIVIDAD FORZADA — %d dias sin trade — entrando con 1 contrato minimo.' % (estrategia, dias_sin_trade))
            entry = signal_actividad_minima(df_hasta_ahora, hoy)
            if entry:
                if LIVE_MODE:
                    order_id = submit_bracket_entry(entry)
                    if order_id is None:
                        print('[%s] Orden de actividad rechazada — no se reintenta hoy.' % estrategia)
                        # DISEÑO: ya_opero_hoy se setea para no reintentar HOY,
                        # pero ultimo_dia NO se toca — el reloj de 28 dias no resetea
                        # hasta que haya un trade real. Consistente con el path ORB
                        # (linea ~554) donde una orden rechazada tampoco actualiza ultimo_dia.
                        estado[estrategia]['ya_opero_hoy'] = dia_str
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

        # ── Buscar entrada nueva (hasta 3 trades/día si el anterior ya cerró) ──
        trades_hoy      = sum(1 for t in c['trades'] if t['dia'] == dia_str)
        bloqueado       = c.get('entradas_bloqueadas') == dia_str
        puede_entrar    = (trades_hoy < 3 and c.get('posicion_abierta') is None and not bloqueado)

        if puede_entrar:
            if estrategia in ('ORB_LIVE', 'ORB_SIM'):
                es_segundo   = (trades_hoy >= 1)   # fuerza 1 contrato en trade 2 y 3 si ORB es grande
                ws           = c.get('wins_seguidos', 0)
                riesgo_activo = 0.015 if ws >= 2 else None   # anti-martingale
                entry, orb_sizes_nuevos, motivo = signal_orb_entry(
                    df_hasta_ahora, hoy, c['capital'], c['orb_sizes'],
                    force_contrato=es_segundo, riesgo_pct=riesgo_activo)
                # Actualizar orb_sizes solo cuando signal_orb_entry computó el ORB (append ocurrió)
                # y solo una vez por dia — evita duplicados y evita que un early-return (sin datos
                # suficientes aun) bloquee el guard el resto del dia sin haber grabado ningun ORB.
                orb_fue_computado = len(orb_sizes_nuevos) > len(c.get('orb_sizes', []))
                if orb_fue_computado and c.get('orb_size_dia') != dia_str:
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
            if bloqueado:
                print('[%s] Entradas bloqueadas hoy (posicion Rithmic no registrada).' % estrategia)
            else:
                print('[%s] 3 trades hoy — esperando manana.' % estrategia)

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
            # Usar el estado en memoria si ya fue cargado — captura cambios previos al crash
            _save = _estado_emergencia if _estado_emergencia is not None else cargar_estado()
            guardar_estado(_save)
        except Exception:
            pass
