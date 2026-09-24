"""
Hermes Telegram Bot — Interactive AI trading chatbot.

Connects the MiniMax-powered Hermes agent to Telegram,
allowing natural language interaction for trading operations.
"""

import asyncio
import logging
from telegram import Update, BotCommand, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ChatAction, ParseMode

from hermes.logging_setup import log
from hermes.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from hermes.agent.agent import HermesAgent
from hermes.agent.memory import agent_memory
from hermes.agent.tools import execute_tool, CONFIRM_REQUIRED

# Suppress overly verbose telegram library logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


# ─── Authorization ─────────────────────────────────────────────────────────────

def authorized(func):
    """Decorator to restrict bot access to authorized chat ID only."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = str(update.effective_chat.id)
        if chat_id != str(TELEGRAM_CHAT_ID):
            await update.message.reply_text(
                "⛔ Unauthorized. Bot ini hanya untuk pemilik bot."
            )
            log.warning(f"[BOT] Unauthorized access from chat_id={chat_id}")
            return
        return await func(update, context)
    return wrapper


# ─── Custom Keyboard Markup ───────────────────────────────────────────────────

HERMES_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("/spot"), KeyboardButton("/positions"), KeyboardButton("/sync")],
        [KeyboardButton("/futures"), KeyboardButton("/screener"), KeyboardButton("/rank")],
        [KeyboardButton("/fear"), KeyboardButton("/news"), KeyboardButton("/plan SOL")],
    ],
    resize_keyboard=True,
    is_persistent=True
)


# ─── Direct Command Parser ─────────────────────────────────────────────────────

import re
import json

def _parse_direct_command(text: str) -> tuple[str, dict] | None:
    """Parse text into (tool_name, tool_input) if it matches a direct command.

    Returns None if text doesn't match any direct command pattern.
    Supports both slash commands (/buy) and plain text (buy doge).
    """
    text = text.strip()
    lower = text.lower()

    # /buy [pair] or buy [pair]
    m = re.match(r'^(?:/buy|buy)\s+(\w+)$', lower)
    if m:
        pair = m.group(1)
        return "execute_buy", {"pair": pair}

    # /sell [pair] [qty] or sell [pair] [qty]
    m = re.match(r'^(?:/sell|sell)\s+(\w+)\s+([\d.]+)$', lower)
    if m:
        pair = m.group(1)
        qty = float(m.group(2))
        return "execute_sell", {"pair": pair, "qty": qty}

    # /signal [pair] or signal [pair]
    m = re.match(r'^(?:/signal|signal)\s+(\w+)$', lower)
    if m:
        pair = m.group(1)
        return "get_signal_v2", {"pair": pair}

    # /price [pair] or price [pair]
    m = re.match(r'^(?:/price|price)\s+(\w+)$', lower)
    if m:
        pair = m.group(1)
        return "get_price", {"pair": pair}

    # /spot or spot
    if re.match(r'^(?:/spot|spot|porto|portfolio|balance|/balance|/portfolio)$', lower):
        return "get_spot_overview", {}

    # /futures or futures or /fapi
    if re.match(r'^(?:/futures|futures|/fapi|fapi|perp|/perp)$', lower):
        return "get_futures_overview", {}

    # /rank or rank
    if re.match(r'^(?:/rank|rank)$', lower):
        return "rank_pairs", {}

    # /fear or fear
    if re.match(r'^(?:/fear|fear)$', lower):
        return "get_fear_greed", {}

    # /news or news or /news [pair]
    m = re.match(r'^(?:/news|news|berita)(?:\s+(\w+))?$', lower)
    if m:
        pair = m.group(1)
        return "get_crypto_news", {"pair": pair} if pair else {}

    # /positions or positions
    if re.match(r'^(?:/positions|positions)$', lower):
        return "check_positions", {}

    # /sync or sync or /reconcile
    if re.match(r'^(?:/sync|sync|reconcile|/reconcile)$', lower):
        return "sync_positions", {}

    # /tf [pair] or mtf [pair]
    m = re.match(r'^(?:/tf|tf|mtf)\s+(\w+)$', lower)
    if m:
        pair = m.group(1)
        return "get_multi_tf_analysis", {"pair": pair}

    # /screener or screener
    if re.match(r'^(?:/screener|screener|scan)$', lower):
        return "get_market_screener", {}

    # /plan [pair]
    m = re.match(r'^(?:/plan|plan|tpsl)\s+(\w+)$', lower)
    if m:
        pair = m.group(1)
        return "get_tp_sl_plan", {"pair": pair}

    # /whale [pair] or /depth [pair]
    m = re.match(r'^(?:/whale|whale|depth|walls)\s+(\w+)$', lower)
    if m:
        pair = m.group(1)
        return "get_whale_walls", {"pair": pair}

    # /backtest [pair] [days]
    m = re.match(r'^(?:/backtest|backtest|bt)\s+(\w+)(?:\s+(\d+))?$', lower)
    if m:
        pair = m.group(1)
        days = int(m.group(2)) if m.group(2) else 7
        return "run_quick_backtest", {"pair": pair, "days": days}

    return None


def _execute_direct_command(tool_name: str, tool_input: dict) -> str:
    """Execute a tool directly and format result for Telegram."""
    try:
        result = execute_tool(tool_name, tool_input)
        data = json.loads(result)

        if "error" in data:
            return f"⚠️ Error: {data['error']}"

        # Format success results based on tool type
        if tool_name == "execute_buy":
            if data.get("success"):
                return f"✅ Buy berhasil!\n💰 Spent: {data.get('idr_spent', 0):,.0f} IDR\n📦 Pair: {data.get('pair', '').upper()}\n💵 Price: {data.get('price', 0):,.0f} IDR"
            return f"❌ Buy gagal: {data.get('error', 'unknown')}"

        elif tool_name == "execute_sell":
            if data.get("success"):
                return f"✅ Sell berhasil!\n📦 Pair: {data.get('pair', '').upper()}\n💵 Qty: {data.get('qty', 0)}\n💵 Price: {data.get('price', 0):,.0f} IDR"
            return f"❌ Sell gagal: {data.get('error', 'unknown')}"

        elif tool_name == "get_price":
            return f"💵 {data.get('pair', '').upper()}: {data.get('price', 0):,.0f} IDR"

        elif tool_name == "get_signal_v2":
            sig = data.get("signal", "N/A")
            conf = data.get("confidence", "N/A")
            entry = data.get("entry_price", 0)
            sl = data.get("stop_loss", 0)
            tp = data.get("take_profit", 0)
            rsi = data.get("rsi", "N/A")
            daily_pos = data.get("daily_pos", "N/A")
            emoji = {"LONG": "🟢", "SHORT": "🔴", "NO TRADE SETUP": "⚪"}.get(sig, "❓")
            return (
                f"{emoji} **{sig}** (confidence: {conf})\n\n"
                f"📊 RSI: {rsi}\n📈 Daily Pos: {daily_pos}%\n"
                f"💰 Entry: {entry:,.0f}\n🛑 SL: {sl:,.0f}\n🎯 TP: {tp:,.0f}\n\n"
                f"Reasons:\n{data.get('reasons', 'N/A')}"
            )

        elif tool_name == "get_balance":
            idr = data.get("idr", 0)
            usdt = data.get("usdt", 0)
            lines = [f"💰 *Balance:*", f"• IDR: {idr:,.0f}", f"• USDT: {usdt:,.2f}"]
            if data.get("holdings"):
                lines.append("\n📦 *Holdings:*")
                for h in data["holdings"]:
                    lines.append(f"• {h['coin'].upper()}: {h['available']} (≈{h.get('idr_value', 0):,.0f} IDR)")
            return "\n".join(lines)

        elif tool_name == "rank_pairs":
            rankings = data.get("rankings", [])
            if not rankings:
                return "📊 No rankings available"
            lines = ["📊 *Top Pairs:*"]
            for r in rankings[:10]:
                sig = r.get("signal", "?")
                emoji = {"STRONG_BUY": "🚀", "BUY": "🟢", "HOLD": "⚪", "SELL": "🔴", "STRONG_SELL": "💥"}.get(sig, "❓")
                lines.append(f"{emoji} {r['pair'].upper()}: {sig} (score: {r.get('score', 0):.1f})")
            return "\n".join(lines)

        elif tool_name == "get_fear_greed":
            return f"😱 *Fear & Greed:* {data.get('fear_greed_value', 'N/A')} — {data.get('classification', 'N/A')}"

        elif tool_name == "get_spot_overview":
            total = data.get("total_portfolio_usdt", 0)
            holdings = data.get("holdings", [])
            lines = [f"💰 **Total Spot Portfolio**: ${total:,.2f} USDT\n"]
            for h in holdings:
                lines.append(f"• **{h['asset']}**: {h['total']} (${h['usdt_value']:,.2f} USDT)")
            if data.get("dust_count", 0) > 0:
                lines.append(f"\n🔍 *Dust assets (< $1)*: {data.get('dust_count')} items")
            return "\n".join(lines)

        elif tool_name == "get_futures_overview":
            total = data.get("total_wallet_balance", 0)
            avail = data.get("available_balance", 0)
            pnl = data.get("unrealized_pnl", 0)
            positions = data.get("open_positions", [])
            lines = [
                f"⚡ **Binance USDT-M Futures Overview**",
                f"💵 **Wallet Balance**: `${total:,.2f} USDT`",
                f"🟢 **Available Margin**: `${avail:,.2f} USDT`",
                f"📊 **Unrealized PnL**: `${pnl:+,.2f} USDT`\n"
            ]
            if positions:
                lines.append("🎯 **Open Positions:**")
                for p in positions:
                    lines.append(f"• **{p['pair']}** ({p['side']} {p['leverage']}x): {p['amount']} @ ${p['entry_price']:.4f} | PnL: `${p['unrealized_pnl']:+,.2f}` (Liq: ${p['liquidation_price']:.4f})")
            else:
                lines.append("⚪ *Tidak ada posisi Futures yang sedang terbuka.*")
            return "\n".join(lines)

        elif tool_name == "get_multi_tf_analysis":
            pair = data.get("pair", "")
            align = data.get("overall_alignment", "NEUTRAL")
            lines = [f"📊 **Multi-Timeframe Analysis: {pair}**", f"🎯 **Trend Alignment**: `{align}`\n"]
            for tf, info in data.get("timeframes", {}).items():
                if "error" in info:
                    lines.append(f"• **{tf}**: Error - {info['error']}")
                else:
                    lines.append(f"• **{tf.upper()}**: {info.get('trend')} | RSI: {info.get('rsi')} | Close: ${info.get('close')}")
            return "\n".join(lines)

        elif tool_name == "get_market_screener":
            dips = data.get("oversold_dips", [])
            moms = data.get("bullish_momentum", [])
            lines = ["🔎 **Market Quick Screener (1H)**\n"]
            if dips:
                lines.append("🟢 **Oversold / Dip Candidates (RSI ≤ 35):**")
                for d in dips:
                    lines.append(f"  • {d['coin']}: RSI {d['rsi']} (${d['price']})")
            else:
                lines.append("🟢 **Oversold Dips:** Tidak ada yang oversold ekstrem.")

            if moms:
                lines.append("\n🚀 **Bullish Momentum Setups (EMA9 > 21 & RSI 45-65):**")
                for m in moms:
                    lines.append(f"  • {m['coin']}: RSI {m['rsi']} (${m['price']})")
            return "\n".join(lines)

        elif tool_name == "get_tp_sl_plan":
            pair = data.get("pair", "")
            if "error" in data:
                return f"⚠️ Error calculating plan: {data['error']}"
            return (
                f"🎯 **Trading Plan: {pair}**\n"
                f"💵 Current / Entry: ${data.get('entry_reference')}\n"
                f"🛑 Stop Loss: ${data.get('stop_loss')} ({data.get('stop_loss_pct')})\n\n"
                f"🎯 **Targets:**\n"
                f"1️⃣ TP 1: ${data.get('take_profit_1', {}).get('price')} ({data.get('take_profit_1', {}).get('gain')}) — {data.get('take_profit_1', {}).get('action')}\n"
                f"2️⃣ TP 2: ${data.get('take_profit_2', {}).get('price')} ({data.get('take_profit_2', {}).get('gain')}) — {data.get('take_profit_2', {}).get('action')}\n"
                f"3️⃣ TP 3: ${data.get('take_profit_3', {}).get('price')} ({data.get('take_profit_3', {}).get('gain')}) — {data.get('take_profit_3', {}).get('action')}"
            )

        elif tool_name == "get_whale_walls":
            pair = data.get("pair", "")
            bid_wall = data.get("whale_bid_wall (Support)", {})
            ask_wall = data.get("whale_ask_wall (Resistance)", {})
            return (
                f"🐋 **Whale Walls & Depth Analysis: {pair}**\n"
                f"📊 Market Pressure: `{data.get('market_pressure')}` (Imbalance: {data.get('orderbook_imbalance_ratio')}x)\n"
                f"💰 Total Bids: {data.get('total_bid_liquidity_usd')} | Asks: {data.get('total_ask_liquidity_usd')}\n\n"
                f"🟢 **Whale Support Wall:** {bid_wall.get('price')} ({bid_wall.get('total_wall_usd')})\n"
                f"🔴 **Whale Resistance Wall:** {ask_wall.get('price')} ({ask_wall.get('total_wall_usd')})"
            )

        elif tool_name == "run_quick_backtest":
            pair = data.get("pair", "")
            return (
                f"📈 **Backtest Strategy V2: {pair} ({data.get('period_days')} Days)**\n"
                f"🕯️ Candles: {data.get('candles_analyzed')} | Trades: {data.get('total_trades')}\n"
                f"🎯 Win Rate: **{data.get('win_rate')}** ({data.get('wins')} Win / {data.get('losses')} Loss)\n"
                f"💰 Total Return: **{data.get('total_pnl_pct')}**\n"
                f"📊 Avg/Trade: {data.get('avg_trade_pnl')}"
            )

        elif tool_name == "check_positions":
            positions = data.get("positions", [])
            if not positions:
                return "📭 No open positions"
            lines = ["📦 *Open Positions:*"]
            for p in positions:
                pnl = p.get("pnl_pct", 0)
                emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(f"{emoji} {p['pair'].upper()}: {p['qty']} @ {p['entry_price']:,.0f} (PnL: {pnl:+.2f}%)")
            return "\n".join(lines)

        elif tool_name == "get_portfolio":
            return data.get("summary", str(data))

        elif tool_name == "sync_positions":
            cnt = data.get("positions_count", 0)
            positions = data.get("positions", {})
            lines = [f"🔄 **Sinkronisasi Binance myTrades Berhasil!**", f"Total posisi aktif terkawal: **{cnt}**\n"]
            for pair, p in positions.items():
                lines.append(f"• **{pair}**: {p.get('qty')} @ ${p.get('entry_price', 0):.4f} (🛑 SL: ${p.get('stop_loss', 0):.4f} | 🎯 TP: ${p.get('take_profit', 0):.4f})")
            return "\n".join(lines)

        elif tool_name == "get_crypto_news":
            from hermes.indicators.news_sentiment import format_news_telegram
            pair = tool_input.get("pair")
            return format_news_telegram(pair)

        else:
            return json.dumps(data, indent=2)

    except Exception as e:
        return f"⚠️ Error executing {tool_name}: {str(e)}"


# ─── Bot Handlers ──────────────────────────────────────────────────────────────

@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    welcome = (
        "🤖 **Hermes AI Trading Agent**\n\n"
        "Halo! Gue Hermes, AI autonomous trading assistant lo di Binance.\n\n"
        "**Fitur & Analisis Utama:**\n"
        "💰 `/spot` — Cek total aset Spot Binance & valuasi live USDT\n"
        "🔎 `/screener` — Scan koin momentum & dip oversold\n"
        "🐋 `/whale <coin>` — Deteksi tembok orderbook paus (Whale Walls)\n"
        "🎯 `/plan <coin>` — Kalkulator TP 1/2/3 & Stop Loss (ATR Volatility)\n"
        "📊 `/tf <coin>` — Analisis tren Multi-Timeframe (15m, 1h, 4h)\n"
        "📈 `/backtest <coin> 7` — Quick Backtest strategi V2 di data Binance\n"
        "🏆 `/rank` — Ranking skor sinyal 30 pair crypto\n"
        "😱 `/fear` — Crypto Fear & Greed Index live\n"
        "📊 `/status` — Ringkasan status & posisi bot\n\n"
        "Silakan gunakan tombol menu di bawah atau langsung ketik chat biasa!"
    )
    await update.message.reply_text(welcome, reply_markup=HERMES_KEYBOARD, parse_mode=ParseMode.MARKDOWN)


@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status — Quick portfolio + market summary."""
    chat_id = str(update.effective_chat.id)
    await update.message.reply_chat_action(ChatAction.TYPING)

    agent: HermesAgent = context.bot_data["agent"]
    response = await agent.chat(chat_id, "Kasih gue ringkasan cepat: balance USDT, posisi terbuka (kalau ada), Fear & Greed, dan market regime sekarang. Singkat aja.")
    await _send_long_message(update, response)


