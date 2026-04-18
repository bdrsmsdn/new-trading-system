import math
import time
from hermes.logging_setup import log
from hermes.state import state
from hermes.api.auth import api_call
from hermes.indicators.volatility import get_dynamic_position_size, calculate_volatility
from hermes.config import MIN_TRADE_RP, FEE_BUFFER, STOP_LOSS_PCT, TAKE_PROFIT_PCT, PAIR_DECIMAL_PLACES

def format_coin(amount: float, symbol: str) -> str:
    """Format coin amount for display."""
    if amount < 0.00001:
        return f"{amount:.8f} {symbol}"
    elif amount < 0.001:
        return f"{amount:.6f} {symbol}"
    elif amount < 1:
        return f"{amount:.4f} {symbol}"
    else:
        return f"{amount:.2f} {symbol}"

def execute_buy(pair: str, price: float, idr_balance: float) -> bool:
    """Execute a buy order with dynamic position sizing based on volatility."""
    buy_amount_rp = get_dynamic_position_size(pair, price, idr_balance)
    if buy_amount_rp <= 0:
        log.info(f"Skipping {pair}: insufficient balance for minimum trade")
        return False

    vol_factor = calculate_volatility(pair, price)
    # For sub-IDR coins (FLOKI, PEPE etc.), Indodax expects price as decimal string
    # with exactly 6 decimal places, e.g., "0.564338" (NOT integer 564338).
    decimals = PAIR_DECIMAL_PLACES.get(pair, 0)
    if decimals > 0:
        # Format: exactly 6 decimal places as string
        api_price = f"{price:.{decimals}f}"  # e.g., "0.564338" for FLOKI
    else:
        api_price = str(int(price))
    log.info(f"BUY order (dynamic, vol_factor={vol_factor:.2f}): Rp {buy_amount_rp:,.0f} @ Rp {price:,.0f}")

    result = api_call("trade",
        pair=f"{pair}_idr",
        type="buy",
        price=api_price,
        idr=int(buy_amount_rp),
    )

    if result.get("success") == 1:
        trade_details = result["return"]
        # Actual coin received is in receive_{pair} (e.g. receive_doge)
        coin_amount = float(trade_details.get(f"receive_{pair}", 0) or 0)
        if coin_amount <= 0:
            coin_amount = buy_amount_rp / price  # fallback estimate
        spent_rp = float(trade_details.get("spend_rp", buy_amount_rp) or buy_amount_rp)
        log.info(f"✅ BUY SUCCESS: {format_coin(coin_amount, pair)} @ Rp {price:,.0f}")
        log.info(f"   Total: Rp {spent_rp:,.0f}")

        state.positions[pair] = {
            "entry_price": price,
            "qty": coin_amount,
            "time": time.time(),
            "stop_loss": price * (1 - STOP_LOSS_PCT),
            "take_profit": price * (1 + TAKE_PROFIT_PCT),
            "peak_price": price
        }
        state.last_trade_time[pair] = time.time()
        state.save()
        return True
    else:
        log.error(f"❌ BUY FAILED: {result.get('error', result)}")
        return False

def execute_sell(pair: str, price: float, qty: float, reason: str = "", order_type: str = "limit") -> bool:
    """Execute a sell order.

    Args:
        pair: Trading pair (e.g., 'doge')
        price: Reference price for limit orders, or 0 for market orders
        qty: Amount to sell
        reason: Reason for sell (for logging)
        order_type: "limit" (default) or "market" (for urgent exits like SL/Trailing)
    """
    # Validate qty won't fail due to being too small
    idr_value = qty * price
    if idr_value < 10000:
        log.warning(f"SELL SKIPPED: Rp value ({idr_value:,.0f}) below minimum Rp 10,000")
        return False
    if qty <= 0:
        log.warning(f"SELL SKIPPED: qty ({qty}) is zero or negative")
        return False

    # Round qty to valid decimal places for this pair (Indodax requires ≤8 fractional digits)
    decimals = PAIR_DECIMAL_PLACES.get(pair, 4)
    if decimals == 0:
        qty = math.floor(qty)
    else:
        qty = math.floor(qty * (10 ** decimals)) / (10 ** decimals)

    log.info(f"SELL order ({reason}): {format_coin(qty, pair)} @ Rp {price:,.0f} [{order_type.upper()}]")

    if order_type == "market":
        # Market order — instant execution, no price needed
        # For sub-IDR coins, qty must be integer string
        decimals = PAIR_DECIMAL_PLACES.get(pair, 0)
        if decimals > 0:
            api_qty = str(int(qty))
        else:
            api_qty = str(qty)
        sell_params = {
            "pair": f"{pair}_idr",
            "type": "sell_market",
            pair: api_qty,
        }
    else:
        # Limit order — sell at specific price or better
        # For sub-IDR coins (FLOKI, PEPE etc.), Indodax expects price as decimal string
        # with exactly 6 decimal places, e.g., "0.564338" (NOT integer 564338).
        decimals = PAIR_DECIMAL_PLACES.get(pair, 0)
        if decimals > 0:
            api_price = f"{price:.{decimals}f}"  # e.g., "0.564338" for FLOKI
            api_qty = str(int(qty))  # sub-IDR qty must be integer string
        else:
            api_price = str(int(price))
            api_qty = str(qty)
        sell_params = {
            "pair": f"{pair}_idr",
            "type": "sell",
            "price": api_price,
            pair: api_qty,
        }

    result = api_call("trade", **sell_params)
    
    if result.get("success") == 1:
        trade_details = result["return"]
        log.info(f"✅ SELL SUCCESS: {format_coin(qty, pair)} @ Rp {price:,.0f}")
        log.info(f"   Total: Rp {float(trade_details.get('total', 0)):,.0f}")
        
        if pair in state.positions:
            del state.positions[pair]
        state.last_trade_time[pair] = time.time()
        state.save()
        return True
    else:
        log.error(f"❌ SELL FAILED: {result.get('error', result)}")
        return False
