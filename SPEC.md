# Crypto Trading System — Architecture SPEC

**Updated:** 2026-04-18
**Exchange:** Binance
**Owner:** Hermes Agent (Badra's personal trading agent)

---

## 1. System Overview

Single unified trading system: `hermes_trader.py` (Python)

| Component | File | Type |
|-----------|------|------|
| Trading Engine | `hermes_trader.py` | Python asyncio daemon |
| Real-time Feed | Built-in `BinanceWS` class | Native websockets (wss://stream.binance.com:9443/stream) |
| State | `hermes_trader_state.json` | Persistent JSON |

> **Note:** Previous architecture with `hermes_daemon.py`, `crypto-trading-system.js`, and openclaw workspace is **DEPRECATED** (deleted 2026-04-13).

---

## 2. Exchange Overview

| Item | Value |
|------|-------|
| WebSocket URL | `wss://stream.binance.com:9443/stream` (combined streams) |
| REST URL | `https://api.binance.com/api/v3/` |
| Rate Limit | 1200 requests/minute |
| Pair Format | `<COIN>USDT` (e.g., DOGEUSDT, BTCUSDT) |

---

## 2. Credentials

| Item | Value |
|------|-------|
| API Key | (from .env) |
| API Secret | (from .env) |
| Nonce | Not required (Binance uses timestamp-based signatures) |

---

## 3. Pair Architecture — Dynamic, Auto-Adjusting

```
TRACKED_PAIRS (universe) ─── All pairs we monitor
    ├── WS_PAIRS (15) ─────── Real-time WebSocket feeds
    │      doge, xrp, ton, sol, btc, eth, bnb,
    │      pepe, neirocto, floki, shib, ada, matic, link, avax
    └── REST_PAIRS (15) ───── Polled via REST API (fallback/lower freq)
           dot, bonk, dogewif, labu, orto,
           near, algo, trx, sand, mana, axs, enj, ftm, atom, uni

ACTIVE_PAIRS ──────────────── Dynamically managed by daemon
    - Auto-selected every 5 minutes based on signal scores
    - Max 8 pairs simultaneously (adjustable per regime)
    - Pairs with open positions are always kept active
    - WS_PRIORITY pairs never demoted to REST
```

### Modes

```
python3 hermes_trader.py --daemon     # Continuous daemon (all features)
python3 hermes_trader.py --one-shot  # Single iteration + trade check
python3 hermes_trader.py --analyze   # Analysis only, no trading
```

---

## 4. Trading Strategy

### Indicators
| Indicator | Source | Used For |
|-----------|--------|---------|
| Fear & Greed Index | alternative.me API | Market regime + entry timing |
| RSI(3) | Wilder's smoothing | Short-term momentum |
| Daily Position | `(price - low) / (high - low) * 100` | Entry/exit timing |

### Entry/Exit Rules
| Rule | Threshold |
|------|-----------|
| BUY | F&G ≤ 30 + RSI ≤ 35 + daily position < 30% |
| STRONG BUY | F&G ≤ 20 + RSI(3m) ≤ 30 + RSI(1h) ≤ 40 |
| Take Profit | +10% from entry |
| Stop Loss | -5% from entry (or regime-adjusted) |
| Trailing SL | Activates at +5% profit, trails 20% from peak |

### Position Sizing
- **Max trade:** $100 USDT per execution
- **Min trade:** $10 USDT
- **Kelly Criterion:** Used for dynamic sizing (capped 10%)
- **Cooldown:** 60 seconds per pair between trades

### Strategy V2 (RSI + EMA + Orderbook) ⚡ NEW
> Professional-grade strategy combining RSI, EMA crossover, and orderbook analysis.

**Indicators:**
| Indicator | Parameter | Purpose |
|-----------|-----------|---------|
| RSI | Period 14 | Overbought/oversold detection |
| EMA | 9, 21 | Trend direction + crossover |
| Orderbook | Bid/Ask depth | Support/resistance strength |

**LONG Setup (all must be true):**
1. RSI ≤ 30 (oversold) OR RSI exiting oversold zone
2. EMA 9 crosses ABOVE EMA 21 (bullish crossover)
3. Price closes above both EMA 9 and EMA 21
4. RSI moving upward (confirming momentum)
5. Orderbook: bid volume > ask volume (bullish pressure)

**SHORT Setup (all must be true):**
1. RSI ≥ 70 (overbought) OR RSI exiting overbought zone
2. EMA 9 crosses BELOW EMA 21 (bearish crossover)
3. Price closes below both EMA 9 and EMA 21
4. RSI moving downward (confirming momentum)
5. Orderbook: ask volume > bid volume (bearish pressure)

**Signal Filters:**
- Avoid signals during extremely low volatility
- Ignore signals if EMA crossover happened > 3 candles ago
- Only produce signals when RSI + EMA conditions align

**Risk Management:**
- Risk per trade: 1-2% of capital (configurable via `--risk`)
- Stop Loss: below recent swing low (LONG) / above recent swing high (SHORT)
- Take Profit: TP1 = 1:1 R:R, TP2 = 1:2 R:R, TP3 = 1:3 R:R

**Orderbook Imbalance:**
- `imbalance = bid_vol / ask_vol`
- > 1.0 = bullish pressure (buyers aggressive)
- < 1.0 = bearish pressure (sellers aggressive)
- Combined with LONG/SHORT signal for confidence boost

**Output:**

---

## 5. Market Regime System

| Regime | F&G | Max Active Pairs | Position Size | Sell Discipline |
|--------|-----|-------------------|---------------|-----------------|
| 🐂 BULL | ≥ 60 | 12 (1.5x) | 1.2x normal | Aggressive — sell on strength |
| ↔️ SIDEWAYS | 31-59 | 8 (1.0x) | 0.8x normal | Normal — take profit at target |
| 🐻 BEAR | ≤ 30 | 4 (0.5x) | 0.5x normal | Defensive — no new sells, cut losses fast |

---

## 6. Autonomous Features

### Dynamic Pair Selection
- Every 5 minutes: daemon re-ranks ALL tracked pairs by signal score
- Top-scoring pairs promoted to ACTIVE_PAIRS
- Pairs with open positions are locked in
- In BEAR mode: only score ≥ 3 (BUY+) pairs are traded

### Portfolio Rebalancer
- Runs every 6 hours
- Checks allocation drift per position
- Triggers partial profit-taking if drift > 20% and profit ≥ 5%
- Skipped in BEAR regime (preserve positions)
- Equal-weight target allocation

### Auto Pair Promotion
- Pairs not in WS_PAIRS can be promoted if they show strong signals
- REST pairs with score ≥ 5 get polled more frequently

---

## 7. Daemon Tasks

| Task | Interval | Description |
|------|----------|-------------|
| `ws_client.run_forever()` | Real-time | WebSocket listener for WS pairs |
| `daemon_price_poll()` | 60s | Poll REST pairs only |
| `daemon_trade_check()` | 30s | Check TP/SL + find entries |
| `daemon_fg_fetch()` | 300s | Refresh Fear & Greed |
| `daemon_pair_reassess()` | 300s | Re-rank and adjust ACTIVE_PAIRS |
| `daemon_rebalance()` | 21600s (6h) | Check portfolio drift |
| `daemon_morning_brief()` | Daily 07:00 WIB | Send morning brief |

---

## 8. Files

```
/home/badra/.hermes/trading/
├── hermes_trader.py           # Main script (~1,800 lines)
├── hermes_trader_state.json   # Persistent state
├── hermes_prices.json         # Price cache
├── daemon.log                 # Daemon output log
├── daemon_prices.json         # Daemon price cache
├── daemon_trades.log          # Trade log
├── hermes_trades.log          # All trades
├── hermes_trader_state.json   # Positions + active pairs
├── .env                       # API credentials
└── .nonce                     # Timestamp nonce
```

---

## 9. State Schema

```json
{
  "positions": {
    "DOGEUSDT": {
      "entry_price": 0.1234,
      "qty": 6.0,
      "time": 1713180000,
      "stop_loss": 0.1173,
      "take_profit": 0.1357,
      "peak_price": 0.1280
    }
  },
  "last_trade_time": {"DOGEUSDT": 1713180000},
  "active_pairs": ["DOGEUSDT", "XRPUSDT", "TONUSDT", "SOLUSDT", "BTCUSDT", "ETHUSDT"]
}
```

---

## 10. Troubleshooting

### Binance Rate Limiting
- Combined streams endpoint: 1200 requests/minute
- WebSocket connections: real-time prices, no polling needed
- REST pairs polled at 60s interval to stay within limits

### Timestamp Sync
- Binance requires accurate server time for signature
- System clock drift can cause authentication failures
- Ensure NTP sync is enabled on the trading host

### Common Issues
- **WS disconnection**: Auto-reconnect with exponential backoff
- **Auth failures**: Check API key permissions and time sync
- **429 Too Many Requests**: Reduce polling frequency

---

## DRY_RUN Mode

Run `./hermes/cli.py daemon --dry-run` or `./hermes/cli.py one-shot --dry-run` to simulate trading without real order execution. Useful for testing strategies and verifying Telegram alerts work correctly.

**How it works:**
- No Binance API calls are made — orders are simulated only
- State is still updated (positions tracked, last_trade_time updated) so dry-run outcome is observable
- Telegram alerts still fire so notification pipeline can be verified
- All price deviation warnings still log

**CLI examples:**
```bash
python hermes/cli.py daemon --dry-run    # dry-run daemon
python hermes/cli.py one-shot --dry-run # dry-run one-shot
```

---

## Security Notes

- API signatures are never logged (query strings stripped from log URLs)
- Binance error responses are sanitized (error codes only, no raw response in logs)
- Price deviation >2% triggers warning before market orders
- Telegram rate limited to 3 messages per minute
