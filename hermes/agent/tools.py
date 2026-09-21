"""
Hermes Agent Tools — Function definitions for CosmosHub function calling.

Defines all tools the AI agent can invoke to interact with the Hermes trading system.
Uses OpenAI-compatible tool format (function calling).
"""

import json
import asyncio
from typing import Any, Dict, Optional

from hermes.logging_setup import log

# ─── Tool Definitions (OpenAI function calling format) ────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_price",
            "description": "Get the current live price for a crypto trading pair on Binance. Returns the price in USDT.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {
                        "type": "string",
                        "description": "The trading pair symbol, e.g. 'doge', 'btc', 'eth', 'xrp', 'sol'"
                    }
                },
                "required": ["pair"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_signal",
            "description": "Get a trading signal (STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL) and score for a specific crypto pair using the legacy F&G+RSI strategy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {
                        "type": "string",
                        "description": "The trading pair symbol, e.g. 'doge', 'btc', 'eth'"
                    }
                },
                "required": ["pair"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_signal_v2",
            "description": "Get a professional-grade trading signal using Strategy V2 (RSI + Orderbook + Momentum). Returns LONG/SHORT/NO TRADE SETUP with confidence level, entry price, stop loss, and take profit levels. This is the recommended strategy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {
                        "type": "string",
                        "description": "The trading pair symbol, e.g. 'doge', 'btc'"
                    },
                    "risk_pct": {
                        "type": "number",
                        "description": "Risk percentage per trade (0.01 = 1%). Default: 0.01"
                    }
                },
                "required": ["pair"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_balance",
            "description": "Get the current account balance including USDT cash balance and all coin holdings on Binance.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio",
            "description": "Get a full portfolio dashboard showing all positions with current P&L, entry prices, current values, and overall portfolio performance.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_fear_greed",
            "description": "Get the current Crypto Fear & Greed Index (0-100). Values ≤30 = Fear (good to buy), ≥60 = Greed (consider selling). This is a key market sentiment indicator.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_regime",
            "description": "Get the current market regime: BULL (F&G ≥60), SIDEWAYS (31-59), or BEAR (≤30). The regime affects position sizing and active pair count.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "rank_pairs",
            "description": "Rank all 30 tracked crypto pairs by signal score. Returns pairs sorted from best to worst trading opportunity. Use this to find the best pairs to trade right now.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_positions",
            "description": "Check all open positions for take-profit and stop-loss conditions. Automatically closes positions if TP/SL levels are hit.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_state",
            "description": "View the current system state: open positions (with entry prices, quantities, TP/SL levels), active trading pairs, and Fear & Greed values.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_buy",
            "description": "Execute a BUY order for a crypto pair on Binance. Uses dynamic position sizing based on volatility. IMPORTANT: This executes a REAL trade with real money. The minimum trade is $1.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {
                        "type": "string",
                        "description": "The trading pair to buy, e.g. 'doge', 'btc', 'eth'"
                    },
                    "price": {
                        "type": "number",
                        "description": "Optional specific price. If not provided, uses current market price."
                    }
                },
                "required": ["pair"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_sell",
            "description": "Execute a SELL order for a crypto pair on Binance. IMPORTANT: This executes a REAL trade with real money. You must specify quantity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {
                        "type": "string",
                        "description": "The trading pair to sell, e.g. 'doge', 'btc'"
                    },
                    "qty": {
                        "type": "number",
                        "description": "Quantity of coins to sell"
                    },
                    "price": {
                        "type": "number",
                        "description": "Optional specific price. If not provided, uses current market price."
                    }
                },
                "required": ["pair", "qty"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_trade_history",
            "description": "Get recent trade log entries from hermes_trades.log. Shows the last N trades executed by the system.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Number of recent trades to retrieve. Default: 10"
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_performance",
            "description": "Analyze the self-learning memory to get performance statistics: win rate, average PnL, best/worst pairs, strategy effectiveness. Use this to learn from past trades and improve future decisions.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_strategy_note",
            "description": "Save a custom trading strategy note or rule that the agent has learned. This will be persisted and used in future trading decisions. Example: 'Avoid DOGE when F&G > 60' or 'BTC performs best when RSI < 25 and orderbook imbalance > 1.5'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rule": {
                        "type": "string",
                        "description": "The strategy rule or observation to save"
                    },
                    "confidence": {
                        "type": "string",
                        "description": "Confidence level: 'hypothesis', 'observed', 'confirmed'",
                        "enum": ["hypothesis", "observed", "confirmed"]
                    }
                },
                "required": ["rule", "confidence"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_spot_overview",
            "description": "Get complete Binance spot wallet holdings including coins held directly (e.g. SOL, DOGE, DOT), their live USDT valuation, fiat valuation, and dust/small assets (< $1). Use this to see the real total assets and allocation.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_screener",
            "description": "Scan top tracked crypto pairs on Binance for breakout setups, oversold dip opportunities (RSI <= 35), and overbought risk warnings (RSI >= 70).",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_tp_sl_plan",
            "description": "Calculate professional volatility-based (ATR) Multi-Target Take Profit (TP1 +1.5R, TP2 +2.5R, TP3 +4R) and Stop Loss plan for any coin or open position.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {
                        "type": "string",
                        "description": "The coin/pair, e.g. 'sol', 'doge', 'btc'"
                    },
                    "entry_price": {
                        "type": "number",
                        "description": "Optional average cost / buy price. If 0, uses live market price."
                    }
                },
                "required": ["pair"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_whale_walls",
            "description": "Inspect Binance orderbook depth for a coin to detect Whale Bid/Ask Walls (massive institutional limit orders), liquidity support/resistance, and buy vs sell volume pressure.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {
                        "type": "string",
                        "description": "The coin/pair, e.g. 'sol', 'btc', 'doge'"
                    }
                },
                "required": ["pair"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "run_quick_backtest",
            "description": "Run an instant historical backtest of Strategy V2 on Binance candles for 3, 7, 14, or 30 days. Returns win rate, total PnL %, number of trades, and average trade return.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {
                        "type": "string",
                        "description": "The coin/pair, e.g. 'sol', 'doge', 'btc'"
                    },
                    "days": {
                        "type": "integer",
                        "description": "Number of days of historical 1h candles to backtest (e.g. 7 or 14). Default: 7"
                    }
                },
                "required": ["pair"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_futures_overview",
            "description": "Get complete Binance USDT-M Futures account overview, available margin balance, and all open LONG/SHORT positions with live PnL.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_futures_trade",
            "description": "Open a LONG or SHORT position on Binance USDT-M Futures with isolated leverage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {"type": "string", "description": "Asset symbol (e.g. BTC, ETH, SOL, DOGE)"},
                    "side": {"type": "string", "enum": ["LONG", "SHORT"], "description": "Trade side"},
                    "usdt_margin": {"type": "number", "description": "Margin amount in USDT to commit"},
                    "leverage": {"type": "integer", "description": "Leverage multiplier (e.g. 2, 3, 5). Default: 3"}
                },
                "required": ["pair", "side", "usdt_margin"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_crypto_news",
            "description": "Fetch real-time crypto news, AI-ranked importance, and sentiment analysis (bullish/bearish/emergency). Can filter by specific coin or get market-wide news.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {
                        "type": "string",
                        "description": "Optional crypto pair or symbol (e.g. 'btc', 'eth', 'sol'). Omit for global market news."
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "close_futures_trade",
            "description": "Close an existing open position on Binance USDT-M Futures.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pair": {"type": "string", "description": "Asset symbol (e.g. BTC, ETH, SOL, DOGE)"},
                    "side": {"type": "string", "enum": ["LONG", "SHORT"], "description": "Side of current open position to close"},
                    "qty": {"type": "number", "description": "Quantity to close"}
                },
                "required": ["pair", "side", "qty"]
            }
        }
    }
]


