import time as _time
import requests
from datetime import datetime
from hermes.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from hermes.state import state, prices

_telegram_enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
_telegram_last_send = 0.0
_telegram_min_interval = 20.0  # seconds between messages (3 per minute max)

def telegram_send(message: str) -> bool:
    """Send a message via Telegram bot with rate limiting."""
    if not _telegram_enabled:
        return False

    global _telegram_last_send
    now = _time.time()

    # Rate limit: max 3 messages per minute (20s between messages)
    if now - _telegram_last_send < _telegram_min_interval:
        log.debug(f"[TELEGRAM] Rate limited, skipping message")
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }, timeout=10)
        _telegram_last_send = now
        return resp.status_code == 200
    except Exception as e:
        log.debug(f"Telegram send failed: {e}")
        return False

def telegram_trade_alert(pair: str, side: str, qty: float, price: float, total: float) -> None:
    """Send enhanced trade alert with full context."""
    if not _telegram_enabled:
        return

    pos = state.positions.get(pair, {})
    entry_price = pos.get("entry_price", price)
    stop_loss = pos.get("stop_loss", price * 0.95)
    hold_time = (_time.time() - pos.get("time", _time.time())) / 60

    current_price_data = prices.get(pair, {})
    current_price = current_price_data.get("price", price)
    pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price > 0 else 0

    emoji = "🟢" if side == "BUY" else "🔴"

    msg = (
        f"{emoji} *HERMES {'BUY' if side == 'BUY' else 'SELL'} EXECUTED*\n"
        f"Pair: {pair.upper()}\n"
        f"Price: ${price:.4f}\n"
        f"Qty: {qty:,.6f}\n"
        f"Total: ${total:.2f}\n"
        f"---\n"
        f"Entry: ${entry_price:.4f} | Current: ${current_price:.4f}\n"
        f"PnL: {pnl_pct:+.1f}% | SL: ${stop_loss:.4f}\n"
        f"Hold: {int(hold_time)}m"
    )
    telegram_send(msg)

def telegram_tp_alert(pair: str, pnl_pct: float) -> None:
    """Alert when take profit is hit."""
    msg = (
        f"🎯 *Take Profit Hit!*\n"
        f"Pair: {pair.upper()}\n"
        f"PnL: +{pnl_pct:.1f}%"
    )
    telegram_send(msg)

def telegram_ts_alert(pair: str, pnl_pct: float) -> None:
    """Alert when stop loss is hit."""
    msg = (
        f"🛑 *Stop Loss Hit!*\n"
        f"Pair: {pair.upper()}\n"
        f"PnL: {pnl_pct:.1f}%"
    )
    telegram_send(msg)

def telegram_exit_alert(pair: str, side: str, entry: float, exit_price: float, qty: float, pnl_pct: float, hold_hours: float, reason: str) -> None:
    """Send exit alert when position is closed (TP/SL/Trailing)."""
    if not _telegram_enabled:
        return

    emoji = "🎯" if pnl_pct >= 0 else "🛑"
    reason_key = "TP" if "Take Profit" in reason else ("SL" if "Stop Loss" in reason else ("TS" if "Trailing" in reason else "EXIT"))

    msg = (
        f"{emoji} *{reason_key} HIT — {pair.upper()}*\n"
        f"Entry: ${entry:.4f} → Exit: ${exit_price:.4f}\n"
        f"PnL: {pnl_pct:+.1f}% | Held: {int(hold_hours)}h {int((hold_hours % 1) * 60)}m\n"
        f"Qty: {qty:,.6f} | Total: ${qty * exit_price:.2f}\n"
        f"Reason: {reason}"
    )
    telegram_send(msg)

def telegram_morning_brief(fg_val: int, fg_class: str, usdt_balance: float, positions: dict) -> None:
    """Send morning brief with F&G, portfolio summary, and daily P&L."""
    from hermes.agent.memory import agent_memory

    lines = [
        f"🌅 *Hermes Morning Brief*\n"
        f"Time: {datetime.now().strftime('%d %b %Y, %H:%M WIB')}",
        f"",
        f"📊 *Fear & Greed:* {fg_val} ({fg_class})",
        f"",
        f"💰 *USDT Balance:* ${usdt_balance:,.2f}",
    ]

    # Daily P&L from memory
    daily_stats = getattr(agent_memory, 'get_daily_stats', lambda: None)()
    if daily_stats:
        lines.append(f"")
        lines.append(f"📈 *Yesterday's Performance:*")
        lines.append(f"  Trades: {daily_stats.get('trade_count', 0)}")
        lines.append(f"  Win Rate: {daily_stats.get('win_rate', 0):.0f}%")
        lines.append(f"  PnL: ${daily_stats.get('pnl_usdt', 0):+.2f}")
        if daily_stats.get('best_trade'):
            lines.append(f"  Best: {daily_stats['best_trade']}")
        if daily_stats.get('worst_trade'):
            lines.append(f"  Worst: {daily_stats['worst_trade']}")

    if positions:
        lines.append(f"")
        lines.append(f"📁 *Open Positions ({len(positions)}):*")
        total_unrealized = 0
        for pair, pos in positions.items():
            current_price_data = prices.get(pair, {})
            current_price = current_price_data.get("price", 0)
            entry = pos["entry_price"]
            pnl = (current_price - entry) / entry * 100 if entry > 0 and current_price else 0
            unrealized = (current_price - entry) * pos.get("qty", 0)
            total_unrealized += unrealized
            lines.append(
                f"  {pair.upper()}: ${entry:.4f} → ${current_price:.4f} ({pnl:+.1f}%)"
            )
        lines.append(f"  Unrealized P&L: ${total_unrealized:+.2f}")
    else:
        lines.append(f"")
        lines.append(f"📁 *Open Positions:* None")

    telegram_send("\n".join(lines))

def telegram_regime_alert(new_regime: str, fg_val: int, max_pairs: int, position_mult: float) -> None:
    """Alert when market regime changes."""
    if not _telegram_enabled:
        return

    emoji = {"BULL": "🐂", "SIDEWAYS": "↔️", "BEAR": "🐻"}.get(new_regime, "📊")

    msg = (
        f"📊 *Market Regime Changed*\n"
        f"{emoji} {new_regime} | F&G: {fg_val}\n"
        f"Max Active Pairs: {max_pairs}\n"
        f"Position Size: {position_mult:.1f}x"
    )
    telegram_send(msg)
