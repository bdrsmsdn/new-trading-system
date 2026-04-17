# Hermes Trading System — Agent Instructions

## Overview
You are controlling Hermes, an autonomous crypto trading system on Indodax (Indonesian exchange).
All amounts are in Indonesian Rupiah (IDR). Minimum trade size: Rp 10,000.

## Available Commands
All commands: `python -m hermes.cli <command> [args]`
All outputs are **JSON to stdout**. Logs go to stderr/files.

### Market Data
| Command | Description | Example |
|---------|-------------|---------|
| `fear-greed` | Get Fear & Greed index (0-100) | `python -m hermes.cli fear-greed` |
| `get-price <pair>` | Get live price for a pair | `python -m hermes.cli get-price doge` |
| `market-regime` | Get current regime (BULL/BEAR/SIDEWAYS) | `python -m hermes.cli market-regime` |

### Analysis (Legacy Strategy)
| Command | Description | Example |
|---------|-------------|---------|
| `get-signal <pair>` | Get signal + score for a pair | `python -m hermes.cli get-signal doge` |
| `rank-pairs` | Rank all 30 pairs by signal score | `python -m hermes.cli rank-pairs` |
| `analyze --pair <pair>` | Full analysis of one pair | `python -m hermes.cli analyze --pair doge` |
| `analyze --all` | Full analysis of all pairs | `python -m hermes.cli analyze --all` |

### Analysis V2 (RSI + EMA + Orderbook)
> **Professional-grade strategy** using RSI(14), EMA(9/21) crossover, and orderbook analysis.
> Recommended over legacy strategy for better signal quality.

| Command | Description | Example |
|---------|-------------|---------|
| `signal-v2 --pair <pair>` | V2 signal for one pair | `python -m hermes.cli signal-v2 --pair doge` |
| `signal-v2 --all` | V2 signal for all pairs | `python -m hermes.cli signal-v2 --all` |
| `signal-v2 --pair <pair> --risk 0.02` | Custom risk % (default: 1%) | `python -m hermes.cli signal-v2 --pair doge --risk 0.02` |

**V2 Signal Interpretation:**
- **LONG**: All conditions met — RSI ≤30 OR exiting oversold + EMA9 crosses above EMA21 + price above both EMAs
- **SHORT**: All conditions met — RSI ≥70 OR exiting overbought + EMA9 crosses below EMA21 + price below both EMAs
- **NO TRADE SETUP**: Conditions not aligned

**V2 Output Fields:**
- `signal_type`: LONG / SHORT / NO TRADE SETUP
- `entry_price`, `stop_loss`, `take_profit_1/2/3`
- `rsi_value`, `ema_9`, `ema_21`, `trend_bias`
- `signal_confidence`: Low / Medium / High
- `orderbook_imbalance`: bid_vol/ask_vol ratio (>1 = bullish pressure, <1 = bearish)
- `risk_percent`, `position_size`
- `reason`: Explanation of why signal is valid

### Portfolio
| Command | Description | Example |
|---------|-------------|---------|
| `get-balance` | Get IDR + all coin balances | `python -m hermes.cli get-balance` |
| `portfolio` | Full portfolio dashboard (JSON) | `python -m hermes.cli portfolio` |
| `state` | View positions, active pairs | `python -m hermes.cli state` |
| `check-positions` | Check TP/SL on open positions | `python -m hermes.cli check-positions` |

### Trading
| Command | Description | Example |
|---------|-------------|---------|
| `execute-buy <pair>` | Buy with auto price + sizing | `python -m hermes.cli execute-buy doge` |
| `execute-buy <pair> --price N` | Buy at specific price | `python -m hermes.cli execute-buy doge --price 1600` |
| `execute-sell <pair> --qty N` | Sell specific quantity | `python -m hermes.cli execute-sell doge --qty 6.0` |

### Autonomous
| Command | Description |
|---------|-------------|
| `daemon` | Start autonomous trading (runs forever) |
| `one-shot` | Single trading iteration then exit |

## Signal Interpretation
- **STRONG_BUY** (score ≥5): High confidence entry
- **BUY** (score ≥3): Good entry
- **WEAK_BUY** (score 1-2): Marginal, probably skip
- **HOLD** (score 0): No action
- **SELL** (score ≤-1): Consider exiting
- **STRONG_SELL** (score ≤-3): Exit immediately

## Rate Limit Awareness
⚠️ Indodax limits public API to ~150 req/min.
- ❌ Do NOT call `analyze --all` more than once per 5 minutes
- ❌ Do NOT call `get-signal` for all 30 pairs in rapid succession
- ✅ Use `rank-pairs` instead (batched, cached)
- ✅ `get-price` is cheap (uses WebSocket cache when daemon is running)
- ✅ `get-balance` is cached for 120s automatically

## Common Workflows

### "Should I buy X?"
1. `fear-greed` → Check market sentiment
2. `get-signal <pair>` → Get signal + score
3. If BUY/STRONG_BUY → `execute-buy <pair>`

### "What's my portfolio status?"
1. `portfolio` → Full dashboard with P&L

### "What are the best opportunities right now?"
1. `rank-pairs` → See all pairs ranked by signal score
2. Focus on top 3-5 with BUY+ signals

### "Check if I should take profit"
1. `check-positions` → Auto-checks TP/SL/trailing for all positions

## Error Handling
- If a command returns `{"error": "..."}`, the operation failed
- `{"error": "rate_limited"}` → Wait 60s before retrying
- `{"error": "insufficient_balance"}` → IDR balance too low for trade
- `{"error": "no_price"}` → Price data unavailable, try again later

## Tracked Pairs (30 total)
doge, xrp, ton, sol, btc, eth, bnb, pepe, neirocto, floki,
shib, ada, matic, link, avax, dot, bonk, dogewif, labu, orto,
near, algo, trx, sand, mana, axs, enj, ftm, atom, uni
