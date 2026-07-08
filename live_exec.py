"""
Ejecucion en vivo via async-rithmic.
Se activa cuando LIVE_MODE=true en el entorno.

Variables de entorno requeridas:
    RITHMIC_USER  — usuario Rithmic (ej. LT-ZW98EY91)
    RITHMIC_PASS  — contrasena Rithmic
"""

import asyncio
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from async_rithmic import RithmicClient, Gateway, OrderType, TimeBarType, TransactionType

from config import SYMBOL_LIVE, EXCHANGE_LIVE, RITHMIC_SYSTEM, TICK_SIZE

ET = ZoneInfo("America/New_York")


def _make_client():
    user = os.environ.get('RITHMIC_USER')
    pwd  = os.environ.get('RITHMIC_PASS')
    if not user or not pwd:
        raise RuntimeError('RITHMIC_USER y RITHMIC_PASS deben estar definidos.')
    return RithmicClient(
        user=user,
        password=pwd,
        system_name=RITHMIC_SYSTEM,
        app_name='LucidFlexBot',
        app_version='1.0',
        gateway=Gateway.CHICAGO,
    )


# ──────────────────────────────────────────────
# Enviar orden bracket (entrada + SL + TP)
# ──────────────────────────────────────────────

async def _submit_bracket_async(entry_signal):
    client = _make_client()
    await client.connect()

    direccion  = entry_signal['direccion']
    entrada    = entry_signal['entrada']
    sl         = entry_signal['sl']
    tp         = entry_signal['tp']
    contratos  = entry_signal['contratos']
    estrategia = entry_signal['estrategia']

    if direccion == 'LONG':
        tx_type      = TransactionType.BUY
        stop_ticks   = max(1, int(round((entrada - sl)  / TICK_SIZE)))
        target_ticks = max(1, int(round((tp  - entrada) / TICK_SIZE)))
    else:
        tx_type      = TransactionType.SELL
        stop_ticks   = max(1, int(round((sl  - entrada) / TICK_SIZE)))
        target_ticks = max(1, int(round((entrada - tp)  / TICK_SIZE)))

    order_id = 'LFB_%s_%s' % (estrategia, datetime.now(ET).strftime('%H%M%S'))

    print('[LIVE] Enviando bracket: %s %s x%d | SL %d ticks | TP %d ticks | id=%s' % (
        direccion, SYMBOL_LIVE, contratos, stop_ticks, target_ticks, order_id))

    result = await client.submit_order(
        order_id=order_id,
        symbol=SYMBOL_LIVE,
        exchange=EXCHANGE_LIVE,
        qty=contratos,
        transaction_type=tx_type,
        order_type=OrderType.MARKET,
        stop_ticks=stop_ticks,
        target_ticks=target_ticks,
    )

    # Validar que Rithmic aceptó la orden (rp_code=['0'] = éxito)
    # result=None significa timeout/sin respuesta — tratar como rechazo
    if result is None:
        print('[LIVE] ERROR: Rithmic devolvio None — orden NO enviada.')
        try:
            await client.disconnect()
        except Exception:
            pass
        return None
    resp = result[0] if (isinstance(result, list) and result) else result
    if resp is not None:
        rp = getattr(resp, 'rp_code', None)
        if rp is not None:
            rp_list = list(rp) if hasattr(rp, '__iter__') and not isinstance(rp, (str, bytes)) else [rp]
            if rp_list and rp_list != ['0']:
                print('[LIVE] ALERTA: Rithmic rechazó la orden — rp_code=%s' % rp_list)
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return None

    print('[LIVE] Orden aceptada: %s | result=%s' % (order_id, str(result)))
    try:
        await client.disconnect()
    except Exception:
        pass  # orden ya enviada — ignorar error de disconnect
    return order_id


# ──────────────────────────────────────────────
# Variante C — STOP de entrada en el nivel (en reposo) + bracket SL/TP
# ──────────────────────────────────────────────

ULTIMO_ERROR_C = None  # detalle del último fallo de orden C (se vuelca a estado.json para diagnóstico)