@authorized
async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /news [coin] — Show latest crypto news and sentiment."""
    await update.message.reply_chat_action(ChatAction.TYPING)
    args = context.args
    pair = args[0] if args else None
    from hermes.indicators.news_sentiment import format_news_telegram
    res = format_news_telegram(pair)
    await _send_long_message(update, res)


@authorized
async def cmd_learn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /learn — Trigger self-learning review."""
    chat_id = str(update.effective_chat.id)
    await update.message.reply_chat_action(ChatAction.TYPING)

    agent: HermesAgent = context.bot_data["agent"]
    response = await agent.chat(
        chat_id,
        "Review performa trading kita. Gunakan analyze_performance untuk lihat statistik. "
        "Kalau ada pattern menarik, save sebagai strategy note. "
        "Kasih insight apa yang bisa diperbaiki."
    )
    await _send_long_message(update, response)


@authorized
async def cmd_strategies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /strategies — Show learned strategies."""
    strategies = agent_memory.get_custom_strategies()

    if not strategies:
        await update.message.reply_text(
            "🧠 Belum ada strategy yang dipelajari.\n\n"
            "Gunakan /learn untuk mulai self-learning, atau chat:\n"
            "_\"bikin strategy berdasarkan data trading\"_",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    lines = ["🧠 **Strategy yang Sudah Dipelajari:**\n"]
    for i, s in enumerate(strategies, 1):
        conf_emoji = {"hypothesis": "💭", "observed": "👁️", "confirmed": "✅"}.get(s["confidence"], "❓")
        lines.append(f"{i}. {conf_emoji} [{s['confidence']}] {s['rule']}")
        if s.get("created_at"):
            lines.append(f"   📅 {s['created_at'][:10]}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@authorized
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /reset — Reset conversation history."""
    chat_id = str(update.effective_chat.id)
    agent: HermesAgent = context.bot_data["agent"]
    agent.reset_conversation(chat_id)
    await update.message.reply_text("🔄 Conversation reset! Mulai dari awal ya.")


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text messages — try direct command first, fall back to AI agent."""
    chat_id = str(update.effective_chat.id)
    user_text = update.message.text

    if not user_text:
        return

    log.info(f"[BOT] Message from {chat_id}: {user_text[:100]}...")

    # ─── 0. Check for pending confirmation response ───
    pending_cmds = context.bot_data.get("pending_cmds", {})
    if chat_id in pending_cmds:
        normalized = user_text.strip().lower()
        if normalized in ("ya", "yes", "ok", "oke", "confirm", "y", "gas", " lanjut", "eksekusi"):
            # Execute the pending command
            pending = pending_cmds.pop(chat_id)
            tool_name = pending["tool_name"]
            tool_input = pending["tool_input"]
            log.info(f"[BOT] Confirmed direct command: {tool_name}({tool_input})")
            await update.message.reply_chat_action(ChatAction.TYPING)
            result = _execute_direct_command(tool_name, tool_input)
            await _send_long_message(update, result)
            return
        elif normalized in ("batal", "cancel", "no", "tidak"):
            pending_cmds.pop(chat_id, None)
            await update.message.reply_text("❌ Cancelled.")
            return
        # Not a confirmation word — fall through to process as command

    # ─── 1. Try direct command first (no AI needed) ───
    direct = _parse_direct_command(user_text)
    if direct:
        tool_name, tool_input = direct
        log.info(f"[BOT] Direct command: {tool_name}({tool_input})")

        # Buy/Sell need confirmation
        if tool_name in CONFIRM_REQUIRED:
            pair = tool_input.get("pair", "?").upper()
            qty = tool_input.get("qty", 0)
            if tool_name == "execute_buy":
                msg = f"⚠️ **Konfirmasi BUY {pair}**\n\nMau eksekusi beli {pair} sekarang?\n\nKetik **ya** untuk konfirmasi atau **batal**."
            else:
                msg = f"⚠️ **Konfirmasi SELL {pair}** ({qty} coin)\n\nMau jual sekarang?\n\nKetik **ya** untuk konfirmasi atau **batal**."
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

            # Store pending confirmation in context for later
            context.bot_data.setdefault("pending_cmds", {})[chat_id] = {
                "tool_name": tool_name,
                "tool_input": tool_input,
            }
            return

        # Execute direct command
        await update.message.reply_chat_action(ChatAction.TYPING)
        result = _execute_direct_command(tool_name, tool_input)
        await _send_long_message(update, result)
        return

    # ─── 2. Fall back to AI agent ───
    await update.message.reply_chat_action(ChatAction.TYPING)

    agent: HermesAgent = context.bot_data["agent"]

    try:
        response = await agent.chat(chat_id, user_text)
        await _send_long_message(update, response)
    except Exception as e:
        log.error(f"[BOT] Error processing message: {e}")
        await update.message.reply_text(
            f"⚠️ Error: {str(e)}\n\nCoba lagi ya."
        )


# ─── Helpers ───────────────────────────────────────────────────────────────────

async def _send_long_message(update: Update, text: str, max_length: int = 4000):
    """Send a message, splitting into chunks if too long for Telegram's limit."""
    if not text:
        text = "🤔 (no response)"

    # Try sending with Markdown first, fallback to plain text
    chunks = _split_message(text, max_length)
    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            # Markdown parse error — send as plain text
            try:
                await update.message.reply_text(chunk)
            except Exception as e:
                log.error(f"[BOT] Failed to send message chunk: {e}")


