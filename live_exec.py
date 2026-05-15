"""
Ejecucion en vivo via async-rithmic.
Se activa cuando LIVE_MODE=true en el entorno.

Variables de entorno requeridas:
    RITHMIC_USER  — usuario Rithmic (ej. LT-ZW98EY91)
    RITHMIC_PASS  — contrasena Rithmic
"""

import asyncio
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from async_rithmic import RithmicClient, Gateway, OrderType, TransactionType

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

    print('[LIVE] Orden enviada: %s' % str(result))
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
    for pos in positions:
        sym = getattr(pos, 'symbol', '')
        if sym != SYMBOL_LIVE:
            continue
        long_qty  = getattr(pos, 'open_long_qty',  0) or 0
        short_qty = getattr(pos, 'open_short_qty', 0) or 0
        net = long_qty - short_qty
        if net > 0:
            return net, 'LONG'
        if net < 0:
            return abs(net), 'SHORT'

    return 0, None


async def _flatten_async(qty, direccion):
    """Cierra la posicion abierta con orden de mercado."""
    client = _make_client()
    await client.connect()

    tx_type  = TransactionType.SELL if direccion == 'LONG' else TransactionType.BUY
    order_id = 'LFB_CLOSE_%s' % datetime.now(ET).strftime('%H%M%S')

    print('[LIVE] Cerrando posicion: %s x%d → %s' % (direccion, qty, order_id))

    await client.submit_order(
        order_id=order_id,
        symbol=SYMBOL_LIVE,
        exchange=EXCHANGE_LIVE,
        qty=qty,
        transaction_type=tx_type,
        order_type=OrderType.MARKET,
    )

    print('[LIVE] Cierre enviado: %s' % order_id)
    try:
        await client.disconnect()
    except Exception:
        pass  # cierre ya enviado — ignorar error de disconnect


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
    """Cierra la posicion abierta."""
    try:
        asyncio.run(_flatten_async(qty, direccion))
    except Exception as e:
        print('[LIVE] ERROR al cerrar posicion: %s' % e)