async def _submit_stop_bracket_async(direccion, trigger_price, sl, tp, contratos,
                                     cancel_at=None):
    """
    Coloca una orden STOP_MARKET de entrada en `trigger_price` con bracket SL/TP.
    cancel_at (datetime ET): si la orden no se llena, Rithmic la auto-cancela a esa hora.
    Devuelve order_id o None.
    """
    client = _make_client()
    await client.connect()

    if direccion == 'LONG':
        tx_type      = TransactionType.BUY
        stop_ticks   = max(1, int(round((trigger_price - sl) / TICK_SIZE)))
        target_ticks = max(1, int(round((tp - trigger_price) / TICK_SIZE)))
    else:
        tx_type      = TransactionType.SELL
        stop_ticks   = max(1, int(round((sl - trigger_price) / TICK_SIZE)))
        target_ticks = max(1, int(round((trigger_price - tp) / TICK_SIZE)))

    order_id = 'LFBC_%s_%s' % (direccion, datetime.now(ET).strftime('%H%M%S'))
    kwargs = dict(
        order_id=order_id, symbol=SYMBOL_LIVE, exchange=EXCHANGE_LIVE,
        qty=contratos, transaction_type=tx_type, order_type=OrderType.STOP_MARKET,
        trigger_price=round(trigger_price, 2), stop_ticks=stop_ticks, target_ticks=target_ticks,
    )
    if cancel_at is not None:
        kwargs['cancel_at'] = cancel_at

    result = await client.submit_order(**kwargs)

    # async_rithmic LANZA excepción si Rithmic devuelve un rp_code de error real
    # (ver base.py: raise si len(rp_code) y rp_code[0] != '0'). Si llegamos acá sin
    # excepción, la orden fue ACEPTADA. Para órdenes STOP-bracket el rp_code vuelve
    # vacío/anidado ([[]]) — NO es rechazo. Solo result=None es fallo real.
    if result is None:
        globals()['ULTIMO_ERROR_C'] = 'rithmic_none (%s)' % direccion
        print('[LIVE-C] ERROR: Rithmic devolvio None — stop %s NO enviado.' % direccion)
        try: await client.disconnect()
        except Exception: pass
        return None

    # Verificar que el stop REALMENTE quedó en Rithmic (no confiar solo en el ack —
    # el rp_code de las STOP-bracket es ambiguo). Match por user_tag == order_id.
    confirmado = None
    for intento in range(2):
        try:
            await asyncio.sleep(1.5)
            ordenes = await client.list_orders()
            confirmado = any(getattr(o, 'user_tag', '') == order_id for o in (ordenes or []))
            if confirmado:
                break
        except Exception as _e_chk:
            print('[LIVE-C] No se pudo verificar stop %s (%d/2): %s' % (direccion, intento + 1, _e_chk))
            confirmado = None
    if confirmado is False:
        globals()['ULTIMO_ERROR_C'] = 'no_aparece_en_list_orders (%s)' % direccion
        print('[LIVE-C] ALERTA: stop %s NO aparece en Rithmic tras submit — NO colocado.' % direccion)
        try: await client.disconnect()
        except Exception: pass
        return None
    estado_v = 'CONFIRMADO en Rithmic' if confirmado else 'enviado (verificación incierta)'
    print('[LIVE-C] Stop %s %s: %s @ trigger %.2f | SL %d tk | TP %d tk' % (
        direccion, estado_v, order_id, trigger_price, stop_ticks, target_ticks))
    try: await client.disconnect()
    except Exception: pass
    return order_id


async def _cancel_entry_orders_async(order_ids):
    """Cancela órdenes de entrada pendientes por order_id (las que aún no se llenaron)."""
    client = _make_client()
    await client.connect()
    canceladas = []
    try:
        for oid in order_ids:
            try:
                await client.cancel_order(order_id=oid)
                canceladas.append(oid)
                print('[LIVE-C] Stop pendiente cancelado: %s' % oid)
            except Exception as _e:
                print('[LIVE-C] No se pudo cancelar %s: %s' % (oid, _e))
    finally:
        try: await client.disconnect()
        except Exception: pass
    return canceladas