# ─── Tool Executors ────────────────────────────────────────────────────────────

def _execute_get_price(pair: str) -> dict:
    from hermes.api.rest import fetch_price_rest
    from hermes.state import prices
    # Try WS price first
    cached = prices.get(pair, {})
    price = cached.get("price")
    if not price:
        price = fetch_price_rest(pair)
    if price:
        return {"pair": pair, "price": price, "currency": "USDT"}
    return {"error": "no_price", "pair": pair}


def _execute_get_signal(pair: str) -> dict:
    from hermes.api.rest import fetch_price_rest
    from hermes.indicators.rsi import get_multi_rsi
    from hermes.indicators.signals import get_signal
    from hermes.state import prices

    cached = prices.get(pair, {})
    price = cached.get("price")
    if not price:
        price = fetch_price_rest(pair)
    if not price:
        return {"error": "no_price", "pair": pair}

    multi_rsi = get_multi_rsi(pair, price)
    signal, score, reasons = get_signal(pair, price, multi_rsi)
    return {"pair": pair, "price": price, "signal": signal, "score": score, "reasons": reasons}


def _execute_get_signal_v2(pair: str, risk_pct: float = 0.01) -> dict:
    from hermes.indicators.strategy_new import get_signal_v2
    from hermes.api.balance import get_balance
    from hermes.config import MAX_TRADE_USDT
    balance = get_balance(use_cache=True)
    capital = balance.get("usdt", MAX_TRADE_USDT)
    return get_signal_v2(pair, capital=capital, risk_pct=risk_pct)


