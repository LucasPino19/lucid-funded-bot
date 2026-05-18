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


async def _get_position_async():
    """Devuelve (qty_neta, direccion) de la posicion abierta en SYMBOL_LIVE."""
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
        net = long_qty - short_qty
        if net > 0:
            return net, 'LONG'
        if net < 0:
            return abs(net), 'SHORT'

    return 0, None


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
        await asyncio.sleep(2)
        positions = await client.list_positions()
        for pos in (positions or []):
            if getattr(pos, 'symbol', '') != SYMBOL_LIVE:
                continue
            long_qty  = getattr(pos, 'buy_qty',  0) or 0
            short_qty = getattr(pos, 'sell_qty', 0) or 0
            net = long_qty - short_qty
            if net != 0:
                raise RuntimeError('Posicion sigue abierta despues del cierre: net=%d' % net)

        print('[LIVE] Cierre confirmado FLAT: %s' % order_id)
        return True
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


def fetch_historical_bars(num_trading_days=60):
    """Descarga velas 1h de Rithmic. Devuelve DataFrame OHLCV con DatetimeIndex ET, o None si falla."""
    try:
        bars = asyncio.run(_fetch_bars_async(num_trading_days))
    except Exception as e:
        print('[LIVE] ERROR al descargar barras historicas: %s' % e)
        return None

    if not bars:
        print('[LIVE] Rithmic devolvio 0 barras.')
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
    # Rithmic etiqueta cada barra con su END time; yfinance usa START time.
    # estrategias.py espera start-time → restamos la duracion de la barra (1h).
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
    """Devuelve (qty, direccion) o (0, None) si flat."""
    try:
        return asyncio.run(_get_position_async())
    except Exception as e:
        print('[LIVE] ERROR al consultar posicion: %s' % e)
        return 0, None


def flatten_position(qty, direccion):
    try:
        return asyncio.run(_flatten_async(qty, direccion))
    except Exception as e:
        print('[LIVE] ERROR CRITICO al cerrar posicion: %s' % e)
        return False