def submit_stop_bracket(direccion, trigger_price, sl, tp, contratos, cancel_at=None, dry_run=True):
    """
    API pública C. dry_run=True → imprime los parámetros SIN enviar (verificación segura).
    Devuelve order_id (real) o un id ficticio 'DRYRUN_...' en dry-run, o None si falla.
    """
    if direccion == 'LONG':
        st = max(1, int(round((trigger_price - sl) / TICK_SIZE)))
        tt = max(1, int(round((tp - trigger_price) / TICK_SIZE)))
        tx = 'BUY'
    else:
        st = max(1, int(round((sl - trigger_price) / TICK_SIZE)))
        tt = max(1, int(round((trigger_price - tp) / TICK_SIZE)))
        tx = 'SELL'
    print('[%s] STOP_MARKET %s %s x%d | trigger=%.2f | SL=%d tk (%.2f) | TP=%d tk (%.2f) | cancel_at=%s' % (
        'DRY-RUN' if dry_run else 'LIVE-C', tx, SYMBOL_LIVE, contratos, trigger_price,
        st, sl, tt, tp, cancel_at.strftime('%H:%M') if cancel_at else 'none'))
    if dry_run:
        return 'DRYRUN_%s_%s' % (direccion, datetime.now(ET).strftime('%H%M%S'))
    try:
        return asyncio.run(_submit_stop_bracket_async(direccion, trigger_price, sl, tp, contratos, cancel_at))
    except Exception as e:
        globals()['ULTIMO_ERROR_C'] = 'exc:%s (%s)' % (e, direccion)
        print('[LIVE-C] ERROR al enviar stop %s: %s' % (direccion, e))
        return None


def cancel_entry_orders(order_ids, dry_run=True):
    """Cancela los stops de entrada pendientes. dry_run → solo imprime."""
    order_ids = [o for o in (order_ids or []) if o and not str(o).startswith('DRYRUN_')]
    if not order_ids:
        return []
    if dry_run:
        print('[DRY-RUN] cancelaría stops de entrada: %s' % order_ids)
        return order_ids
    try:
        return asyncio.run(_cancel_entry_orders_async(order_ids))
    except Exception as e:
        print('[LIVE-C] ERROR al cancelar stops: %s' % e)
        return []


async def _cancelar_todo_pendiente_async():
    """Cancela TODAS las órdenes pendientes (working/pre-submitted) en SYMBOL_LIVE.
    Se usa para limpiar órdenes huérfanas (ej. patas de bracket que quedaron colgadas)."""
    client = _make_client()
    await client.connect()
    canceladas = 0
    try:
        ordenes = await client.list_orders()
        for orden in (ordenes or []):
            if getattr(orden, 'symbol', '') != SYMBOL_LIVE:
                continue
            bid = getattr(orden, 'basket_id', None)
            if bid:
                try:
                    await client.cancel_order(basket_id=bid)
                    canceladas += 1
                    print('[LIVE-C] Orden huérfana cancelada: basket_id=%s' % bid)
                except Exception as _e:
                    print('[LIVE-C] No se pudo cancelar %s: %s' % (bid, _e))
    finally:
        try: await client.disconnect()
        except Exception: pass
    return canceladas


def cancelar_todo_pendiente(dry_run=True):
    """Limpia órdenes pendientes huérfanas en SYMBOL_LIVE. dry_run → solo imprime."""
    if dry_run:
        print('[DRY-RUN] limpiaría órdenes pendientes huérfanas en %s' % SYMBOL_LIVE)
        return 0
    try:
        return asyncio.run(_cancelar_todo_pendiente_async())
    except Exception as e:
        print('[LIVE-C] ERROR al limpiar pendientes: %s' % e)
        return 0


async def _get_position_async():
    """Devuelve (qty_neta, direccion, avg_open_fill_price) de la posicion abierta en SYMBOL_LIVE."""
    client = _make_client()
    await client.connect()

    positions = await client.list_positions()
    try:
        await client.disconnect()
    except Exception:
        pass
    for pos in (positions or []):
        sym = getattr(pos, 'symbol', '')
        if sym != SYMBOL_LIVE:
            continue
        long_qty  = getattr(pos, 'buy_qty',  0) or 0
        short_qty = getattr(pos, 'sell_qty', 0) or 0
        fill_price = getattr(pos, 'avg_open_fill_price', None) or 0
        net = long_qty - short_qty
        if net > 0:
            return net, 'LONG', float(fill_price)
        if net < 0:
            return abs(net), 'SHORT', float(fill_price)

    return 0, None, 0


