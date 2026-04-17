import time
from hermes.logging_setup import log
from hermes.state import state
from hermes.api.auth import api_call
from hermes.indicators.volatility import get_dynamic_position_size, calculate_volatility
from hermes.config import MIN_TRADE_RP, FEE_BUFFER, STOP_LOSS_PCT, TAKE_PROFIT_PCT

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
    log.info(f"BUY order (dynamic, vol_factor={vol_factor:.2f}): Rp {buy_amount_rp:,.0f} @ Rp {price:,.0f}")

    result = api_call("trade",
        pair=f"{pair}_idr",
        type="buy",
        price=int(price),
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

def execute_sell(pair: str, price: float, qty: float, reason: str = "") -> bool:
    """Execute a sell order."""
    # Validate qty won't fail due to being too small
    idr_value = qty * price
    if idr_value < 10000:
        log.warning(f"SELL SKIPPED: Rp value ({idr_value:,.0f}) below minimum Rp 10,000")
        return False
    if qty <= 0:
        log.warning(f"SELL SKIPPED: qty ({qty}) is zero or negative")
        return False

    log.info(f"SELL order ({reason}): {format_coin(qty, pair)} @ Rp {price:,.0f}")

    sell_params = {
        "pair": f"{pair}_idr",
        "type": "sell",
        "price": str(int(price)),
        pair: str(qty),          # e.g. doge="100.0" — required by Indodax docs
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
