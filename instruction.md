# Hermes Trading System — Agent Instructions

## Overview
Hermes is an autonomous crypto trading system for Binance. It runs as a daemon that monitors pairs, executes trades, and manages a portfolio. The AI agent ("Hermes") interacts via Telegram.

**You are the AI agent controlling this system.** Your job is to help the owner ("Badra") trade crypto via Telegram chat.

---

## Quick Reference

### Starting the System
```bash
cd C:/BADRA/new-trading-system
PYTHONPATH=. python hermes/cli.py daemon [--dry-run]  # Full daemon
PYTHONPATH=. python hermes/cli.py one-shot [--dry-run] # Single trading cycle
PYTHONPATH=. python hermes/cli.py agent                 # Telegram AI agent only
PYTHONPATH=. python hermes/cli.py rank-pairs            # CLI: rank all pairs
PYTHONPATH=. python hermes/cli.py get-balance            # Check balance
PYTHONPATH=. python hermes/cli.py get-price <PAIR>      # Get price
```

### Key Files
| File | Purpose |
|------|---------|
| `hermes/trading/execution.py` | Buy/sell execution on Binance |
| `hermes/trading/positions.py` | Open position monitoring (TP/SL/Trailing) |
| `hermes/indicators/strategy_new.py` | Strategy V2 signal generation |
| `hermes/indicators/rsi.py` | RSI calculation (Wilder smoothing) |
| `hermes/api/websocket.py` | Real-time WebSocket price feed (Binance streams) |
| `hermes/api/rest.py` | REST API calls (prices, candles, Binance) |
| `hermes/agent/agent.py` | AI agent (MiniMax M2.7) |
| `hermes/agent/tools.py` | Agent function tools (15 tools) |
| `hermes/agent/memory.py` | Self-learning memory |
| `hermes/daemon/tasks.py` | Daemon loops |
| `hermes/state.py` | Global state (positions, prices, RSI) |
| `hermes/notifications/telegram.py` | Telegram notifications |

### Key State
- `state.positions` — open positions: `{pair: {entry_price, qty, time, stop_loss, take_profit, peak_price}}`
- `state.prices` — live WS prices: `{pair: {price, bid, ask, high, low, source, updated}}`
- `state.rsi_state` — RSI Wilder state: `{pair: {avg_gain, avg_loss, last_price, initialized}}`
- `agent_memory.outcomes` — closed trade outcomes for performance analysis

---

## How Trades Work

### Buy Flow
1. Daemon `daemon_trade_check_v2()` finds LONG signal with confidence
2. Calls `execute_buy(pair, price, idr_balance)` in `execution.py`
3. Position saved to `state.positions[pair]`
4. `telegram_trade_alert()` sent with BUY details
5. `log_trade_decision()` called in `agent.py` (before confirmation, if via AI agent)

### Sell Flow
1. `check_open_positions()` monitors prices every tick
2. TP hit → `execute_sell(pair, price, qty, reason="tp")`
3. After sell success:
   - `log_trade_outcome()` → saves PnL to `agent_memory.outcomes`
   - `telegram_trade_alert()` → sends SELL notification
4. Position deleted from `state.positions`

### Exit Reasons
- `"tp"` — Take Profit hit
- `"sl"` — Stop Loss hit
- `"trailing"` — Trailing stop hit
- `"signal_sell"` — Strategy V2 SELL signal
- `"rebalance_drift"` — Portfolio rebalancing
- `"manual"` — Manual sell via agent

---

## Important Patterns

### RSI Calculation
- **3m RSI**: Updated live from WS price ticks via `update_rsi(pair, price)` — no REST needed
- **1h/4h RSI**: Fetched from `/tradingview/history_v2` via `fetch_candles()` — uses REST budget
- RSI formula: Wilder's smoothed RSI (not simple SMA)
- `get_rsi(pair)` reads from `state.rsi_state` (read-only, no update)

### Strategy V2 Signal
```
LONG when: RSI <= 35 AND daily_pos <= 30% AND (RSI direction up/neutral)
SHORT when: RSI >= 65 AND daily_pos >= 70% AND (RSI direction down/neutral)
NO TRADE SETUP: otherwise
Confidence: High (>=0.8) / Medium (>=0.6) / Low
```

### Agent Tools (for AI agent)
- `get_price(pair)` — live WS price
- `get_signal(pair)` — V1 signal (legacy)
- `get_signal_v2(pair)` — V2 signal with entry/SL/TP/confidence
- `get_balance()` — account balance
- `get_portfolio()` — open positions with PnL
- `get_fear_greed()` — F&G index
- `rank_pairs()` — all pairs ranked by score
- `execute_buy(pair)` — **requires "ya" confirmation**
- `execute_sell(pair, qty)` — **requires "ya" confirmation**
- `analyze_performance()` — trade history from `agent_memory.outcomes`
- `save_strategy_note()` — agent saves custom strategy

### Performance Tracking
Trade outcomes are tracked:
- `execute_sell()` calls `log_trade_outcome()` → saved to `hermes_agent_memory.json`
- `analyze_performance()` reads from `agent_memory.outcomes` → shows win rate, avg PnL, best/worst trade
- Telegram alerts sent on every buy/sell

