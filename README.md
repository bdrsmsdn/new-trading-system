# Hermes Trading Bot

Autonomous crypto trading bot for Binance, powered by AI (MiniMax) and technical analysis.

## Features

- **Autonomous Trading**: Runs 24/7 as a daemon, automatically finding entry/exit points
- **AI Agent**: Natural language Telegram bot powered by MiniMax-M2.7
- **Strategy V2**: RSI(14) + Daily Position + Orderbook imbalance analysis
- **Dry-Run Mode**: Test strategies without risking real money
- **Market Regime Adaptation**: Adjusts behavior based on Fear & Greed index
- **Dynamic Pair Selection**: Auto-ranks and rebalances across 30 pairs

## Requirements

- Python 3.10+
- Binance API key (with trade permissions)
- Optional: Telegram bot token + chat ID (for AI agent mode)
- Optional: MiniMax API key (for AI agent)

## Setup

1. **Clone the repo**
2. **Copy the environment template:**
   ```
   cp .env.example .env
   ```
3. **Edit `.env`** and fill in your credentials:
   - `API_KEY` - Binance API key
   - `API_SECRET` - Binance API secret
   - `TESTNET=false` - Set to `true` for Binance Testnet
   - `TELEGRAM_BOT_TOKEN` - Telegram bot token (optional, for agent mode)
   - `TELEGRAM_CHAT_ID` - Your Telegram chat ID (optional, for agent mode)
   - `MINIMAX_API_KEY` - MiniMax API key (optional, for agent mode)

4. **Install dependencies:**
   ```bash
   pip install asyncio websockets python-telegram-bot python-dotenv requests pandas numpy
   ```

## Usage

### Dry-Run (No Real Trading)

```bash
# Run daemon in dry-run mode
python -m hermes.cli daemon --dry-run

# Single trade check in dry-run mode
python -m hermes.cli one-shot --dry-run
```
Dry-run mode does NOT execute real trades. It still requires API keys to fetch market data, but will never place orders.

### Real Trading

```bash
python -m hermes.cli daemon
```

### With AI Agent (Telegram Bot)

Requires `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and `MINIMAX_API_KEY` in `.env`:

```bash
python -m hermes.cli daemon --with-agent
```

Or start the AI agent standalone:

```bash
python -m hermes.cli agent
```

### CLI Commands

| Command | Description |
|---------|-------------|
| `daemon [--dry-run] [--with-agent]` | Run autonomous trading daemon |
| `one-shot [--dry-run]` | Single trading iteration |
| `signal-v2 --pair BTCUSDT --all` | Get V2 trading signal |
| `analyze --pair BTCUSDT --all` | Run technical analysis |
| `portfolio` | Show open positions |
| `check-positions` | Check TP/SL status |
| `fear-greed` | Get Fear & Greed index |
| `market-regime` | Get market regime |
| `rank-pairs` | Rank all pairs by score |
| `agent` | Start Telegram AI chatbot |

## Strategy V2

The bot uses RSI(14) + Daily Position + Orderbook imbalance for signal generation:

- **LONG**: RSI ≤ 35 + Daily Position ≤ 30% + RSI momentum up + bid volume > ask volume
- **SHORT**: RSI ≥ 65 + Daily Position ≥ 70% + RSI momentum down + ask volume > bid volume

Risk management: 1-2% per trade, 1:1/1:2/1:3 R:R take-profit levels.

## Disclaimer

This software is provided "as is" without warranty of any kind.
Cryptocurrency trading involves substantial risk of loss. Past performance is not indicative of future results.
This bot may execute real trades on your Binance account. Use at your own risk.
The authors and contributors are not responsible for any financial losses incurred while using this software.
Always test with dry-run mode first.