def _execute_get_balance() -> dict:
    from hermes.api.balance import get_balance
    return get_balance(use_cache=False)


def _execute_get_portfolio() -> dict:
    from hermes.display.dashboard import print_portfolio_dashboard
    from hermes.api.balance import get_balance
    return print_portfolio_dashboard(get_balance)


def _execute_get_fear_greed() -> dict:
    from hermes.indicators.fear_greed import fetch_fear_greed
    fg_val, fg_class = fetch_fear_greed()
    return {"fear_greed_value": fg_val, "classification": fg_class}


def _execute_get_market_regime() -> dict:
    from hermes.indicators.signals import get_market_regime
    regime, desc = get_market_regime()
    return {"regime": regime, "description": desc}


def _execute_rank_pairs() -> dict:
    from hermes.daemon.tasks import rank_all_pairs
    from hermes.api.rest import fetch_all_prices
    from hermes.state import prices
    from concurrent.futures import ThreadPoolExecutor

    # Make sure we have fresh prices
    if len(prices) < 5:
        fetch_all_prices()

    # rank_all_pairs is async; run it in a thread with its own event loop
    # to avoid "event loop already running" error when called from async context
    with ThreadPoolExecutor(max_workers=1) as executor:
        rankings = executor.submit(asyncio.run, rank_all_pairs())
        rankings = rankings.result(timeout=60)

    result = [{"pair": p, "score": s, "signal": sig, "daily_pos": dp}
              for p, s, sig, dp in rankings[:15]]  # Top 15
    return {"rankings": result}


def _execute_check_positions() -> dict:
    from hermes.trading.positions import check_open_positions
    from hermes.api.balance import get_balance
    from hermes.state import state, prices

    balance = get_balance(use_cache=True)
    results = []
    for pair in list(state.positions.keys()):
        current_price = prices.get(pair, {}).get("price")
        if current_price:
            check_open_positions(current_price, balance, specific_pair=pair)
            pos = state.positions.get(pair)
            if pos:
                pnl = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
                results.append({
                    "pair": pair, "entry_price": pos["entry_price"],
                    "current_price": current_price, "pnl_pct": round(pnl, 2),
                    "qty": pos["qty"], "status": "open"
                })
            else:
                results.append({"pair": pair, "status": "closed_by_tp_sl"})

    return {"positions_checked": len(results), "results": results}


