import time
import math
from hermes.logging_setup import log
from hermes.state import state
from hermes.api.auth import api_call
from hermes.indicators.volatility import get_dynamic_position_size, calculate_volatility
from hermes.config import MIN_TRADE_RP, FEE_BUFFER, PAIR_DECIMAL_PLACES, STOP_LOSS_PCT, TAKE_PROFIT_PCT

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
    if buy_amount_rp < MIN_TRADE_RP * 0.5:
        log.info(f"Skipping {pair}: position size too small after volatility adjustment (Rp {buy_amount_rp:,.0f})")
        return False
    if buy_amount_rp > idr_balance * (1 - FEE_BUFFER):
        buy_amount_rp = idr_balance * (1 - FEE_BUFFER)
    
    coin_amount = buy_amount_rp / price
    
    decimals = PAIR_DECIMAL_PLACES.get(pair, 4)
    if decimals == 0:
        coin_amount = math.floor(coin_amount)
    else:
        coin_amount = math.floor(coin_amount * (10 ** decimals)) / (10 ** decimals)
    
    if coin_amount <= 0:
        log.info(f"Skipping {pair}: coin amount too small")
        return False
    
    vol_factor = calculate_volatility(pair, price)
    log.info(f"BUY order (dynamic, vol_factor={vol_factor:.2f}): {format_coin(coin_amount, pair)} @ Rp {price:,.0f}")
    
    result = api_call("trade",
        pair=f"{pair}idr",
        type="buy",
        price=int(price),
        amount=str(coin_amount)
    )
    
    if result.get("success") == 1:
        trade_details = result["return"]
        log.info(f"✅ BUY SUCCESS: {format_coin(coin_amount, pair)} @ Rp {price:,.0f}")
        log.info(f"   Total: Rp {float(trade_details.get('total', 0)):,.0f}")
        
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
    log.info(f"SELL order ({reason}): {format_coin(qty, pair)} @ Rp {price:,.0f}")
    
    result = api_call("trade",
        pair=f"{pair}idr",
        type="sell",
        price=int(price),
        amount=str(qty)
    )
    
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