---

## Common Issues

### RSI Stuck at 50.0 or 100.0
- **3m RSI stuck at 100**: Stale cache from previous run. Fix: restart daemon (cache cleared on startup).
- **1h/4h RSI at 50**: `/tradingview/history_v2` returned empty or 404. Check REST budget.

### 401 Auth Errors
- MiniMax API requires `anthropic-version: 2023-06-01` header — fixed in `agent.py`
- If Telegram bot fails, check `MINIMAX_API_KEY` in `.env`

### REST 429 Errors
- Budget limit: 1200 requests/minute to Binance REST API
- Daemon uses WS prices first, only falls back to REST if WS fails
- `DAEMON_REBALANCE_INTERVAL = 21600` (6 hours) to stay within limits

### Telegram Bot Not Responding
- Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`
- Bot only responds to `TELEGRAM_CHAT_ID`
- Run `python -m hermes.cli agent` for AI chat mode

---

## Config Values (from `config.py`)
```
MIN_TRADE_USDT = 1           # Minimum trade in USDT
MAX_TRADE_USDT = 100         # Max per trade
STOP_LOSS_PCT = 0.05         # 5% stop loss
TAKE_PROFIT_PCT = 0.10       # 10% take profit
TRAILING_STOP_PCT = 0.03     # 3% trailing stop
TRAILING_ACTIVATION_PCT = 0.05  # Activate after 5% gain
FG_BUY_THRESHOLD = 30        # F&G <= 30 → buy zone
RSI_BUY_THRESHOLD = 35
RSI_STRONG_BUY = 30
RSI_SELL_THRESHOLD = 65
TESTNET = false              # Set true in .env to use Binance Testnet
```

---

## File Locations
```
hermes/
  agent/
    agent.py          # AI agent (MiniMax M2.7)
    tools.py          # 15 function tools for agent
    memory.py         # Self-learning: log_trade_decision(), log_trade_outcome(), get_performance_summary()
    bot.py            # Telegram bot polling
  api/
    rest.py           # REST API calls (fetch_candles, fetch_price_rest, update_price)
    websocket.py      # WS client (Binance streams)
    auth.py           # Binance HMAC-SHA256 signed API calls
  indicators/
    rsi.py            # RSI calculation
    signals.py        # V1 signals
    strategy_new.py   # Strategy V2
    fear_greed.py     # Fear & Greed index
    candles.py        # EMA, swing levels, ATR
    volatility.py     # Dynamic position sizing
    technicals.py    # MACD, Bollinger Bands, Supertrend, ATR, ADX (NEW)
  trading/
    execution.py      # Buy/sell on Binance
    positions.py      # TP/SL/Trailing monitoring
    rebalancer.py    # Portfolio rebalancing
    dca.py           # DCA and Grid trading (NEW)
    risk.py          # Monte Carlo + Kelly criterion (NEW)
  daemon/
    tasks.py          # Daemon loops
  notifications/
    telegram.py       # Telegram alerts
  state.py            # Global state singleton
  config.py           # All config values
  logging_setup.py    # Logging config
backtesting.py       # Backtesting engine (NEW)
```

---

## DRY_RUN Mode

To test the system without risking real money:

```bash
# Dry-run daemon (simulated trading)
python hermes/cli.py daemon --dry-run

# Dry-run one-shot
python hermes/cli.py one-shot --dry-run
```

The agent should use `--dry-run` flag when instructed to test or verify trading behavior. Telegram alerts still fire in dry-run mode so the agent can verify notification flow.

**When to use:**
- Testing new strategies
- Verifying Telegram alerts work
- Checking system behavior without real money at risk

---

## Telegram Bot Commands

When running the bot (`python hermes/cli.py agent`), available commands:

| Command | Description |
|---------|-------------|
| `/start` | Welcome message & capabilities |
| `/status` | Quick portfolio summary (positions, balance, PnL) |
| `/learn` | Self-learning review of past trades |
| `/strategies` | View learned strategies |
| `/reset` | Reset conversation context |
| *(free text)* | Chat with AI agent (MiniMax M2.7) |

The agent can also execute trades, check prices, analyze performance via natural language.

---

## IDR to USDT Workflow

Hermes trades on **Binance spot market** using **USDT pairs** (DOGEUSDT, BTCUSDT, etc.).

**To add IDR balance:**

1. **Buy USDT with IDR** via Binance P2P:
   - Go to Binance → P2P Trading
   - Buy USDT using IDR bank transfer
   - This deposits USDT to your Binance spot wallet

2. **No pair changes needed** — Hermes automatically uses USDT pairs

3. **Important**: `PAIR` in `.env` is only for CLI default, NOT for daemon trading. Daemon uses `ALL_TRACKED` pairs from config.

---

## Dynamic Pair Addition

The agent can add pairs dynamically without restart:

```python
from hermes.api.websocket import ws_add_pair, ws_get_pairs

# Add a new pair to WS subscription
result = ws_add_pair('NEWP AIR')

# Get current subscribed pairs
pairs = ws_get_pairs()
```

This reconnects the WebSocket to include the new pair. Useful when the agent is instructed to track a new coin.