def _execute_get_state() -> dict:
    from hermes.state import state
    return {
        "positions": state.positions,
        "active_pairs": state.active_pairs,
        "fg_value": state.fg_value,
        "fg_class": state.fg_class,
        "num_positions": len(state.positions)
    }


def _execute_buy(pair: str, price: float = None) -> dict:
    from hermes.api.rest import fetch_price_rest
    from hermes.api.balance import get_balance
    from hermes.trading.execution import execute_buy
    from hermes.state import prices as price_cache
    from hermes.config import MIN_TRADE_USDT

    balance = get_balance(use_cache=False)
    usdt = balance.get("usdt", 0)
    if usdt < MIN_TRADE_USDT:
        return {"error": "insufficient_balance", "usdt_balance": usdt}

    if not price:
        cached = price_cache.get(pair, {})
        price = cached.get("price") or fetch_price_rest(pair)
    if not price:
        return {"error": "no_price", "pair": pair}

    # Guard: reject obviously wrong prices (e.g., AI hallucinating price)
    if price < 0.00000001:
        return {"error": "invalid_price", "price_provided": price, "hint": "Price too small. Use /price to get current market price."}

    success, error_msg = execute_buy(pair, price, usdt)
    if success:
        return {"success": True, "pair": pair, "price": price, "idr_spent": usdt}
    return {"success": False, "pair": pair, "price": price, "error": error_msg}


def _execute_sell(pair: str, qty: float, price: float = None) -> dict:
    from hermes.api.rest import fetch_price_rest
    from hermes.trading.execution import execute_sell
    from hermes.state import prices as price_cache
    from hermes.config import MIN_TRADE_USDT

    if not price:
        cached = price_cache.get(pair, {})
        price = cached.get("price") or fetch_price_rest(pair)
    if not price:
        return {"error": "no_price", "pair": pair}

    idr_value = qty * price
    if idr_value < MIN_TRADE_USDT:
        return {"error": "below_minimum_trade", "idr_value": idr_value}

    success, error_msg = execute_sell(pair, price, qty, reason="agent_chat")
    if success:
        return {"success": True, "pair": pair, "price": price, "qty": qty}
    return {"success": False, "pair": pair, "price": price, "error": error_msg}


def _execute_get_trade_history(count: int = 10) -> dict:
    from hermes.config import SCRIPT_DIR
    trade_log = SCRIPT_DIR / "hermes_trades.log"
    if not trade_log.exists():
        return {"trades": [], "message": "No trade log found"}

    lines = trade_log.read_text().strip().split("\n")
    recent = lines[-count:] if len(lines) >= count else lines
    return {"trades": recent, "total_trades": len(lines)}


def _execute_analyze_performance() -> dict:
    from hermes.agent.memory import agent_memory
    return agent_memory.get_performance_summary()


def _execute_save_strategy_note(rule: str, confidence: str = "hypothesis") -> dict:
    from hermes.agent.memory import agent_memory
    agent_memory.save_custom_strategy(rule, confidence)
    return {"saved": True, "rule": rule, "confidence": confidence}


def _execute_get_spot_overview() -> dict:
    from hermes.api.balance import get_spot_account_overview
    return get_spot_account_overview()


def _execute_get_multi_tf_analysis(pair: str) -> dict:
    from hermes.indicators.strategy_new import get_multi_tf_analysis
    return get_multi_tf_analysis(pair)


def _execute_get_market_screener() -> dict:
    from hermes.indicators.strategy_new import get_market_screener
    return get_market_screener()


def _execute_get_tp_sl_plan(pair: str, entry_price: float = 0.0) -> dict:
    from hermes.indicators.strategy_new import calculate_tp_sl_plan
    return calculate_tp_sl_plan(pair, entry_price=entry_price)


