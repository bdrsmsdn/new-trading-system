import time as _time
import requests
from datetime import datetime
from hermes.config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    TAKE_PROFIT_PCT, STOP_LOSS_PCT, TRAILING_ACTIVATION_PCT
)
from hermes.state import state, prices
from hermes.logging_setup import log

_telegram_enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
_telegram_last_send = 0.0
_telegram_min_interval = 2.0  # seconds between messages (allow trade execution & exit alerts)

def telegram_send(message: str) -> bool:
    """Send a message via Telegram bot with rate limiting."""
    if not _telegram_enabled:
        return False

    global _telegram_last_send
    now = _time.time()

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
    """Send enhanced trade alert for BUY (or fallback for SELL)."""
    if not _telegram_enabled:
        return

    side_upper = side.upper()
    is_buy_or_long = ("BUY" in side_upper) or ("LONG" in side_upper)
    is_futures = ("-PERP" in pair.upper()) or ("LONG" in side_upper) or ("SHORT" in side_upper)
    clean_pair = pair.upper()
    coin = clean_pair.replace("USDT", "").replace("-PERP", "")

    if is_buy_or_long:
        if is_futures:
            msg = (
                f"🟢 *HERMES FUTURES {side_upper}*\n"
                f"Pair: *{clean_pair}*\n"
                f"Entry Price: *${price:.4f}*\n"
                f"Kuantitas: {qty:,.6f} {coin}\n"
                f"Margin: *${total:.2f} USDT* (3x Leverage)\n"
                f"────────────────────\n"
                f"🎯 Target TP: *+5.0% ROE*\n"
                f"🛡️ Trailing Stop: *Aktif di +3.0% ROE* (pullback 1.5%)\n"
                f"🛑 Target SL: *-2.5% ROE*"
            )
        else:
            tp_target = price * (1 + TAKE_PROFIT_PCT)
            sl_target = price * (1 - STOP_LOSS_PCT)
            trail_act = price * (1 + TRAILING_ACTIVATION_PCT)
            msg = (
                f"🟢 *HERMES BUY EXECUTED (SPOT)*\n"
                f"Pair: *{clean_pair}*\n"
                f"Harga Beli (Entry): *${price:.4f}*\n"
                f"Kuantitas: {qty:,.6f} {coin}\n"
                f"Total Modal: *${total:.2f} USDT*\n"
                f"────────────────────\n"
                f"🎯 Target TP (+{TAKE_PROFIT_PCT*100:.1f}%): *${tp_target:.4f}*\n"
                f"🛡️ Trailing Stop: *Aktif di ${trail_act:.4f}* (+{TRAILING_ACTIVATION_PCT*100:.1f}%)\n"
                f"🛑 Target SL (-{STOP_LOSS_PCT*100:.1f}%): *${sl_target:.4f}*"
            )
    else:
        # Fallback if telegram_trade_alert is invoked for SELL
        pos = state.positions.get(pair, {})
        entry_price = pos.get("entry_price", price)
        hold_time = (_time.time() - pos.get("time", _time.time())) / 60
        pnl_pct = (price - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
        pnl_usdt = (price - entry_price) * qty
        is_profit = pnl_pct >= 0

        emoji = "🎯" if is_profit else "🛑"
        tag = "TAKE PROFIT (PROFIT)" if is_profit else "STOP LOSS (CUT LOSS)"
        outcome_lbl = "Untung Bersih" if is_profit else "Rugi / Cut Loss"
        outcome_val = f"+${pnl_usdt:,.2f} USDT (+{pnl_pct:.2f}%)" if is_profit else f"-${abs(pnl_usdt):,.2f} USDT ({pnl_pct:.2f}%)"
        note = "Profit diamankan ke saldo USDT! 🚀" if is_profit else "Proteksi modal aktif: cut loss disiplin. 🛡️"

        msg = (
            f"{emoji} *HERMES {tag} — {clean_pair}*\n"
            f"────────────────────\n"
            f"💵 {outcome_lbl}: *{outcome_val}*\n"
            f"📈 Entry: *${entry_price:.4f}* → Exit: *${price:.4f}*\n"
            f"📦 Kuantitas: {qty:,.6f} {coin}\n"
            f"💰 Total Nilai: *${total:.2f} USDT*\n"
            f"⏱️ Durasi Hold: {int(hold_time)}m\n"
            f"────────────────────\n"
            f"ℹ️ {note}"
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

def telegram_exit_alert(pair: str, side: str, entry: float, exit_price: float, qty: float, pnl_pct: float, hold_hours: float, reason: str, peak_price: float = 0.0) -> None:
    """Send detailed exit alert when spot position is closed (TP/SL/Trailing/Signal)."""
    if not _telegram_enabled:
        return

    clean_pair = pair.upper()
    coin = clean_pair.replace("USDT", "")
    pnl_usdt = (exit_price - entry) * qty
    total_usdt = qty * exit_price

    hours = int(hold_hours)
    minutes = int((hold_hours - hours) * 60)
    if hours > 0:
        duration_str = f"{hours}h {minutes}m"
    else:
        duration_str = f"{minutes}m"

    reason_lower = reason.lower()
    is_profit = pnl_pct >= 0
    is_dynamic = "dynamic" in reason_lower or "peak" in reason_lower
    is_trailing = ("trailing" in reason_lower or "trail" in reason_lower) and not is_dynamic
    is_tp = ("take profit" in reason_lower or "tp" in reason_lower) and not is_dynamic or (is_profit and not is_trailing and not is_dynamic and "signal" not in reason_lower)
    is_sl = "stop loss" in reason_lower or "sl" in reason_lower

    if is_dynamic:
        emoji = "🚀"
        title = f"🚀 *DYNAMIC TAKE PROFIT (PEAK CAPTURE) — {clean_pair}*"
        outcome_label = "Untung Bersih Maksimal"
        outcome_val = f"+${pnl_usdt:,.2f} USDT (+{pnl_pct:.2f}%)"
        note = "Sukses menunggangi tren pump & amankan profit di dekat pucuk! 🚀🎯"
    elif is_tp:
        emoji = "🎯"
        title = f"🎯 *TAKE PROFIT (TP) HIT — {clean_pair}*"
        outcome_label = "Untung Bersih"
        outcome_val = f"+${pnl_usdt:,.2f} USDT (+{pnl_pct:.2f}%)"
        note = "Target profit tercapai & profit diamankan ke saldo USDT! 🚀"
    elif is_trailing:
        emoji = "🛡️"
        title = f"🛡️ *TRAILING STOP EXIT — {clean_pair}*"
        outcome_label = "Hasil PnL"
        outcome_val = f"{'+' if pnl_usdt >= 0 else ''}${pnl_usdt:,.2f} USDT ({pnl_pct:+.2f}%)"
        note = "Trailing stop mengunci profit saat ada indikasi koreksi harga! 🎯"
    elif is_sl:
        emoji = "🛑"
        title = f"🛑 *STOP LOSS (CUT LOSS) — {clean_pair}*"
        outcome_label = "Rugi / Cut Loss"
        outcome_val = f"-${abs(pnl_usdt):,.2f} USDT ({pnl_pct:.2f}%)"
        note = "Proteksi modal aktif: cut loss disiplin untuk membatasi risiko. 🛡️"
    else:
        if is_profit:
            emoji = "🎯"
            title = f"🎯 *SELL EXECUTED (PROFIT) — {clean_pair}*"
            outcome_label = "Untung Bersih"
            outcome_val = f"+${pnl_usdt:,.2f} USDT (+{pnl_pct:.2f}%)"
            note = "Posisi ditutup profit sesuai sinyal strategi. 🚀"
        else:
            emoji = "🛑"
            title = f"🛑 *SELL EXECUTED (CUT LOSS) — {clean_pair}*"
            outcome_label = "Rugi / Cut Loss"
            outcome_val = f"-${abs(pnl_usdt):,.2f} USDT ({pnl_pct:.2f}%)"
            note = "Sinyal strategi merekomendasikan exit untuk pengamanan modal. 🛡️"

    lines = [
        title,
        f"Alasan: *{reason}*",
        f"────────────────────",
        f"💵 {outcome_label}: *{outcome_val}*",
        f"📈 Harga Beli (Entry): *${entry:.4f}*",
        f"📉 Harga Jual (Exit): *${exit_price:.4f}*",
    ]
    if peak_price and peak_price > entry:
        lines.append(f"🔝 Harga Tertinggi (Peak): *${peak_price:.4f}*")

    lines.extend([
        f"📦 Kuantitas: {qty:,.6f} {coin}",
        f"💰 Total Nilai Cair: *${total_usdt:.2f} USDT*",
        f"⏱️ Durasi Hold: {duration_str}",
        f"────────────────────",
        f"ℹ️ {note}"
    ])

    telegram_send("\n".join(lines))

def telegram_futures_exit_alert(
    pair: str,
    side: str,
    entry_price: float,
    exit_price: float,
    amount: float,
    roe_pct: float,
    pnl_usd: float,
    initial_margin: float,
    leverage: int,
    reason: str,
    exit_type: str = "TP",
    peak_roe: float = 0.0
) -> None:
    """Send rich exit alert for Binance Futures positions (TP, Trail, SL)."""
    if not _telegram_enabled:
        return

    clean_pair = pair.upper().replace("-PERP", "")
    notional = amount * exit_price if exit_price > 0 else amount * entry_price
    is_dynamic = "dynamic" in reason.lower() or "peak" in reason.lower()

    if is_dynamic:
        emoji = "🚀"
        title = f"🚀 *FUTURES DYNAMIC TP (PEAK CAPTURE) — {clean_pair}-PERP*"
        outcome_label = "Untung Bersih (ROE)"
        outcome_val = f"+${pnl_usd:,.2f} USDT (+{roe_pct*100:.2f}%)"
        peak_str = f" (Peak: +{peak_roe*100:.2f}%)" if peak_roe > 0 else ""
        note = f"Sukses menunggangi tren pump futures & amankan profit maksimal! 🚀🎯{peak_str}"
    elif exit_type == "TP":
        emoji = "🎯"
        title = f"🎯 *FUTURES TAKE PROFIT (TP) — {clean_pair}-PERP*"
        outcome_label = "Untung Bersih (ROE)"
        outcome_val = f"+${pnl_usd:,.2f} USDT (+{roe_pct*100:.2f}%)"
        note = "Target profit tercapai & posisi futures sukses ditutup! 🚀"
    elif exit_type == "TRAIL":
        emoji = "🛡️"
        title = f"🛡️ *FUTURES TRAILING STOP — {clean_pair}-PERP*"
        outcome_label = "Hasil PnL (ROE)"
        outcome_val = f"{'+' if pnl_usd >= 0 else ''}${pnl_usd:,.2f} USDT ({roe_pct*100:+.2f}%)"
        peak_str = f" (Peak: +{peak_roe*100:.2f}%)" if peak_roe > 0 else ""
        note = f"Trailing stop sukses mengunci keuntungan dari pembalikan arah harga! 🎯{peak_str}"
    else:  # SL
        emoji = "🛑"
        title = f"🛑 *FUTURES STOP LOSS (CUT LOSS) — {clean_pair}-PERP*"
        outcome_label = "Rugi / Cut Loss (ROE)"
        outcome_val = f"-${abs(pnl_usd):,.2f} USDT ({roe_pct*100:.2f}%)"
        note = "Stop loss disiplin dieksekusi untuk mencegah risiko likuidasi. 🛡️"

    lines = [
        title,
        f"Alasan: *{reason}*",
        f"Mode: *{leverage}x Leverage ({side.upper()})*",
        f"────────────────────",
        f"💵 {outcome_label}: *{outcome_val}*",
        f"📈 Entry Price: *${entry_price:.4f}*",
        f"📉 Exit Price: *${exit_price:.4f}*",
    ]
    if exit_type == "TRAIL" and peak_roe > 0:
        lines.append(f"🔝 Peak ROE: *+{peak_roe*100:.2f}%*")

    lines.extend([
        f"📦 Ukuran Posisi: {amount:,.4f} {clean_pair}",
        f"💰 Margin Terpakai: *${initial_margin:.2f} USDT* (Notional: ${notional:.2f})",
        f"────────────────────",
        f"ℹ️ {note}"
    ])

    telegram_send("\n".join(lines))

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