async def _flatten_async(qty, direccion):
    client = _make_client()
    await client.connect()
    try:
        # 1. Cancelar legs SL/TP pendientes del bracket
        try:
            ordenes = await client.list_orders()
            for orden in (ordenes or []):
                if getattr(orden, 'symbol', '') != SYMBOL_LIVE:
                    continue
                basket_id = getattr(orden, 'basket_id', None)
                if basket_id:
                    try:
                        await client.cancel_order(basket_id=basket_id)
                        print('[LIVE] Orden pendiente cancelada: basket_id=%s' % basket_id)
                    except Exception as _e_cancel:
                        print('[LIVE] No se pudo cancelar orden %s: %s' % (basket_id, _e_cancel))
        except Exception as _e_list:
            print('[LIVE] Advertencia al listar ordenes abiertas: %s' % _e_list)

        # 2. Cerrar la posicion con market order
        tx_type  = TransactionType.SELL if direccion == 'LONG' else TransactionType.BUY
        order_id = 'LFB_CLOSE_%s' % datetime.now(ET).strftime('%H%M%S%f')[:-3]
        print('[LIVE] Cerrando posicion: %s x%d -> %s' % (direccion, qty, order_id))
        result_close = await client.submit_order(
            order_id=order_id,
            symbol=SYMBOL_LIVE,
            exchange=EXCHANGE_LIVE,
            qty=qty,
            transaction_type=tx_type,
            order_type=OrderType.MARKET,
        )

        # Verificar rp_code de la respuesta inmediata
        # result_close=None significa timeout — la orden de cierre NO llegó a Rithmic
        if result_close is None:
            raise RuntimeError('submit_order de cierre devolvio None — orden NO enviada')
        resp_c = result_close[0] if (isinstance(result_close, list) and result_close) else result_close
        if resp_c is not None:
            rp_c = getattr(resp_c, 'rp_code', None)
            if rp_c is not None:
                rp_c_list = list(rp_c) if hasattr(rp_c, '__iter__') and not isinstance(rp_c, (str, bytes)) else [rp_c]
                if rp_c_list and rp_c_list != ['0']:
                    raise RuntimeError('Rithmic rechazo el cierre — rp_code=%s' % rp_c_list)

        # 3. VERIFICAR que la posicion realmente esta flat
        # Reintenta hasta 3 veces con 4s de espera — Rithmic puede tardar en actualizar,
        # especialmente cerca del cierre del viernes (5pm ET) o si hay latencia.
        # CRITICO: si list_positions() devuelve None/vacio no se puede confirmar flat
        # (podria ser un timeout de API, no que la posicion se cerro). En ese caso
        # se reintenta; si tras 3 intentos sigue vacio, se lanza error para forzar
        # reconciliacion manual en lugar de marcar la posicion como cerrada en falso.
        for intento in range(3):
            await asyncio.sleep(4)
            positions = await client.list_positions()
            if positions is None:
                print('[LIVE] Verificacion cierre intento %d/3: list_positions() devolvio None — reintentando.' % (intento + 1))
                continue
            aun_abierta = False
            for pos in positions:
                if getattr(pos, 'symbol', '') != SYMBOL_LIVE:
                    continue
                long_qty  = getattr(pos, 'buy_qty',  0) or 0
                short_qty = getattr(pos, 'sell_qty', 0) or 0
                net = long_qty - short_qty
                if net != 0:
                    aun_abierta = True
                    break
            if aun_abierta:
                raise RuntimeError('Posicion sigue abierta despues del cierre (intento %d/3)' % (intento + 1))
            # list_positions() devolvio lista valida (aunque vacia) y no encontro posicion abierta
            print('[LIVE] Cierre confirmado FLAT (intento %d/3): %s' % (intento + 1, order_id))
            return True

        raise RuntimeError('No se pudo verificar cierre — list_positions() devolvio None en 3 intentos. Reconciliar manualmente.')
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


# ──────────────────────────────────────────────
# Barras historicas 1h
# ──────────────────────────────────────────────

async def _fetch_bars_async(num_trading_days):
    from datetime import timezone
    client = _make_client()
    await client.connect()

    end_time   = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=num_trading_days)

    bars = await client.get_historical_time_bars(
        symbol=SYMBOL_LIVE,
        exchange=EXCHANGE_LIVE,
        start_time=start_time,
        end_time=end_time,
        bar_type=TimeBarType.MINUTE_BAR,
        bar_type_periods=60,
    )
    try:
        await client.disconnect()
    except Exception:
        pass
    return bars