def _split_message(text: str, max_length: int = 4000) -> list:
    """Split a long message into chunks."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break

        # Try to split at a newline
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1 or split_at < max_length // 2:
            split_at = max_length

        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")

    return chunks


# ─── Bot Runner ────────────────────────────────────────────────────────────────

async def run_telegram_bot():
    """Start the Telegram bot with polling."""
    if not TELEGRAM_BOT_TOKEN:
        log.error("[BOT] TELEGRAM_BOT_TOKEN not set! Cannot start bot.")
        return

    if not TELEGRAM_CHAT_ID:
        log.error("[BOT] TELEGRAM_CHAT_ID not set! Cannot authorize users.")
        return

    log.info("[BOT] Initializing Hermes AI Trading Agent...")

    # Initialize the AI agent
    agent = HermesAgent()

    # Build the Telegram application
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Store agent in bot_data for access in handlers
    app.bot_data["agent"] = agent

    # Set bot commands (visible in Telegram UI)
    await app.bot.set_my_commands([
        BotCommand("spot", "Cek total saldo Spot & valuasi live USDT"),
        BotCommand("futures", "Cek posisi & margin balance Futures"),
        BotCommand("news", "Berita crypto & sentimen pasar AI terkini"),
        BotCommand("screener", "Scan koin bullish momentum & oversold dips"),
        BotCommand("fear", "Cek Crypto Fear & Greed Index live"),
        BotCommand("rank", "Ranking skor 30 pair crypto terbaik"),
        BotCommand("status", "Ringkasan status daemon & open posisi"),
        BotCommand("start", "Menu utama & refresh tombol shortcut"),
        BotCommand("reset", "Reset history obrolan dengan AI agent"),
    ])

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("learn", cmd_learn))
    app.add_handler(CommandHandler("strategies", cmd_strategies))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT, handle_message))

    log.info(f"[BOT] Starting Telegram bot (authorized chat: {TELEGRAM_CHAT_ID})")
    log.info("[BOT] Hermes AI Agent is ready! Send a message on Telegram.")

    # Run with polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Keep running
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("[BOT] Shutting down...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def start_bot():
    """Entry point for starting the bot (synchronous wrapper)."""
    asyncio.run(run_telegram_bot())
