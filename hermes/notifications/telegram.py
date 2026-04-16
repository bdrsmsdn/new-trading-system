import requests
from datetime import datetime
from hermes.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from hermes.state import prices

_telegram_enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

def telegram_send(message: str) -> bool:
    """Send a message via Telegram bot."""
    if not _telegram_enabled:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False

def telegram_trade_alert(pair: str, side: str, qty: float, price: float, total: float) -> None:
    """Send trade execution alert."""
    emoji = "🟢" if side == "BUY" else "🔴"
    msg = (
        f"{emoji} *HERMES TRADE EXECUTED*\n"
        f"Type: {side}\n"
        f"Pair: {pair.upper()}\n"
        f"Qty: {qty:,.8f} @ Rp {price:,.0f}\n"
        f"Total: Rp {total:,.0f}"
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

def telegram_morning_brief(fg_val: int, fg_class: str, idr_balance: float, positions: dict) -> None:
    """Send morning brief with F&G and portfolio summary."""
    lines = [
        f"🌅 *Hermes Morning Brief*\n"
        f"Time: {datetime.now().strftime('%d %b %Y, %H:%M WIB')}",
        f"",
        f"📊 *Fear & Greed:* {fg_val} ({fg_class})",
        f"",
        f"💰 *Balance:* Rp {idr_balance:,.0f}",
    ]
    if positions:
        lines.append(f"")
        lines.append(f"📁 *Open Positions:*")
        for pair, pos in positions.items():
            pnl = (prices.get(pair, {}).get("price", 0) - pos["entry_price"]) / pos["entry_price"] * 100
            lines.append(
                f"  {pair.upper()}: Rp {pos['entry_price']:,.0f} ({pnl:+.1f}%)"
            )
    else:
        lines.append(f"")
        lines.append(f"📁 *Open Positions:* None")

    telegram_send("\n".join(lines))