def _execute_get_whale_walls(pair: str) -> dict:
    from hermes.indicators.strategy_new import get_orderbook_whale_walls
    return get_orderbook_whale_walls(pair)


def _execute_run_quick_backtest(pair: str, days: int = 7) -> dict:
    from hermes.indicators.strategy_new import run_quick_backtest
    return run_quick_backtest(pair, days=days)


def _execute_get_futures_overview() -> dict:
    from hermes.trading.futures import get_futures_account_overview
    return get_futures_account_overview()


def _execute_futures_trade(pair: str, side: str, usdt_margin: float, leverage: int = 3) -> dict:
    from hermes.trading.futures import execute_futures_order
    success, res = execute_futures_order(pair=pair, side=side, usdt_margin=usdt_margin, leverage=leverage)
    return {"success": success, "result": res}


def _execute_close_futures_trade(pair: str, side: str, qty: float) -> dict:
    from hermes.trading.futures import close_futures_position
    success, res = close_futures_position(pair=pair, side=side, qty=qty)
    return {"success": success, "result": res}


def _execute_get_crypto_news(pair: Optional[str] = None) -> dict:
    from hermes.indicators.news_sentiment import get_news_sentiment
    return get_news_sentiment(pair)


# ─── Dispatcher ────────────────────────────────────────────────────────────────

# Tools that require confirmation before execution
CONFIRM_REQUIRED = {"execute_buy", "execute_sell"}

def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a tool by name and return JSON result string."""
    try:
        if tool_name == "get_price":
            result = _execute_get_price(**tool_input)
        elif tool_name == "get_signal":
            result = _execute_get_signal(**tool_input)
        elif tool_name == "get_signal_v2":
            result = _execute_get_signal_v2(**tool_input)
        elif tool_name == "get_balance":
            result = _execute_get_balance()
        elif tool_name == "get_portfolio":
            result = _execute_get_portfolio()
        elif tool_name == "get_fear_greed":
            result = _execute_get_fear_greed()
        elif tool_name == "get_market_regime":
            result = _execute_get_market_regime()
        elif tool_name == "rank_pairs":
            result = _execute_rank_pairs()
        elif tool_name == "check_positions":
            result = _execute_check_positions()
        elif tool_name == "get_state":
            result = _execute_get_state()
        elif tool_name == "execute_buy":
            result = _execute_buy(**tool_input)
        elif tool_name == "execute_sell":
            result = _execute_sell(**tool_input)
        elif tool_name == "get_trade_history":
            result = _execute_get_trade_history(**tool_input)
        elif tool_name == "analyze_performance":
            result = _execute_analyze_performance()
        elif tool_name == "save_strategy_note":
            result = _execute_save_strategy_note(**tool_input)
        elif tool_name == "get_spot_overview":
            result = _execute_get_spot_overview()
        elif tool_name == "get_multi_tf_analysis":
            result = _execute_get_multi_tf_analysis(**tool_input)
        elif tool_name == "get_market_screener":
            result = _execute_get_market_screener()
        elif tool_name == "get_tp_sl_plan":
            result = _execute_get_tp_sl_plan(**tool_input)
        elif tool_name == "get_whale_walls":
            result = _execute_get_whale_walls(**tool_input)
        elif tool_name == "run_quick_backtest":
            result = _execute_run_quick_backtest(**tool_input)
        elif tool_name == "get_futures_overview":
            result = _execute_get_futures_overview()
        elif tool_name == "execute_futures_trade":
            result = _execute_futures_trade(**tool_input)
        elif tool_name == "close_futures_trade":
            result = _execute_close_futures_trade(**tool_input)
        elif tool_name == "get_crypto_news":
            result = _execute_get_crypto_news(**tool_input)
        else:
            result = {"error": f"Unknown tool: {tool_name}"}

        return json.dumps(result, default=str, ensure_ascii=False)

    except Exception as e:
        log.error(f"[AGENT-TOOL] Error executing {tool_name}: {e}")
        return json.dumps({"error": str(e)})
