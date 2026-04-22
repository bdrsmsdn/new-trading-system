import math
import time
from hermes.logging_setup import log
from hermes.state import state
from hermes.api.auth import api_call
from hermes.indicators.volatility import get_dynamic_position_size, calculate_volatility
from hermes.config import MIN_TRADE_USDT, FEE_BUFFER, STOP_LOSS_PCT, TAKE_PROFIT_PCT, PAIR_DECIMAL_PLACES

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

def execute_buy(pair: str, price: float, usdt_balance: float, dry_run: bool = False) -> tuple[bool, str]:
    """Execute a buy order on Binance with dynamic position sizing.

    Returns (success, error_message).
    Binance market buy uses quoteOrderQty for USDT amount.
    """
    from hermes.state import prices as _prices_cache
    # Price deviation guard
    cached = _prices_cache.get(pair, {})
    last_price = cached.get("price")
    if last_price and last_price > 0:
        deviation = abs(price - last_price) / last_price
        if deviation > 0.02:
            log.warning(f"[PRICE] {pair} deviation {deviation*100:.1f}% from last price ${last_price:.4f}")

    buy_amount_usdt = get_dynamic_position_size(pair, price, usdt_balance)
    if buy_amount_usdt <= 0:
        msg = f"Skipping {pair}: insufficient balance (${usdt_balance:.2f}) for minimum trade (${MIN_TRADE_USDT:.2f})"
        log.info(msg)
        return False, msg

    vol_factor = calculate_volatility(pair, price)
    log.info(f"BUY order (dynamic, vol_factor={vol_factor:.2f}): ${buy_amount_usdt:.2f} @ ${price:.4f}")

    if dry_run:
        buy_amount_usdt = get_dynamic_position_size(pair, price, usdt_balance)
        log.info(f"[DRY_RUN] BUY: {pair} @ ${price:.4f}, qty_usdt=${buy_amount_usdt:.2f}, expected_coins={buy_amount_usdt/price:.2f}")
        _simulate_buy(pair, price, buy_amount_usdt)
        return True, ""

    # Binance market buy: symbol, side, type, quoteOrderQty (USDT amount)
    # quoteOrderQty must have max 2 decimal places for Binance
    result = api_call("trade",
        pair=f"{pair.upper()}USDT",  # e.g., DOGEUSDT
        type="buy",
        quoteOrderQty=round(buy_amount_usdt, 2),
    )

    if result.get("status") == "FILLED" or result.get("success") == 1:
        trade_details = result
        # Binance returns: {"executedQty": "1000", "cummulativeQuoteQty": "120.00"}
        coin_amount = float(trade_details.get("executedQty", 0) or 0)
        if coin_amount <= 0:
            coin_amount = buy_amount_usdt / price  # fallback estimate
        spent_usdt = float(trade_details.get("cummulativeQuoteQty", buy_amount_usdt) or buy_amount_usdt)
        log.info(f"✅ BUY SUCCESS: {format_coin(coin_amount, pair)} @ ${price:.4f}")
        log.info(f"   Total: ${spent_usdt:.2f}")

        from hermes.notifications.telegram import telegram_trade_alert
        telegram_trade_alert(pair=pair, side="BUY", qty=coin_amount, price=price, total=spent_usdt)

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
        return True, ""
    else:
        error_msg = result.get('msg') or result.get('error') or str(result)
        log.error(f"❌ BUY FAILED: {error_msg}")
        return False, error_msg