def fetch_historical_bars(num_calendar_days=90):
    """Descarga velas 1h de Rithmic (90 dias calendario ≈ 63 trading days). Devuelve DataFrame ET o None."""
    try:
        bars = asyncio.run(_fetch_bars_async(num_calendar_days))
    except Exception as e:
        print('[LIVE] ERROR al descargar barras historicas: %s' % e)
        return None

    if not bars:
        print('[LIVE] Rithmic devolvio 0 barras historicas.')
        return None

    rows = []
    for bar in bars:
        marker = bar.get('marker')
        if not marker:
            continue
        ts = datetime.fromtimestamp(marker, tz=ET)
        rows.append({
            'datetime': ts,
            'Open':   float(bar.get('open_price',  0) or 0),
            'High':   float(bar.get('high_price',  0) or 0),
            'Low':    float(bar.get('low_price',   0) or 0),
            'Close':  float(bar.get('close_price', 0) or 0),
            'Volume': int(bar.get('volume',        0) or 0),
        })

    if not rows:
        return None

    df = pd.DataFrame(rows).set_index('datetime')
    df = df[~df.index.duplicated(keep='last')].sort_index()
    df = df[df['Close'] > 0]
    # Rithmic etiqueta END time → convertir a start-time
    df.index = df.index - pd.Timedelta(hours=1)
    print('[LIVE] Historico Rithmic: %d barras | %s → %s ET' % (
        len(df), df.index[0].strftime('%Y-%m-%d'), df.index[-1].strftime('%Y-%m-%d')))
    return df


async def _fetch_today_bars_async():
    from datetime import timezone
    client = _make_client()
    await client.connect()

    end_time   = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=28)  # desde ayer para capturar sesion completa

    bars = await client.get_historical_time_bars(
        symbol=SYMBOL_LIVE,
        exchange=EXCHANGE_LIVE,
        start_time=start_time,
        end_time=end_time,
        bar_type=TimeBarType.MINUTE_BAR,
        bar_type_periods=60,
    )
    try:
        await client.disconnect()
    except Exception:
        pass
    return bars


def fetch_today_bars():
    """Descarga barras de las ultimas 28h desde Rithmic (sesion de hoy). Devuelve DataFrame ET o None."""
    try:
        bars = asyncio.run(_fetch_today_bars_async())
    except Exception as e:
        print('[LIVE] ERROR al descargar barras de hoy: %s' % e)
        return None

    if not bars:
        print('[LIVE] Rithmic devolvio 0 barras de hoy.')
        return None

    rows = []
    for bar in bars:
        marker = bar.get('marker')
        if not marker:
            continue
        ts = datetime.fromtimestamp(marker, tz=ET)
        rows.append({
            'datetime': ts,
            'Open':   float(bar.get('open_price',  0) or 0),
            'High':   float(bar.get('high_price',  0) or 0),
            'Low':    float(bar.get('low_price',   0) or 0),
            'Close':  float(bar.get('close_price', 0) or 0),
            'Volume': int(bar.get('volume',        0) or 0),
        })

    if not rows:
        return None

    df = pd.DataFrame(rows).set_index('datetime')
    df = df[~df.index.duplicated(keep='last')].sort_index()
    df = df[df['Close'] > 0]
    # Rithmic etiqueta END time → convertir a start-time
    df.index = df.index - pd.Timedelta(hours=1)
    return df


# ──────────────────────────────────────────────
# API publica (sincrona)
# ──────────────────────────────────────────────

def submit_bracket_entry(entry_signal):
    """Envia bracket order. Devuelve order_id o None si falla."""
    try:
        return asyncio.run(_submit_bracket_async(entry_signal))
    except Exception as e:
        print('[LIVE] ERROR al enviar orden: %s' % e)
        return None


def get_open_position():
    """Devuelve (qty, direccion, fill_price) o (0, None, 0) si flat."""
    try:
        return asyncio.run(_get_position_async())
    except Exception as e:
        print('[LIVE] ERROR al consultar posicion: %s' % e)
        return 0, None, 0


def flatten_position(qty, direccion):
    try:
        return asyncio.run(_flatten_async(qty, direccion))
    except Exception as e:
        print('[LIVE] ERROR CRITICO al cerrar posicion: %s' % e)
        return False
