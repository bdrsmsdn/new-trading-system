"""
Hermes Telegram Bot — Interactive AI trading chatbot.

Connects the MiniMax-powered Hermes agent to Telegram,
allowing natural language interaction for trading operations.
"""

import asyncio
import logging
from telegram import Update, BotCommand
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
                "⛔ Unauthorized. Bot ini hanya untuk Badra."
            )
            log.warning(f"[BOT] Unauthorized access from chat_id={chat_id}")
            return
        return await func(update, context)
    return wrapper


# ─── Bot Handlers ──────────────────────────────────────────────────────────────

@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    welcome = (
        "🤖 **Hermes AI Trading Agent**\n\n"
        "Halo Badra! Gue Hermes, AI trading assistant lo.\n\n"
        "**Yang bisa gue lakuin:**\n"
        "📊 Cek market & sentiment (F&G, regime)\n"
        "📈 Analisis pair & signal trading\n"
        "💰 Cek portfolio & balance\n"
        "🛒 Eksekusi buy/sell (dengan konfirmasi)\n"
        "🧠 Self-learning dari trade history\n"
        "🎯 Bikin strategy sendiri\n\n"
        "**Commands:**\n"
        "/status — Quick portfolio summary\n"
        "/learn — Review & learn dari trade history\n"
        "/strategies — Lihat strategy yang udah dipelajari\n"
        "/reset — Reset conversation\n\n"
        "Atau langsung chat aja natural, misal:\n"
        "• _\"Market gimana sekarang?\"_\n"
        "• _\"Rekomendasiin pair buat dibeli\"_\n"
        "• _\"Beli DOGE\"_\n"
        "• _\"Berapa portfolio gue?\"_"
    )
    await update.message.reply_text(welcome, parse_mode=ParseMode.MARKDOWN)


@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status — Quick portfolio + market summary."""
    chat_id = str(update.effective_chat.id)
    await update.message.reply_chat_action(ChatAction.TYPING)

    agent: HermesAgent = context.bot_data["agent"]
    response = await agent.chat(chat_id, "Kasih gue ringkasan cepat: balance USDT, posisi terbuka (kalau ada), Fear & Greed, dan market regime sekarang. Singkat aja.")
    await _send_long_message(update, response)


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
    """Handle free-text messages — forward to AI agent."""
    chat_id = str(update.effective_chat.id)
    user_text = update.message.text

    if not user_text:
        return

    log.info(f"[BOT] Message from {chat_id}: {user_text[:100]}...")

    # Show typing indicator
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
        BotCommand("start", "Welcome & capabilities"),
        BotCommand("status", "Quick portfolio summary"),
        BotCommand("learn", "Self-learning review"),
        BotCommand("strategies", "View learned strategies"),
        BotCommand("reset", "Reset conversation"),
    ])

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("learn", cmd_learn))
    app.add_handler(CommandHandler("strategies", cmd_strategies))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

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
