import math
import time
from hermes.logging_setup import log
from hermes.state import state
from hermes.api.auth import api_call, binance_signed_request
from hermes.indicators.volatility import get_dynamic_position_size, calculate_volatility
from hermes.config import MIN_TRADE_USDT, FEE_BUFFER, STOP_LOSS_PCT, TAKE_PROFIT_PCT, PAIR_DECIMAL_PLACES
from hermes.utils import format_price, format_qty


# Cache for exchange info LOT_SIZE constraints
_lot_size_cache = {}


def _get_lot_size_filter(pair: str) -> dict:
    """Fetch LOT_SIZE filter for a symbol from Binance and cache it."""
    if pair in _lot_size_cache:
        return _lot_size_cache[pair]

    symbol = f"{pair.upper()}USDT"
    try:
        # Binance public endpoint, no auth needed
        import requests
        resp = requests.get(
            "https://api.binance.com/api/v3/exchangeInfo",
            params={"symbol": symbol},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            for f in data.get("symbols", []):
                if f["symbol"] == symbol:
                    for filter_block in f.get("filters", []):
                        if filter_block["filterType"] == "LOT_SIZE":
                            _lot_size_cache[pair] = {
                                "minQty": float(filter_block["minQty"]),
                                "maxQty": float(filter_block["maxQty"]),
                                "stepSize": float(filter_block["stepSize"]),
                            }
                            return _lot_size_cache[pair]
        _lot_size_cache[pair] = None
    except Exception as e:
        log.warning(f"Failed to fetch LOT_SIZE for {pair}: {e}")
        _lot_size_cache[pair] = None
    return None


def _round_qty_to_lot_size(qty: float, pair: str) -> float:
    """Round quantity to conform to Binance LOT_SIZE filter."""
    lot_size = _get_lot_size_filter(pair)
    if not lot_size:
        # Fallback: use PAIR_DECIMAL_PLACES
        decimals = PAIR_DECIMAL_PLACES.get(pair.upper(), 4)
        if decimals == 0:
            return math.floor(qty)
        return math.floor(qty * (10 ** decimals)) / (10 ** decimals)

    min_qty = lot_size["minQty"]
    max_qty = lot_size["maxQty"]
    step_size = lot_size["stepSize"]

    # Calculate number of decimal places in stepSize
    step_str = f"{step_size:f}".rstrip('0')
    step_decimals = len(step_str.split('.')[-1]) if '.' in step_str else 0

    # Round to step size: floor(qty / step) * step
    qty_rounded = math.floor(qty / step_size) * step_size

    # Ensure minimum
    if qty_rounded < min_qty:
        qty_rounded = min_qty

    # Ensure maximum
    if qty_rounded > max_qty:
        qty_rounded = max_qty

    # Round to step decimals
    if step_decimals == 0:
        return math.floor(qty_rounded)
    return math.floor(qty_rounded * (10 ** step_decimals)) / (10 ** step_decimals)

def format_coin(amount: float, symbol: str) -> str:
    """Format coin amount for display."""
    return f"{format_qty(amount)} {symbol}"

def execute_buy(pair: str, price: float, usdt_balance: float, dry_run: bool = False, confidence: str = "Medium", score: int = 5) -> tuple[bool, str]:
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
            log.warning(f"[PRICE] {pair} deviation {deviation*100:.1f}% from last price {format_price(last_price)}")

    buy_amount_usdt = get_dynamic_position_size(pair, price, usdt_balance, confidence=confidence, score=score)
    if buy_amount_usdt <= 0:
        msg = f"Skipping {pair}: insufficient balance (${usdt_balance:.2f}) for minimum trade (${MIN_TRADE_USDT:.2f})"
        log.info(msg)
        return False, msg

    vol_factor = calculate_volatility(pair, price)
    log.info(f"BUY order (dynamic, vol_factor={vol_factor:.2f}): ${buy_amount_usdt:.2f} @ {format_price(price)}")

    # Centralized Portfolio Risk Gate Check
    from hermes.trading.portfolio_risk import check_entry_risk, calculate_portfolio_equity
    from hermes.state import prices as _prices_for_equity
    portfolio_equity = calculate_portfolio_equity({"usdt": usdt_balance}, _prices_for_equity)
    if portfolio_equity < usdt_balance:
        portfolio_equity = usdt_balance

    risk_decision = check_entry_risk(
        symbol=pair,
        side="LONG",
        proposed_usdt=buy_amount_usdt,
        price=price,
        current_equity=portfolio_equity,
        free_usdt=usdt_balance,
        open_positions=state.positions,
        is_futures=False
    )
    if not risk_decision.allowed:
        msg = f"Risk gate rejected BUY {pair}: {risk_decision.reason} ({risk_decision.reason_code})"
        log.warning(f"🛡️ [RISK-GATE] {msg}")
        return False, msg

    if dry_run:
        buy_amount_usdt = get_dynamic_position_size(pair, price, usdt_balance, confidence=confidence, score=score)
        log.info(f"[DRY_RUN] BUY: {pair} @ {format_price(price)}, qty_usdt=${buy_amount_usdt:.2f}, expected_coins={buy_amount_usdt/price:.2f}")
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
        log.info(f"✅ BUY SUCCESS: {format_coin(coin_amount, pair)} @ {format_price(price)}")
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

    # Round qty to valid LOT_SIZE for Binance
    original_qty = qty
    qty = _round_qty_to_lot_size(qty, pair)
    if qty != original_qty:
        log.info(f"[LOT_SIZE] Rounded qty {original_qty} -> {qty} for {pair}")

    if qty <= 0:
        msg = f"SELL SKIPPED: qty ({qty}) is zero or negative after LOT_SIZE rounding"
        log.warning(msg)
        return False, msg

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
                pair=pair, entry_price=entry, exit_price=price,
                pnl_pct=pnl_pct, exit_reason=reason or "manual",
                hold_duration_hours=hold_hours,
            )

            peak_price = state.positions[pair].get("peak_price", entry)
            from hermes.notifications.telegram import telegram_exit_alert
            telegram_exit_alert(
                pair=pair,
                side="SELL",
                entry=entry,
                exit_price=price,
                qty=qty,
                pnl_pct=pnl_pct,
                hold_hours=hold_hours,
                reason=reason or "Take Profit",
                peak_price=peak_price
            )

            # Profit handling: daily collection model (accumulates in Spot,
            # collector sweeps once daily target is reached) with legacy
            # per-trade sweep kept as optional fallback.
            from hermes.config import AUTO_SWEEP_PROFIT_TO_FUNDING, PROFIT_SWEEP_MIN_USDT, DAILY_PROFIT_COLLECTION
            pnl_usdt = (price - entry) * qty
            try:
                if DAILY_PROFIT_COLLECTION:
                    from hermes.api.transfer import track_realized_profit, run_daily_profit_collector
                    track_realized_profit(pair=pair, pnl_usdt=pnl_usdt)
                    run_daily_profit_collector()
                elif AUTO_SWEEP_PROFIT_TO_FUNDING and pnl_usdt >= PROFIT_SWEEP_MIN_USDT:
                    from hermes.api.transfer import sweep_profit_to_funding
                    sweep_profit_to_funding(profit_usdt=pnl_usdt, min_threshold=PROFIT_SWEEP_MIN_USDT, pair=pair)
            except Exception as swe:
                log.error(f"[PROFIT-HANDLING] Error: {swe}")

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

    peak_price = state.positions[pair].get("peak_price", entry)
    from hermes.notifications.telegram import telegram_exit_alert
    telegram_exit_alert(pair=pair, side="SELL", entry=entry, exit_price=price, qty=qty, pnl_pct=pnl_pct, hold_hours=hold_hours, reason=reason, peak_price=peak_price)

    del state.positions[pair]
    state.last_trade_time[pair] = time.time()
    state.save()
