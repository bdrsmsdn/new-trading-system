import time
from typing import Dict
from hermes.logging_setup import log
from hermes.state import state, _multi_rsi_cache
from hermes.indicators.rsi import get_rsi, get_multi_rsi
from hermes.indicators.signals import get_signal
from hermes.trading.execution import execute_sell, execute_buy
from hermes.api.balance import get_balance
from hermes.config import STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_ACTIVATION_PCT, TRAILING_STOP_PCT, TRADE_COOLDOWN, MIN_TRADE_USDT

_MULTI_RSI_TTL = 300  # Match rsi.py TTL

def check_open_positions(current_price: float, balance: Dict[str, float]) -> None:
    """Check and manage open positions (TP/SL/Trailing Stop).
    Uses cached RSI only — no REST calls for position checks."""
    for pair, pos in list(state.positions.items()):
        entry = pos["entry_price"]
        qty = pos.get("qty", 0)
        stop_loss = pos.get("stop_loss", entry * (1 - STOP_LOSS_PCT))
        take_profit = pos.get("take_profit", entry * (1 + TAKE_PROFIT_PCT))
        peak_price = pos.get("peak_price", entry)
        
        pnl_pct = (current_price - entry) / entry
        
        if current_price > peak_price:
            peak_price = current_price
            pos["peak_price"] = peak_price
        
        if current_price >= take_profit:
            log.info(f"TP hit for {pair}: +{pnl_pct*100:.1f}%")
            execute_sell(pair, current_price, qty, "Take Profit")
            continue
        
        if pnl_pct >= TRAILING_ACTIVATION_PCT:
            trailing_stop_price = peak_price * (1 - TRAILING_STOP_PCT)
            if current_price <= trailing_stop_price:
                log.info(f"Trailing SL hit for {pair}: price dropped to {current_price}, trail price {trailing_stop_price}, peak {peak_price}")
                execute_sell(pair, current_price, qty, "Trailing Stop", order_type="market")
                continue
        
        if current_price <= stop_loss:
            log.info(f"SL hit for {pair}: {pnl_pct*100:.1f}%")
            execute_sell(pair, current_price, qty, "Stop Loss", order_type="market")
            continue
        
        # Signal-based exit — only trigger if profit is already substantial (>= +2.0%) or severe reversal
        if pnl_pct >= 0.02:
            cached_mrsi = _multi_rsi_cache.get(pair, {})
            if cached_mrsi and (time.time() - cached_mrsi.get("ts", 0)) < _MULTI_RSI_TTL:
                multi_rsi = dict(cached_mrsi["rsi"])
                multi_rsi["3m"] = get_rsi(pair)
            else:
                # No cache — use WS-derived RSI only (NO REST)
                multi_rsi = {"3m": get_rsi(pair), "1h": 50.0, "4h": 50.0}
            signal, score, reasons = get_signal(pair, current_price, multi_rsi)
            if signal == "STRONG_SELL":
                coin = pair.replace("USDT", "")
                balances = get_balance(use_cache=False)
                coin_lower = coin.lower()
                balance = balances.get(coin_lower, 0)
                if balance == 0:
                    log.warning(f"Stale position {pair} — Binance balance is 0, skipping sell and removing from state")
                    del state.positions[pair]
                    state.save()
                    continue
                log.info(f"Signal exit for {pair}: {signal} with +{pnl_pct*100:.2f}%")
                execute_sell(pair, current_price, qty, f"Signal: {signal}")

def check_for_entries(pair: str, current_price: float, idr_balance: float, dry_run: bool = False) -> bool:
    """Check if we should enter a position. Budget-aware."""
    last_trade = state.last_trade_time.get(pair, 0)
    if time.time() - last_trade < TRADE_COOLDOWN:
        return False
    
    if pair in state.positions:
        return False
    
    # Use cached RSI if available, only fetch if budget allows
    from hermes.api.rest import _check_budget
    cached_mrsi = _multi_rsi_cache.get(pair, {})
    if cached_mrsi and (time.time() - cached_mrsi.get("ts", 0)) < _MULTI_RSI_TTL:
        multi_rsi = dict(cached_mrsi["rsi"])
        multi_rsi["3m"] = get_rsi(pair)
    elif _check_budget():
        multi_rsi = get_multi_rsi(pair, current_price)
    else:
        multi_rsi = {"3m": get_rsi(pair), "1h": 50.0, "4h": 50.0}

    signal, score, reasons = get_signal(pair, current_price, multi_rsi)
    
    log.info(f"{pair.upper()}: {signal} (score={score}) - {', '.join(reasons)}")
    
    if signal in ["STRONG_BUY", "BUY"] and idr_balance >= MIN_TRADE_USDT:
        return execute_buy(pair, current_price, idr_balance, dry_run=dry_run)
    
    return False