def execute_sell(pair: str, price: float, qty: float, reason: str = "", order_type: str = "limit", dry_run: bool = False) -> tuple[bool, str]:
    """Execute a sell order on Binance.

    Returns (success, error_message).
    Args:
        pair: Trading pair (e.g., 'DOGE')
        price: Reference price (not used for market orders)
        qty: Amount to sell (in coin units)
        reason: Reason for sell (for logging)
        order_type: "limit" or "market" (default: "limit")
        dry_run: If True, simulate the sell without API call
    """
    from hermes.state import prices as _prices_cache
    # Price deviation guard
    cached = _prices_cache.get(pair, {})
    last_price = cached.get("price")
    if last_price and last_price > 0:
        deviation = abs(price - last_price) / last_price
        if deviation > 0.02:
            log.warning(f"[PRICE] {pair} deviation {deviation*100:.1f}% from last price ${last_price:.4f}")

    # DRY_RUN handling
    if dry_run:
        usdt_value = qty * price
        log.info(f"[DRY_RUN] SELL: {pair} @ ${price:.4f}, qty={qty}, usdt_value=${usdt_value:.2f}, reason={reason}")
        _simulate_sell(pair, price, qty, reason)
        return True, ""

    # Validate qty won't fail due to being too small
    usdt_value = qty * price
    if usdt_value < MIN_TRADE_USDT:
        msg = f"SELL SKIPPED: USDT value (${usdt_value:.2f}) below minimum ${MIN_TRADE_USDT}"
        log.warning(msg)
        return False, msg
    if qty <= 0:
        msg = f"SELL SKIPPED: qty ({qty}) is zero or negative"
        log.warning(msg)
        return False, msg

    # Pre-emptive stale position cleanup: if pair is in state.positions but we hold 0 of it, remove it
    if pair in state.positions:
        from hermes.api.balance import get_balance
        balances = get_balance(use_cache=True)
        coin = pair.replace("USDT", "")
        coin_balance = balances.get(coin.lower(), 0)
        if coin_balance == 0:
            log.warning(f"Removing stale position {pair} from state — balance is 0, cannot sell")
            del state.positions[pair]
            state.save()
            return False, f"Stale position {pair} removed: zero balance"

    # Round qty to valid decimal places for this pair
    decimals = PAIR_DECIMAL_PLACES.get(pair.upper(), 4)
    if decimals == 0:
        qty = math.floor(qty)
    else:
        qty = math.floor(qty * (10 ** decimals)) / (10 ** decimals)

    log.info(f"SELL order ({reason}): {format_coin(qty, pair)} @ ${price:.4f} [{order_type.upper()}]")

    if order_type == "market":
        # Binance market sell: symbol, side, type, quantity (coin amount)
        result = api_call("trade",
            pair=f"{pair.upper()}USDT",
            type="sell",
            quantity=qty,
        )
    else:
        # Binance limit sell: symbol, side, type, quantity, price
        result = api_call("trade",
            pair=f"{pair.upper()}USDT",
            type="sell",
            quantity=qty,
            price=price,
        )

    if result.get("status") == "FILLED" or result.get("success") == 1:
        trade_details = result
        log.info(f"✅ SELL SUCCESS: {format_coin(qty, pair)} @ ${price:.4f}")
        log.info(f"   Total: ${float(trade_details.get('cummulativeQuoteQty', 0)):.2f}")

        # Archive trade outcome and notify
        if pair in state.positions:
            entry = state.positions[pair]["entry_price"]
            entry_time = state.positions[pair]["time"]
            hold_hours = (time.time() - entry_time) / 3600
            pnl_pct = (price - entry) / entry * 100 if entry > 0 else 0
            total_usdt = float(trade_details.get("cummulativeQuoteQty", 0))

            from hermes.agent.memory import agent_memory
            agent_memory.log_trade_outcome(
                pair=pair, entry_price=entry, exit_price=price, qty=qty,
                pnl_pct=pnl_pct, exit_reason=reason or "manual",
                hold_duration_hours=hold_hours,
            )

            from hermes.notifications.telegram import telegram_trade_alert
            telegram_trade_alert(pair=pair, side="SELL", qty=qty, price=price, total=total_usdt)

            del state.positions[pair]
        state.last_trade_time[pair] = time.time()
        state.save()
        return True, ""
    else:
        error_msg = result.get('msg') or result.get('error') or str(result)
        log.error(f"❌ SELL FAILED: {error_msg}")
        if ("-2010" in error_msg or "insufficient balance" in error_msg.lower()) and pair in state.positions:
            log.warning(f"Removing stale position {pair} from state — SELL failed with insufficient balance")
            del state.positions[pair]
            state.save()
        return False, error_msg

def _simulate_buy(pair: str, price: float, usdt_amount: float):
    """Simulate buy in dry-run mode — no API call, update state."""
    coin_amount = usdt_amount / price
    log.info(f"[DRY_RUN] Simulated BUY: {format_coin(coin_amount, pair)} @ ${price:.4f}")

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

    from hermes.notifications.telegram import telegram_trade_alert
    telegram_trade_alert(pair=pair, side="BUY", qty=coin_amount, price=price, total=usdt_amount)

def _simulate_sell(pair: str, price: float, qty: float, reason: str):
    """Simulate sell in dry-run mode — no API call, update state."""
    if pair not in state.positions:
        log.warning(f"[DRY_RUN] No position to close for {pair}")
        return

    entry = state.positions[pair]["entry_price"]
    entry_time = state.positions[pair]["time"]
    pnl_pct = (price - entry) / entry * 100 if entry > 0 else 0
    hold_hours = (time.time() - entry_time) / 3600
    usdt_total = qty * price

    log.info(f"[DRY_RUN] Simulated SELL: {pair} @ ${price:.4f}, pnl={pnl_pct:.1f}%, hold={hold_hours:.1f}h")

    from hermes.notifications.telegram import telegram_exit_alert
    telegram_exit_alert(pair=pair, side="SELL", entry=entry, exit_price=price, qty=qty, pnl_pct=pnl_pct, hold_hours=hold_hours, reason=reason)

    del state.positions[pair]
    state.last_trade_time[pair] = time.time()
    state.save()
