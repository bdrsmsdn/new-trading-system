#!/usr/bin/env python3
"""
Hermes Autonomous Crypto Trader v3
====================================
Consolidated trading script using Fear & Greed + RSI mean reversion strategy.
Exchange: Indodax (Indonesia)

Entry Rules:
  - BUY:      F&G ≤ 30 AND RSI ≤ 35 AND daily position < 30%
  - STRONG:   F&G ≤ 20 AND RSI(3m) ≤ 30 AND RSI(1h) ≤ 40

Exit Rules:
  - Take Profit: +10% from entry
  - Stop Loss:   -5% from entry
  - SELL:        F&G ≥ 70 AND RSI ≥ 65 AND daily position > 70%

Position Sizing:
  - Max trade: Rp 10,000 per execution
  - Min trade: Rp 10,000 (balance must exceed this to trade)
  - Fee buffer: 3% deducted from order amount
  - Cooldown: 1 trade per pair per 60 seconds

Usage:
  python3 hermes_trader.py --daemon     # Run continuous daemon
  python3 hermes_trader.py --one-shot   # Run once and exit
  python3 hermes_trader.py --analyze    # Analyze without trading
"""

import argparse
import asyncio
import json
import logging
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import websockets

# ============================================================================
# CONFIGURATION
# ============================================================================

SCRIPT_DIR = Path(__file__).parent
STATE_FILE = SCRIPT_DIR / "hermes_trader_state.json"
PRICE_CACHE = SCRIPT_DIR / "hermes_prices.json"
TRADE_LOG = SCRIPT_DIR / "hermes_trades.log"
PRICE_HISTORY_DIR = SCRIPT_DIR / "price_history"
ENV_FILE = SCRIPT_DIR / ".env"

# Load environment variables from .env file
def load_env() -> dict:
    """Load environment variables from .env file (manual parsing, no external libs)."""
    env_vars = {}
    if ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    env_vars[key.strip()] = value.strip()
        except Exception as e:
            print(f"Warning: Failed to load .env file: {e}")
    return env_vars

# Get env vars before using them
_env = load_env()

# API Credentials (loaded from .env)
API_KEY = _env.get("API_KEY", "")
API_SECRET = _env.get("API_SECRET", "")
NONCE_FILE = Path(os.path.expanduser("~/.hermes/trading/.nonce"))

# Trading Parameters (from SPEC.md)
MAX_TRADE_RP = 10_000       # Max Rp per trade
MIN_TRADE_RP = 10_000       # Min Rp per trade
STOP_LOSS_PCT = 0.05        # 5% stop loss
TAKE_PROFIT_PCT = 0.10     # 10% take profit
TRAILING_STOP_PCT = 0.03   # 3% trailing stop (from peak)
TRAILING_ACTIVATION_PCT = 0.05  # 5% profit required to activate trailing
FEE_BUFFER = 0.03          # 3% fee buffer
TRADE_COOLDOWN = 60        # Seconds between trades per pair

# Strategy Thresholds (from SPEC.md)
FG_BUY_THRESHOLD = 30
FG_STRONG_BUY = 20
FG_SELL_THRESHOLD = 70
RSI_BUY_THRESHOLD = 35
RSI_STRONG_BUY = 30
RSI_SELL_THRESHOLD = 65
DAILY_POS_BUY = 30         # Buy if price is in bottom 30% of daily range
DAILY_POS_SELL = 70        # Sell if price is in top 70% of daily range

# ============================================================================
# PAIR CONFIGURATION — Unified, dynamic, auto-adjusting
# ============================================================================
#
# Architecture:
#   TRACKED_PAIRS     — All pairs we monitor (WS + REST). Universe of observation.
#   ACTIVE_PAIRS      — Pairs we're actively trading. Subset of TRACKED_PAIRS.
#                       Auto-adjusted by daemon every N minutes based on signal score.
#   WS_PAIRS          — Pairs with real-time WebSocket feeds (high priority).
#   REST_PAIRS        — Pairs polled via REST API (fallback/lower freq).
#
# Auto-adjust logic:
#   - Daemon runs pair scoring every ANALYSIS_REASSESS_INTERVAL (default 5 min)
#   - Top N pairs by signal score get promoted to ACTIVE_PAIRS
#   - Pairs already in positions stay ACTIVE until closed
#   - Max ACTIVE_PAIRS = MAX_ACTIVE_PAIRS (prevents over-trading)
#

# Core pairs with WebSocket support (Indodax WS v3 — up to ~20 pairs)
# All tracked pairs — subscribed via WebSocket (real-time, no polling)
# Indodax WS supports unlimited subscriptions; no REST polling = no rate limit
ALL_TRACKED = [
    "doge", "xrp", "ton", "sol", "btc", "eth", "bnb",    # Core
    "pepe", "neirocto", "floki",                          # Meme
    "shib", "ada", "matic", "link", "avax",               # Mid-cap alts
    "dot", "bonk", "dogewif", "labu", "orto",             # Lower-cap alts
    "near", "algo", "trx", "sand", "mana",                # Lower-cap alts
    "axs", "enj", "ftm", "atom", "uni",                   # Lower-cap alts
]

WS_PAIRS = ALL_TRACKED   # ALL pairs go through WS — no REST polling
REST_PAIRS = []          # DEPRECATED — all prices come via WS now

# Active trading pairs — dynamically managed by daemon
# Starts with WS_PAIRS subset, auto-adjusts based on signals
MAX_ACTIVE_PAIRS = 8                 # Max pairs to trade simultaneously
MIN_ACTIVE_PAIRS = 3                  # Always keep at least this many
_initial_active = ["doge", "xrp", "ton", "sol", "btc", "eth"]

# Daemon analysis interval for pair reassessment
ANALYSIS_REASSESS_INTERVAL = 300     # Re-score and adjust pairs every 5 min

# Pair priority tiers (WS = better data, faster execution)
WS_PRIORITY_PAIRS = {"doge", "xrp", "ton", "sol", "btc", "eth", "bnb"}  # Never demoted to REST
PAIR_DECIMAL_PLACES = {              # For rounding amounts in orders
    "doge": 0, "xrp": 2, "ton": 2, "btc": 6, "eth": 5, "bnb": 4,
    "sol": 4, "shib": 0, "ada": 2, "matic": 2, "link": 4,
    "avax": 4, "dot": 3, "near": 4, "algo": 3, "trx": 2,
    "pepe": 0, "neirocto": 0, "floki": 0, "shib": 0,
}

# Daemon settings
DAEMON_TRADE_CHECK_INTERVAL = 60   # Check for trades every 60s (Indodax rate limit ~1 req/min)
DAEMON_FG_FETCH_INTERVAL = 300      # Fetch F&G every 5 minutes

# PID file to prevent duplicate daemons
PID_FILE = SCRIPT_DIR / "daemon.pid"

# ============================================================================
# LOGGING
# ============================================================================

def setup_logging():
    """Configure logging to file and console."""
    TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(TRADE_LOG),
            logging.StreamHandler(sys.stdout)
        ]
    )
    return logging.getLogger("hermes_trader")

log = setup_logging()

# ============================================================================
# TELEGRAM NOTIFICATIONS
# ============================================================================

# Telegram config from env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
TELEGRAM_BOT_TOKEN = _env.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env.get("TELEGRAM_CHAT_ID", "")
_telegram_enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

def telegram_send(message: str) -> bool:
    """Send a message via Telegram bot. Returns True on success, False on failure."""
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

# ============================================================================
# STATE MANAGEMENT
# ============================================================================

class State:
    """Manages persistent state for the trader."""
    
    def __init__(self):
        self.positions: Dict[str, Dict] = {}      # {pair: {entry_price, qty, time, stop_loss, take_profit}}
        self.last_trade_time: Dict[str, float] = {}  # {pair: timestamp}
        self.price_history: Dict[str, List[float]] = {}  # {pair: [prices]}
        self.rsi_state: Dict[str, Dict] = {}      # {pair: {avg_gain, avg_loss, last_price, initialized}}
        self.active_pairs: List[str] = []         # Dynamically managed list of pairs to trade
        self.fg_value = 50
        self.fg_class = "Neutral"
        self.balance_cache: Optional[Dict] = None
        self.balance_cache_time = 0
        self.load()
    
    def load(self):
        """Load state from disk."""
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self.positions = data.get("positions", {})
                self.last_trade_time = data.get("last_trade_time", {})
                self.active_pairs = data.get("active_pairs", _initial_active.copy())
                # Don't load price_history to avoid memory bloat
            except Exception as e:
                log.warning(f"Failed to load state: {e}")
                self.active_pairs = _initial_active.copy()
        
        # Initialize RSI state for all pairs
        for pair in ALL_TRACKED:
            if pair not in self.rsi_state:
                self.rsi_state[pair] = {
                    "avg_gain": 0, "avg_loss": 0, 
                    "last_price": 0, "initialized": False
                }
        
        # Ensure active_pairs always has minimum
        if not self.active_pairs:
            self.active_pairs = _initial_active.copy()
    
    def save(self):
        """Save state to disk."""
        try:
            STATE_FILE.write_text(json.dumps({
                "positions": self.positions,
                "last_trade_time": self.last_trade_time,
                "active_pairs": self.active_pairs,
            }, indent=2))
        except Exception as e:
            log.error(f"Failed to save state: {e}")

state = State()

# ============================================================================
# API COMMUNICATION
# ============================================================================

def get_nonce() -> int:
    """Get and increment nonce for API calls.
    
    Uses millisecond timestamp to ensure uniqueness and stay in sync
    with Indodax server-side nonce tracking.
    """
    try:
        # Read current nonce
        if NONCE_FILE.exists():
            with open(NONCE_FILE, "r") as f:
                current = int(f.read().strip())
        else:
            current = 0
        
        # Use millisecond timestamp — must be greater than any previous nonce
        ms = int(time.time() * 1000)
        new_nonce = max(ms, current + 1)
        
        with open(NONCE_FILE, "w") as f:
            f.write(str(new_nonce))
        return new_nonce
    except Exception as e:
        log.error(f"Nonce error: {e}")
        return int(time.time() * 1000)

def sign_request(params_str: str) -> str:
    """Generate HMAC-SHA512 signature."""
    import hmac
    import hashlib
    return hmac.new(
        API_SECRET.encode(),
        params_str.encode(),
        hashlib.sha512
    ).hexdigest()

def api_call(method: str, **params) -> dict:
    """Make authenticated API call to Indodax with retry on rate limit."""
    nonce = get_nonce()
    extra = "&".join(f"{k}={v}" for k, v in params.items())
    if extra:
        params_str = f"method={method}&nonce={nonce}&{extra}"
    else:
        params_str = f"method={method}&nonce={nonce}"
    signature = sign_request(params_str)
    
    max_retries = 3
    base_delay = 2

    for attempt in range(max_retries):
        try:
            result = subprocess.run([
                "curl", "-s", "-X", "POST", "https://indodax.com/tapi",
                "-H", f"Key: {API_KEY}",
                "-H", f"Sign: {signature}",
                "-d", params_str,
                "-H", "User-Agent: Mozilla/5.0",
                "-w", "\n%{http_code}",   # append HTTP status on last line
            ], capture_output=True, text=True, timeout=15)

            # Split HTTP status code from body
            parts = result.stdout.rsplit("\n", 1)
            body = parts[0] if len(parts) == 2 else result.stdout
            http_status = parts[1].strip() if len(parts) == 2 else "200"

            # HTTP 429 — set global public-REST cooldown and back off
            if http_status == "429":
                global _global_rate_limit_until
                _global_rate_limit_until = time.time() + 60
                delay = base_delay * (2 ** attempt)
                log.warning(f"[API] HTTP 429 from Indodax TAPI — global cooldown 60s, retry in {delay}s")
                time.sleep(delay)
                nonce = get_nonce()
                params_str = f"method={method}&nonce={nonce}&{extra}" if extra else f"method={method}&nonce={nonce}"
                signature = sign_request(params_str)
                continue

            data = json.loads(body)

            # JSON-level rate limit signal
            error_msg = str(data.get("error", "")).lower()
            if data.get("success") == 0 and ("too_many_requests" in error_msg or "rate" in error_msg):
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    log.warning(f"Rate limited by Indodax, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    nonce = get_nonce()
                    params_str = f"method={method}&nonce={nonce}&{extra}" if extra else f"method={method}&nonce={nonce}"
                    signature = sign_request(params_str)
                    continue
                else:
                    log.error("Rate limit exceeded after all retries")
                    return data

            return data
        except json.JSONDecodeError as e:
            # Non-JSON response (e.g. HTML error page) — treat as transient error
            log.warning(f"[API] Non-JSON response on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                nonce = get_nonce()
                params_str = f"method={method}&nonce={nonce}&{extra}" if extra else f"method={method}&nonce={nonce}"
                signature = sign_request(params_str)
                continue
            return {"success": 0, "error": "non-json response"}
        except Exception as e:
            log.error(f"API call failed: {e}")
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                continue
            return {"success": 0, "error": str(e)}

    return {"success": 0, "error": "max retries exceeded"}

def fetch_fear_greed() -> Tuple[int, str]:
    """Fetch Fear & Greed index from alternative.me."""
    try:
        result = subprocess.run([
            "curl", "-s", "https://api.alternative.me/fng/?limit=1"
        ], capture_output=True, text=True, timeout=10)
        
        data = json.loads(result.stdout)
        fg_value = int(data["data"][0]["value"])
        fg_class = data["data"][0]["value_classification"]
        state.fg_value = fg_value
        state.fg_class = fg_class
        return fg_value, fg_class
    except Exception as e:
        log.warning(f"F&G fetch failed: {e}")
        return state.fg_value, state.fg_class

def fetch_polymarket_vibes() -> List[Dict]:
    """Fetch top 3 crypto-related prediction markets from Polymarket.
    
    Polymarket API returns markets under "data" key. Each market has a "tokens"
    array with outcome tokens containing "outcome" (YES/NO) and "price" fields.
    """
    try:
        resp = requests.get(
            "https://clob.polymarket.com/markets",
            params={"closed": "false", "limit": 200},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        if resp.status_code != 200:
            return []
        
        markets = resp.json().get("data", [])
        
        # Filter for crypto-related and extract question + yes/no prices
        vibes = []
        crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana",
                          "sol", "dogecoin", "doge", "xrp", "bnb", "cardano", "ada",
                          "ton ", "toncoin", "pepe", "floki", "solana"]
        
        for m in markets:
            question = m.get("question", "").lower()
            if not any(kw in question for kw in crypto_keywords):
                continue
            
            # Skip closed/inactive markets (API returns "true"/"false" as strings)
            closed = m.get("closed", "")
            if str(closed).lower() == "true":
                continue
            
            # Get YES/NO prices from tokens array
            tokens = m.get("tokens", [])
            yes_price = None
            no_price = None
            
            for token in tokens:
                outcome = token.get("outcome", "").upper()
                price = token.get("price")
                if price is not None:
                    if outcome == "YES":
                        yes_price = float(price)
                    elif outcome == "NO":
                        no_price = float(price)
            
            if yes_price is None or no_price is None:
                continue
            
            # Skip if prices are 0 or 1 (fully resolved markets)
            if yes_price == 0 or no_price == 0:
                continue
            
            vibes.append({
                "question": m.get("question", "Unknown"),
                "yes_price": yes_price,
                "no_price": no_price,
                "volume": m.get("volume", 0),
            })
            
            if len(vibes) >= 3:
                break
        
        return vibes
    except Exception:
        return []

def fetch_price_rest(pair: str) -> Optional[float]:
    """Fetch price via public REST API (throttled, no auth required)."""
    # Prefer WS price if fresh (< 10s)
    cached = prices.get(pair, {})
    age = time.time() - cached.get("ts", cached.get("updated", 0))
    if age < 10 and cached.get("price"):
        return cached["price"]

    body = _throttled_public_get(f"https://indodax.com/api/ticker/{pair}_idr")
    if body is None:
        return None
    try:
        data = json.loads(body)
        return float(data["ticker"]["last"])
    except Exception as e:
        log.debug(f"REST price fetch failed for {pair}: {e}")
        return None

def fetch_ticker_full(pair: str) -> Optional[dict]:
    """Fetch full ticker data including high/low/volume (throttled)."""
    body = _throttled_public_get(f"https://indodax.com/api/ticker/{pair}_idr")
    if body is None:
        return None
    try:
        data = json.loads(body)
        t = data["ticker"]
        return {
            "last": float(t["last"]),
            "high": float(t["high"]),
            "low": float(t["low"]),
            "buy": float(t["buy"]),
            "sell": float(t["sell"]),
            "vol": float(t.get(f"vol_{pair}", 0))
        }
    except Exception as e:
        log.debug(f"Ticker fetch failed for {pair}: {e}")
        return None

# ============================================================================
# PUBLIC REST THROTTLE + CANDLE/RSI CACHE
# ============================================================================

# TTL per candle interval (seconds)
_CANDLE_TTL: Dict[str, int] = {
    "1d": 3600, "4h": 1800, "1h": 600, "30m": 300,
    "15m": 300, "5m": 60, "3m": 60, "1m": 30,
}
_candle_cache: Dict[str, Dict] = {}   # {f"{pair}_{interval}": {"candles": list, "ts": float}}

# Multi-RSI result cache (avoid re-fetching 4 candle TFs per pair within same window)
_MULTI_RSI_TTL = 90   # seconds
_multi_rsi_cache: Dict[str, Dict] = {}  # {pair: {"rsi": dict, "ts": float}}

# Global throttle — keeps public REST under ~150 req/min
_PUBLIC_REST_MIN_INTERVAL = 0.4   # seconds between consecutive public calls
_last_public_rest_call: float = 0.0
_global_rate_limit_until: float = 0.0   # if set, block all public REST until then


def _throttled_public_get(url: str, timeout: int = 10) -> Optional[str]:
    """
    Make a throttled public GET via curl.
    - Enforces min 0.4s interval between calls (~150 req/min ceiling).
    - Detects HTTP 429 via curl's %{http_code} and sets a 60s global cooldown.
    - Returns raw response body, or None on 429/error.
    """
    global _last_public_rest_call, _global_rate_limit_until

    # Honour global rate-limit cooldown
    now = time.time()
    if now < _global_rate_limit_until:
        wait = _global_rate_limit_until - now
        log.warning(f"[THROTTLE] Global cooldown active — waiting {wait:.1f}s")
        time.sleep(wait)

    # Enforce per-call minimum interval
    elapsed = time.time() - _last_public_rest_call
    if elapsed < _PUBLIC_REST_MIN_INTERVAL:
        time.sleep(_PUBLIC_REST_MIN_INTERVAL - elapsed)

    _last_public_rest_call = time.time()

    try:
        result = subprocess.run(
            ["curl", "-s", "-A", "Mozilla/5.0", "-w", "\n%{http_code}", url],
            capture_output=True, text=True, timeout=timeout
        )
        # curl -w appends "\nSTATUS_CODE" after the body
        parts = result.stdout.rsplit("\n", 1)
        body = parts[0] if len(parts) == 2 else result.stdout
        status_code = parts[1].strip() if len(parts) == 2 else "200"

        if status_code == "429":
            _global_rate_limit_until = time.time() + 60
            log.warning("[THROTTLE] HTTP 429 received — global cooldown 60s")
            return None

        return body
    except Exception as e:
        log.debug(f"[THROTTLE] Request failed for {url}: {e}")
        return None


def fetch_candles(pair: str, interval: str = "1h", limit: int = 100) -> Optional[List[List[float]]]:
    """
    Fetch OHLCV candles from Indodax public API with TTL cache.
    interval: 1m, 3m, 5m, 15m, 30m, 1h, 4h, 6h, 12h, 1d, 1w
    Returns list of [timestamp, open, high, low, close, volume] lists.
    """
    cache_key = f"{pair}_{interval}"
    ttl = _CANDLE_TTL.get(interval, 300)
    cached = _candle_cache.get(cache_key)
    if cached and (time.time() - cached["ts"]) < ttl:
        return cached["candles"]

    body = _throttled_public_get(
        f"https://indodax.com/api/klines/{pair}idr?interval={interval}&limit={limit}"
    )
    if body is None:
        return cached["candles"] if cached else None

    try:
        data = json.loads(body)
        if data.get("success") == 1:
            candles = data.get("klines", [])
            _candle_cache[cache_key] = {"candles": candles, "ts": time.time()}
            return candles
        return cached["candles"] if cached else None
    except Exception as e:
        log.debug(f"Candles fetch failed for {pair} ({interval}): {e}")
        return cached["candles"] if cached else None

def calc_rsi_from_candles(candles: List[List[float]], period: int = 14) -> Optional[float]:
    """Calculate RSI from candle close prices."""
    if len(candles) < period + 1:
        return None
    
    closes = [float(c[4]) for c in candles]  # close is index 4
    
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i-1]
        if delta > 0:
            gains.append(delta)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(delta))
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_multi_rsi(pair: str, price: float) -> Dict[str, float]:
    """
    Get RSI across multiple timeframes.
    3m from live prices (always fresh), higher TFs from candle cache (90s TTL).
    """
    # Always update 3m RSI from live price
    rsi_3m = update_rsi(pair, price, period=3)

    # Return cached result if fresh (TTL 90s), updating only the live 3m RSI
    cached = _multi_rsi_cache.get(pair)
    if cached and (time.time() - cached["ts"]) < _MULTI_RSI_TTL:
        result = dict(cached["rsi"])
        result["3m"] = rsi_3m
        return result

    result = {
        "3m": rsi_3m,
        "15m": 50.0,
        "1h": 50.0,
        "4h": 50.0,
        "1d": 50.0,
    }

    # Higher timeframes from candles API (served from _candle_cache when warm)
    timeframe_map = {
        "15m": ("15m", 14),
        "1h": ("1h", 14),
        "4h": ("4h", 14),
        "1d": ("1d", 14),
    }

    for key, (interval, period) in timeframe_map.items():
        candles = fetch_candles(pair, interval=interval, limit=100)
        if candles:
            rsi = calc_rsi_from_candles(candles, period=period)
            if rsi is not None:
                result[key] = rsi

    _multi_rsi_cache[pair] = {"rsi": dict(result), "ts": time.time()}
    return result

def get_balance(use_cache: bool = True) -> Dict[str, float]:
    """Get account balance (with caching to avoid nonce exhaustion).
    
    Uses a 120-second cache TTL to avoid hitting Indodax rate limits.
    The cache is shared across all calls when use_cache=True.
    """
    if use_cache and state.balance_cache:
        cache_age = time.time() - state.balance_cache_time
        if cache_age < 120:  # Cache valid for 120 seconds to avoid rate limits
            return state.balance_cache
    
    result = api_call("getInfo")
    if result.get("success") == 1:
        balances = result["return"].get("balance", {})
        # Dynamically extract ALL balances — no hardcoded list
        state.balance_cache = {k: float(v) for k, v in balances.items() if float(v or 0) > 0}
        state.balance_cache_time = time.time()
        return state.balance_cache
    else:
        log.error(f"Balance fetch failed: {result.get('error', result)}")
        # Return stale cache if available rather than nothing
        if state.balance_cache:
            log.warning("Returning stale balance cache due to API error")
            return state.balance_cache
        return {}

# ============================================================================
# INDICATORS
# ============================================================================

def calculate_volatility(pair: str, current_price: float, period: int = 14) -> float:
    """
    Calculate volatility using standard deviation of recent prices.
    Returns volatility factor (1.0 = normal, higher = more volatile).
    Used to scale down position size in volatile markets.
    """
    history = state.price_history.get(pair, [])
    if len(history) < period:
        return 1.0  # Not enough data, assume normal volatility
    
    # Use recent prices for stddev calculation
    recent = history[-period:]
    mean = sum(recent) / len(recent)
    
    # Population standard deviation
    variance = sum((p - mean) ** 2 for p in recent) / len(recent)
    stddev = math.sqrt(variance)
    
    # Coefficient of variation (relative volatility)
    if mean > 0:
        cv = stddev / mean
    else:
        cv = 0
    
    # Scale factor: higher CV = smaller position
    # CV of 0.02 (2%) = normal (factor 1.0)
    # CV of 0.05 (5%) = high volatility (factor ~0.5)
    # Cap between 0.3 and 1.0
    volatility_factor = max(0.3, min(1.0, 0.02 / cv if cv > 0 else 1.0))
    
    return volatility_factor


def calculate_atr(pair: str, current_price: float, period: int = 14) -> float:
    """
    Calculate Average True Range (ATR) as a volatility measure.
    Returns ATR value in price units.
    """
    history = state.price_history.get(pair, [])
    if len(history) < period + 1:
        return current_price * 0.02  # Default 2% ATR if not enough data
    
    # True Range = max of:
    # - High - Low
    # - |High - Previous Close|
    # - |Low - Previous Close|
    tr_values = []
    for i in range(1, min(len(history), period + 1)):
        high_low = history[i] - history[i-1]  # Simplified since we only have closes
        tr = abs(high_low)
        tr_values.append(tr)
    
    if not tr_values:
        return current_price * 0.02
    
    atr = sum(tr_values) / len(tr_values)
    return atr


def get_dynamic_position_size(pair: str, current_price: float, idr_balance: float) -> float:
    """
    Calculate dynamic position size based on volatility.
    High volatility = smaller position.
    Base size: Rp 10,000, scaled by volatility factor.
    """
    base_size = MAX_TRADE_RP  # Rp 10,000
    
    # Get volatility factor using stddev
    vol_factor = calculate_volatility(pair, current_price)
    
    # Also calculate ATR-based adjustment for additional confirmation
    atr = calculate_atr(pair, current_price)
    atr_ratio = atr / current_price if current_price > 0 else 0.02
    
    # Combine factors (weighted average)
    combined_factor = vol_factor * 0.7 + max(0.3, min(1.0, 0.02 / atr_ratio)) * 0.3
    
    # Calculate position size
    dynamic_size = base_size * combined_factor
    
    # Apply fee buffer and balance check
    max_affordable = idr_balance * (1 - FEE_BUFFER)
    final_size = min(dynamic_size, max_affordable)
    
    # Ensure minimum size
    if final_size < MIN_TRADE_RP * 0.5:  # Allow 50% of min for very small positions
        final_size = 0
    
    log.debug(f"Dynamic size for {pair}: Rp {final_size:,.0f} (vol_factor={vol_factor:.2f}, combined={combined_factor:.2f})")
    
    return final_size


def update_rsi(pair: str, current_price: float, period: int = 3) -> float:
    """Update RSI using Wilder's smoothing method."""
    rs_state = state.rsi_state.get(pair, {
        "avg_gain": 0, "avg_loss": 0,
        "last_price": 0, "initialized": False
    })
    
    history = state.price_history.setdefault(pair, [])
    history.append(current_price)
    if len(history) > 100:
        history = history[-100:]
        state.price_history[pair] = history
    
    if not rs_state["initialized"]:
        if rs_state["last_price"] > 0:
            change = current_price - rs_state["last_price"]
            gain = max(change, 0)
            loss = max(-change, 0)
            rs_state["avg_gain"] = gain
            rs_state["avg_loss"] = loss
            rs_state["initialized"] = len(history) >= period
        rs_state["last_price"] = current_price
        state.rsi_state[pair] = rs_state
        return 50.0
    
    change = current_price - rs_state["last_price"]
    gain = max(change, 0)
    loss = max(-change, 0)
    
    n = period
    rs_state["avg_gain"] = (rs_state["avg_gain"] * (n - 1) + gain) / n
    rs_state["avg_loss"] = (rs_state["avg_loss"] * (n - 1) + loss) / n
    rs_state["last_price"] = current_price
    state.rsi_state[pair] = rs_state
    
    if rs_state["avg_loss"] == 0:
        return 100.0
    
    rs = rs_state["avg_gain"] / rs_state["avg_loss"]
    return 100 - (100 / (1 + rs))

def get_rsi(pair: str) -> float:
    """Get current RSI for a pair."""
    rs_state = state.rsi_state.get(pair, {})
    avg_gain = rs_state.get("avg_gain", 0)
    avg_loss = rs_state.get("avg_loss", 0)
    
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_daily_position(pair: str, current_price: float = None) -> float:
    """Calculate where current price sits in daily range (0-100%). Uses cached ticker if fresh."""
    # Try cache first (<5 min)
    ticker_data = _ticker_cache.get(pair, {})
    ticker_age = time.time() - ticker_data.get("ts", 0) if ticker_data else 999
    if ticker_age < 300 and current_price is not None:
        high = ticker_data.get("high", 0)
        low = ticker_data.get("low", 0)
        if high > low:
            return ((current_price - low) / (high - low)) * 100
        return 50.0

    ticker = fetch_ticker_full(pair)
    if not ticker:
        return 50.0

    if pair not in _ticker_cache:
        _ticker_cache[pair] = {}
    _ticker_cache[pair] = {"high": ticker["high"], "low": ticker["low"], "ts": time.time()}

    high = ticker["high"]
    low = ticker["low"]
    if high == low:
        return 50.0

    return ((current_price - low) / (high - low)) * 100

def get_market_regime() -> Tuple[str, str]:
    """
    Detect current market regime: BULL, BEAR, or SIDEWAYS.
    Also returns market sentiment description.
    
    Logic:
    - F&G >= 60 → BULL
    - F&G <= 30 → BEAR
    - F&G 31-59 → SIDEWAYS (unless BTC strongly trending)
    """
    fg = state.fg_value
    
    if fg >= 60:
        return "BULL", f"Fear & Greed at {fg} — Greed market"
    elif fg <= 30:
        return "BEAR", f"Fear & Greed at {fg} — Fear market"
    else:
        return "SIDEWAYS", f"Fear & Greed at {fg} — Neutral market"

def get_regime_trading_config() -> dict:
    """
    Return trading config adjustments based on market regime.
    Allows more aggressive position sizing in BULL, defensive in BEAR.
    """
    regime, desc = get_market_regime()
    
    if regime == "BULL":
        return {
            "max_active_multiplier": 1.5,   # Can have more pairs active
            "position_size_mult": 1.2,       # Larger positions
            "tp_adjust": 1.0,                # Normal TP
            "sl_adjust": 1.2,                # Wider SL (more patient)
            "allow_sell": True,              # Sell on strength
        }
    elif regime == "BEAR":
        return {
            "max_active_multiplier": 0.5,   # Fewer pairs
            "position_size_mult": 0.5,       # Smaller positions
            "tp_adjust": 1.0,                # Quick TP
            "sl_adjust": 0.8,                # Tighter SL (cut losses fast)
            "allow_sell": False,             # Don't sell into fear
        }
    else:  # SIDEWAYS
        return {
            "max_active_multiplier": 1.0,
            "position_size_mult": 0.8,       # Slightly smaller
            "tp_adjust": 1.0,
            "sl_adjust": 1.0,
            "allow_sell": True,
        }

def calc_classic_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """Calculate classic RSI from price list (for analysis mode)."""
    if len(prices) < period + 1:
        return None
    
    gains = []
    losses = []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i-1]
        if delta > 0:
            gains.append(delta)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(delta))
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ============================================================================
# SIGNAL GENERATION
# ============================================================================

def get_signal(pair: str, current_price: float, multi_rsi: Dict[str, float] = None) -> Tuple[str, int, List[str]]:
    """
    Calculate trading signal based on F&G + RSI + daily position.
    
    STRONG BUY requires RSI(3m)<=30 AND RSI(1h)<=40
    
    Returns: (signal_name, score, reasons_list)
    """
    if multi_rsi is None:
        multi_rsi = {"3m": get_rsi(pair), "15m": 50.0, "1h": 50.0, "4h": 50.0, "1d": 50.0}
    
    rsi_3m = multi_rsi.get("3m", 50.0)
    rsi_1h = multi_rsi.get("1h", 50.0)
    rsi = get_rsi(pair)  # Default RSI from state
    fg = state.fg_value
    daily_pos = get_daily_position(pair, current_price)
    
    score = 0
    reasons = []
    
    # Check for STRONG BUY: RSI(3m)<=30 AND RSI(1h)<=40
    if rsi_3m <= 30 and rsi_1h <= 40:
        return "STRONG_BUY", 10, [f"RSI_3M_OVERSOLD ({rsi_3m:.1f})", f"RSI_1H_OVERSOLD ({rsi_1h:.1f})", f"F&G ({fg})"]
    
    # Fear & Greed contribution
    if fg <= FG_STRONG_BUY:
        fg_score = 3
        reasons.append(f"EXTREME_FEAR ({fg})")
    elif fg <= FG_BUY_THRESHOLD:
        fg_score = 2
        reasons.append(f"FEAR ({fg})")
    elif fg <= 50:
        fg_score = 0
        reasons.append(f"NEUTRAL ({fg})")
    elif fg >= FG_SELL_THRESHOLD:
        fg_score = -2
        reasons.append(f"GREED ({fg})")
    else:
        fg_score = -1
        reasons.append(f"{state.fg_class} ({fg})")
    
    score += fg_score
    
    # RSI contribution (using 3m for trading decision)
    if rsi_3m <= RSI_STRONG_BUY:
        rsi_score = 3
        reasons.append(f"RSI_OVERSOLD ({rsi_3m:.1f})")
    elif rsi_3m <= RSI_BUY_THRESHOLD:
        rsi_score = 2
        reasons.append(f"RSI_NEAR_OVERSOLD ({rsi_3m:.1f})")
    elif rsi_3m >= RSI_SELL_THRESHOLD:
        rsi_score = -2
        reasons.append(f"RSI_OVERBOUGHT ({rsi_3m:.1f})")
    elif rsi_3m >= 55:
        rsi_score = -1
        reasons.append(f"RSI_NEAR_OVERBOUGHT ({rsi_3m:.1f})")
    else:
        rsi_score = 0
        reasons.append(f"RSI_NEUTRAL ({rsi_3m:.1f})")
    
    score += rsi_score
    
    # Daily position contribution
    if daily_pos < DAILY_POS_BUY:
        pos_score = 2
        reasons.append(f"LOW_DAILY_POS ({daily_pos:.0f}%)")
    elif daily_pos > DAILY_POS_SELL:
        pos_score = -2
        reasons.append(f"HIGH_DAILY_POS ({daily_pos:.0f}%)")
    else:
        pos_score = 0
    
    score += pos_score
    
    # Determine final signal
    if score >= 5:
        return "STRONG_BUY", score, reasons
    elif score >= 3:
        return "BUY", score, reasons
    elif score >= 1:
        return "WEAK_BUY", score, reasons
    elif score <= -3:
        return "STRONG_SELL", score, reasons
    elif score <= -1:
        return "SELL", score, reasons
    else:
        return "HOLD", score, reasons

# ============================================================================
# TRADING EXECUTION
# ============================================================================

def format_coin(amount: float, symbol: str) -> str:
    """Format coin amount for display."""
    if amount < 0.00001:
        return f"{amount:.8f} {symbol}"
    elif amount < 0.001:
        return f"{amount:.6f} {symbol}"
    elif amount < 1:
        return f"{amount:.4f} {symbol}"
    else:
        return f"{amount:.2f} {symbol}"

def execute_buy(pair: str, price: float, idr_balance: float) -> bool:
    """Execute a buy order with dynamic position sizing based on volatility."""
    # Calculate dynamic position size based on volatility
    buy_amount_rp = get_dynamic_position_size(pair, price, idr_balance)
    if buy_amount_rp < MIN_TRADE_RP * 0.5:
        log.info(f"Skipping {pair}: position size too small after volatility adjustment (Rp {buy_amount_rp:,.0f})")
        return False
    if buy_amount_rp > idr_balance * (1 - FEE_BUFFER):
        buy_amount_rp = idr_balance * (1 - FEE_BUFFER)
    
    coin_amount = buy_amount_rp / price
    
    # Round down based on PAIR_DECIMAL_PLACES config
    decimals = PAIR_DECIMAL_PLACES.get(pair, 4)
    if decimals == 0:
        coin_amount = math.floor(coin_amount)
    else:
        coin_amount = math.floor(coin_amount * (10 ** decimals)) / (10 ** decimals)
    
    if coin_amount <= 0:
        log.info(f"Skipping {pair}: coin amount too small")
        return False
    
    vol_factor = calculate_volatility(pair, price)
    log.info(f"BUY order (dynamic, vol_factor={vol_factor:.2f}): {format_coin(coin_amount, pair)} @ Rp {price:,.0f}")
    
    result = api_call("trade",
        pair=f"{pair}idr",
        type="buy",
        price=int(price),
        amount=str(coin_amount)
    )
    
    if result.get("success") == 1:
        trade_details = result["return"]
        log.info(f"✅ BUY SUCCESS: {format_coin(coin_amount, pair)} @ Rp {price:,.0f}")
        log.info(f"   Total: Rp {float(trade_details.get('total', 0)):,.0f}")
        
        # Record position
        state.positions[pair] = {
            "entry_price": price,
            "qty": coin_amount,
            "time": time.time(),
            "stop_loss": price * (1 - STOP_LOSS_PCT),
            "take_profit": price * (1 + TAKE_PROFIT_PCT),
            "peak_price": price  # Track peak price for trailing stop
        }
        state.last_trade_time[pair] = time.time()
        state.save()
        return True
    else:
        log.error(f"❌ BUY FAILED: {result.get('error', result)}")
        return False

def execute_sell(pair: str, price: float, qty: float, reason: str = "") -> bool:
    """Execute a sell order."""
    log.info(f"SELL order ({reason}): {format_coin(qty, pair)} @ Rp {price:,.0f}")
    
    result = api_call("trade",
        pair=f"{pair}idr",
        type="sell",
        price=int(price),
        amount=str(qty)
    )
    
    if result.get("success") == 1:
        trade_details = result["return"]
        log.info(f"✅ SELL SUCCESS: {format_coin(qty, pair)} @ Rp {price:,.0f}")
        log.info(f"   Total: Rp {float(trade_details.get('total', 0)):,.0f}")
        
        if pair in state.positions:
            del state.positions[pair]
        state.last_trade_time[pair] = time.time()
        state.save()
        return True
    else:
        log.error(f"❌ SELL FAILED: {result.get('error', result)}")
        return False

# ============================================================================
# TRADE MANAGEMENT
# ============================================================================

def check_open_positions(current_price: float, balance: Dict[str, float]) -> None:
    """Check and manage open positions (TP/SL/Trailing Stop)."""
    for pair, pos in list(state.positions.items()):
        entry = pos["entry_price"]
        qty = pos.get("qty", 0)
        stop_loss = pos.get("stop_loss", entry * (1 - STOP_LOSS_PCT))
        take_profit = pos.get("take_profit", entry * (1 + TAKE_PROFIT_PCT))
        peak_price = pos.get("peak_price", entry)
        
        pnl_pct = (current_price - entry) / entry
        
        # Update peak price if current price is higher
        if current_price > peak_price:
            peak_price = current_price
            pos["peak_price"] = peak_price
        
        # Check take profit
        if current_price >= take_profit:
            log.info(f"TP hit for {pair}: +{pnl_pct*100:.1f}%")
            execute_sell(pair, current_price, qty, "Take Profit")
            continue
        
        # Check trailing stop (activates after +5% profit)
        if pnl_pct >= TRAILING_ACTIVATION_PCT:
            trailing_stop_price = peak_price * (1 - TRAILING_STOP_PCT)
            if current_price <= trailing_stop_price:
                log.info(f"Trailing SL hit for {pair}: price dropped to {current_price}, trail price {trailing_stop_price}, peak {peak_price}")
                execute_sell(pair, current_price, qty, "Trailing Stop")
                continue
        
        # Check fixed stop loss (backup)
        if current_price <= stop_loss:
            log.info(f"SL hit for {pair}: {pnl_pct*100:.1f}%")
            execute_sell(pair, current_price, qty, "Stop Loss")
            continue
        
        # Signal exit: only worth checking when we're in profit.
        # Use cached multi_rsi to avoid triggering fresh candle fetches per position.
        if pnl_pct > 0:
            cached_mrsi = _multi_rsi_cache.get(pair, {})
            if cached_mrsi and (time.time() - cached_mrsi.get("ts", 0)) < _MULTI_RSI_TTL:
                multi_rsi = dict(cached_mrsi["rsi"])
                multi_rsi["3m"] = get_rsi(pair)  # keep 3m fresh from WS
            else:
                multi_rsi = get_multi_rsi(pair, current_price)
            signal, score, reasons = get_signal(pair, current_price, multi_rsi)
            if signal in ["STRONG_SELL", "SELL"]:
                log.info(f"Signal exit for {pair}: {signal} with +{pnl_pct*100:.1f}%")
                execute_sell(pair, current_price, qty, f"Signal: {signal}")

def check_for_entries(pair: str, current_price: float, idr_balance: float) -> bool:
    """Check if we should enter a position."""
    # Rate limit check
    last_trade = state.last_trade_time.get(pair, 0)
    if time.time() - last_trade < TRADE_COOLDOWN:
        return False
    
    # Already have position
    if pair in state.positions:
        return False
    
    # Get signal
    multi_rsi = get_multi_rsi(pair, current_price)
    signal, score, reasons = get_signal(pair, current_price, multi_rsi)
    
    log.info(f"{pair.upper()}: {signal} (score={score}) - {', '.join(reasons)}")
    
    if signal in ["STRONG_BUY", "BUY"] and idr_balance >= MIN_TRADE_RP:
        return execute_buy(pair, current_price, idr_balance)
    
    return False

# ============================================================================
# PRICE UPDATES
# ============================================================================

prices = {}  # Live price cache

def update_price(pair: str, price: float, source: str = "rest") -> None:
    """Update price and calculate RSI."""
    prices[pair] = {"price": price, "ts": time.time(), "source": source}
    update_rsi(pair, price)
    
    # Save to shared price cache
    try:
        PRICE_CACHE.write_text(json.dumps(prices))
    except Exception:
        pass

def fetch_all_prices() -> None:
    """Fetch prices for all pairs."""
    for pair in ALL_TRACKED:
        price = fetch_price_rest(pair)
        if price:
            update_price(pair, price, "rest")

# ============================================================================
# ANALYSIS MODE
# ============================================================================

_ticker_cache = {}  # {pair: {"high": x, "low": x, "ts": time}}

def analyze_pair(pair: str, force_refresh: bool = False) -> Dict:
    """Analyze a single pair. Uses cached prices/ticker if fresh to avoid rate-limits."""
    # Use cached price if fresh (<60s) and not forcing refresh
    cached = prices.get(pair, {})
    price = None
    if not force_refresh:
        age = time.time() - cached.get("ts", 0) if cached else 999
        if age < 60 and cached.get("price"):
            price = cached["price"]

    if not price:
        price = fetch_price_rest(pair)
        if not price:
            return {}
        update_price(pair, price)

    rsi = get_rsi(pair)
    daily_pos = get_daily_position(pair, price)
    multi_rsi = get_multi_rsi(pair, price)
    signal, score, reasons = get_signal(pair, price, multi_rsi)

    # Ticker — use cache if fresh (<5 min) to avoid extra API calls
    ticker_data = _ticker_cache.get(pair, {})
    ticker_age = time.time() - ticker_data.get("ts", 0) if ticker_data else 999
    if ticker_age > 300 or force_refresh:
        ticker = fetch_ticker_full(pair)
        if ticker:
            _ticker_cache[pair] = {"high": ticker["high"], "low": ticker["low"], "ts": time.time()}
            ticker_data = _ticker_cache[pair]

    return {
        "pair": pair,
        "price": price,
        "rsi": rsi,
        "multi_rsi": multi_rsi,
        "daily_position": daily_pos,
        "signal": signal,
        "score": score,
        "reasons": reasons,
        "high_24h": ticker_data.get("high") if ticker_data else None,
        "low_24h": ticker_data.get("low") if ticker_data else None,
    }

def print_portfolio_dashboard():
    """Display comprehensive portfolio dashboard with live Indodax prices."""
    log.info("\n" + "=" * 60)
    log.info("📊 PORTFOLIO DASHBOARD")
    log.info("=" * 60)
    
    # Fetch balance
    balance = get_balance(use_cache=False)
    idr_balance = balance.get("idr", 0)
    
    # Get all coin holdings
    holdings = {}
    for coin, amount in balance.items():
        if coin == "idr" or amount <= 0:
            continue
        holdings[coin] = amount
    
    # Prices for held coins — prefer WS cache (prices dict), fall back to REST only
    # for coins not in the WS feed (Fix #8: removes per-coin REST call in loop)
    coin_values = {}
    total_holdings_value = 0

    for coin, amount in holdings.items():
        ws_data = prices.get(coin, {})
        ws_age = time.time() - ws_data.get("ts", ws_data.get("updated", 0))
        price = ws_data.get("price") if ws_age < 30 else None
        if not price:
            price = fetch_price_rest(coin)
        if price:
            value = amount * price
            coin_values[coin] = {
                "amount": amount,
                "price": price,
                "value": value,
                "value_idr": value
            }
            total_holdings_value += value
        else:
            coin_values[coin] = {
                "amount": amount,
                "price": 0,
                "value": 0,
                "value_idr": 0
            }
    
    # Calculate total portfolio value
    total_portfolio_value = idr_balance + total_holdings_value
    
    log.info(f"\n💰 TOTAL PORTFOLIO VALUE: Rp {total_portfolio_value:,.0f}")
    log.info(f"   IDR Balance:         Rp {idr_balance:,.0f}")
    log.info(f"   Holdings Value:      Rp {total_holdings_value:,.0f}")
    
    # Show open positions with live P&L
    if state.positions:
        log.info("\n📁 OPEN POSITIONS:")
        position_data = []
        for pair, pos in state.positions.items():
            current_price_data = coin_values.get(pair, {})
            current_price = current_price_data.get("price")
            if not current_price:
                # Try WS cache before hitting REST
                ws_data = prices.get(pair, {})
                current_price = ws_data.get("price") or fetch_price_rest(pair)
            entry = pos["entry_price"]
            qty = pos.get("qty", 0)
            current_value = qty * current_price if current_price else 0
            entry_value = qty * entry
            pnl = current_value - entry_value
            pnl_pct = (current_price - entry) / entry * 100 if entry > 0 and current_price else 0
            
            position_data.append({
                "pair": pair,
                "entry": entry,
                "current": current_price,
                "qty": qty,
                "entry_value": entry_value,
                "current_value": current_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct
            })
        
        # Sort by PnL percentage
        position_data.sort(key=lambda x: x["pnl_pct"], reverse=True)
        
        for pos in position_data:
            emoji = "🟢" if pos["pnl_pct"] >= 0 else "🔴"
            pnl_str = f"{pos['pnl_pct']:+.1f}%"
            pnl_val_str = f"{pos['pnl']:+,.0f}"
            log.info(f"  {emoji} {pos['pair'].upper()}: Entry Rp {pos['entry']:,.0f} | "
                    f"Current Rp {pos['current']:,.0f} | "
                    f"Value Rp {pos['current_value']:,.0f} | "
                    f"PnL: {pnl_str} (Rp {pnl_val_str})")
    
    # Show top gainers and losers from holdings
    if coin_values:
        log.info("\n🏆 TOP GAINERS (24h - from price data):")
        gainers = []
        for coin, data in coin_values.items():
            if data["price"] > 0:
                # Use _ticker_cache or WS high/low to avoid per-coin REST call (Fix #8)
                ticker_data = _ticker_cache.get(coin, {})
                ticker_age = time.time() - ticker_data.get("ts", 0) if ticker_data else 999
                ws_data = prices.get(coin, {})
                low = None
                if ticker_age < 300:
                    low = ticker_data.get("low")
                if not low and ws_data.get("low"):
                    low = ws_data["low"]
                daily_change = ((data["price"] - low) / low * 100) if low and low > 0 else 0
                gainers.append({
                    "coin": coin,
                    "price": data["price"],
                    "value": data["value"],
                    "daily_change": daily_change
                })
        
        gainers.sort(key=lambda x: x["daily_change"], reverse=True)
        
        for g in gainers[:3]:
            log.info(f"  🟢 {g['coin'].upper()}: Rp {g['price']:,.0f} | "
                    f"Value: Rp {g['value']:,.0f} | "
                    f"24h: {g['daily_change']:+.1f}%")
        
        log.info("\n🔴 TOP LOSERS (24h - from price data):")
        for g in gainers[-3:]:
            log.info(f"  🔴 {g['coin'].upper()}: Rp {g['price']:,.0f} | "
                    f"Value: Rp {g['value']:,.0f} | "
                    f"24h: {g['daily_change']:+.1f}%")
    
    # Allocation breakdown
    if total_holdings_value > 0:
        log.info("\n📈 ALLOCATION BREAKDOWN:")
        alloc_data = []
        for coin, data in coin_values.items():
            if data["value"] > 0:
                alloc_pct = (data["value"] / total_holdings_value) * 100
                alloc_data.append({
                    "coin": coin,
                    "value": data["value"],
                    "pct": alloc_pct
                })
        
        alloc_data.sort(key=lambda x: x["pct"], reverse=True)
        
        for a in alloc_data:
            bar_len = int(a["pct"] / 2)
            bar = "█" * bar_len
            log.info(f"  {a['coin'].upper():8} Rp {a['value']:>15,.0f}  {a['pct']:5.1f}%  {bar}")
        
        # Also show IDR allocation
        idr_pct = (idr_balance / total_portfolio_value) * 100 if total_portfolio_value > 0 else 0
        bar_len = int(idr_pct / 2)
        bar = "█" * bar_len
        log.info(f"  {'IDR':8} Rp {idr_balance:>15,.0f}  {idr_pct:5.1f}%  {bar}")
    
    log.info("\n" + "=" * 60)


def print_analysis():
    """Print market analysis (no trading) with portfolio dashboard."""
    # Load cached prices first to avoid rate-limiting
    # Also seed price_history so RSI can compute
    try:
        if PRICE_CACHE.exists():
            cached = json.loads(PRICE_CACHE.read_text())
            for pair, data in cached.items():
                prices[pair] = data
                # Seed price_history with cached price as first entry
                if pair not in state.price_history:
                    state.price_history[pair] = []
                if data.get("price"):
                    state.price_history[pair] = [data["price"]]
    except Exception:
        pass

    # Pre-warm ticker cache for cached pairs (fetch in parallel with minimal calls)
    for pair in list(prices.keys())[:8]:
        ticker = fetch_ticker_full(pair)
        if ticker:
            _ticker_cache[pair] = {"high": ticker["high"], "low": ticker["low"], "ts": time.time()}
        time.sleep(0.3)  # staggered to avoid burst rate-limit

    log.info("=" * 60)
    log.info(f"Hermes Trader Analysis — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} WIB")
    log.info("=" * 60)
    
    # Fetch F&G
    fg_val, fg_class = fetch_fear_greed()
    regime, regime_desc = get_market_regime()
    regime_emoji = {"BULL": "🐂", "BEAR": "🐻", "SIDEWAYS": "↔️"}.get(regime, "?")
    log.info(f"Fear & Greed: {fg_val} ({fg_class}) {regime_emoji} {regime}")
    
    # Fetch Polymarket vibes
    vibes = fetch_polymarket_vibes()
    if vibes:
        log.info("\n🔮 Polymarket Vibes:")
        for v in vibes:
            pct = v["yes_price"] * 100
            log.info(f"  • {v['question'][:60]}...")
            log.info(f"    YES: {pct:.0f}% | NO: {100-pct:.0f}%")
    else:
        log.info("\n🔮 Polymarket Vibes: unavailable")
    
    # Fetch balance
    balance = get_balance(use_cache=False)
    log.info(f"IDR Balance: Rp {balance.get('idr', 0):,.0f}")
    
    log.info("\n📊 Pair Analysis:")
    log.info("-" * 60)
    
    # Analyze pairs — use cached prices if available, fetch sequentially with delay
    # to avoid rate-limiting. Prioritize active_pairs + positions.
    all_analyses = []
    priority_pairs = list(state.active_pairs) + [p for p in state.positions if p not in state.active_pairs]
    # Fill remaining with other tracked pairs
    for pair in ALL_TRACKED:
        if pair not in priority_pairs:
            priority_pairs.append(pair)
    
    for pair in priority_pairs:
        # Use cached price if fresh (<60s old), else fetch with delay
        cached = prices.get(pair, {})
        age = time.time() - cached.get("ts", 0) if cached else 999
        if age < 60 and cached.get("price"):
            analysis = analyze_pair(pair)  # uses cached price in prices dict
        else:
            analysis = analyze_pair(pair)
            if not analysis:
                time.sleep(0.5)  # delay on miss to avoid rate-limit burst
        if analysis:
            all_analyses.append(analysis)
    
    # Sort by score descending
    all_analyses.sort(key=lambda x: x["score"], reverse=True)
    
    # Show top movers first (non-HOLD signals)
    log.info("\n🎯 TOP SIGNALS:")
    shown = 0
    for analysis in all_analyses:
        if analysis["signal"] in ["STRONG_BUY", "BUY", "WEAK_BUY", "SELL", "STRONG_SELL"]:
            log.info(f"\n{analysis['pair'].upper():8} {analysis['signal']:12} (score: {analysis['score']:+d})")
            log.info(f"  Price: Rp {analysis['price']:>15,.0f}  RSI(3): {analysis['rsi']:.1f}  Daily Pos: {analysis['daily_position']:.0f}%")
            log.info(f"  Reasons: {', '.join(analysis['reasons'])}")
            if analysis.get('high_24h'):
                log.info(f"  24h Range: Rp {analysis['low_24h']:,.0f} - {analysis['high_24h']:,.0f}")
            shown += 1
    if shown == 0:
        log.info("  No strong signals (all HOLD)")
    
    # Show all in ranking table
    log.info("\n📋 FULL RANKING:")
    log.info(f"{'PAIR':<8} {'SIGNAL':<12} {'SCORE':>6}  {'PRICE':>15}  {'RSI':>6}  {'DPOS':>5}")
    log.info("-" * 70)
    for analysis in all_analyses:
        emoji = {"STRONG_BUY": "🟢+", "BUY": "🟢", "WEAK_BUY": "🟡", 
                "HOLD": "⚪", "SELL": "🔴", "STRONG_SELL": "🔴-"}.get(analysis["signal"], "?")
        log.info(f"{analysis['pair'].upper():8} {emoji} {analysis['signal']:<9} {analysis['score']:+4d}  "
                f"Rp {analysis['price']:>14,.0f}  {analysis['rsi']:>5.1f}  {analysis['daily_position']:>5.0f}%")
    
    # Show positions
    if state.positions:
        log.info("\n📁 Open Positions:")
        for pair, pos in state.positions.items():
            current = prices.get(pair, {}).get("price", 0)
            pnl_pct = (current - pos["entry_price"]) / pos["entry_price"] * 100 if current else 0
            log.info(f"  {pair.upper()}: Entry Rp {pos['entry_price']:,.0f} | "
                    f"Current Rp {current:,.0f} ({pnl_pct:+.1f}%) | "
                    f"Peak Rp {pos.get('peak_price', pos['entry_price']):,.0f} | "
                    f"SL: Rp {pos['stop_loss']:,.0f} | TP: Rp {pos['take_profit']:,.0f}")
    
    # Show portfolio dashboard
    print_portfolio_dashboard()
    
    log.info("\n" + "=" * 60)

# ============================================================================
# ONE-SHOT TRADING MODE
# ============================================================================

def run_one_shot():
    """Run one trading iteration and exit."""
    log.info("Running one-shot trading iteration...")
    
    # Fetch F&G
    fg_val, fg_class = fetch_fear_greed()
    log.info(f"Fear & Greed: {fg_val} ({fg_class})")
    
    # Get balance
    balance = get_balance(use_cache=False)
    idr = balance.get("idr", 0)
    log.info(f"IDR Balance: Rp {idr:,.0f}")
    
    if idr < MIN_TRADE_RP:
        log.warning(f"Balance ({idr}) below minimum ({MIN_TRADE_RP}). No buys.")
    
    # Fetch all prices and analyze
    fetch_all_prices()
    
    log.info("\n🎯 Checking trade opportunities...")
    
    # Check for take profit / stop loss on existing positions
    for pair in list(state.positions.keys()):
        if pair in prices:
            check_open_positions(prices[pair]["price"], balance)
    
    # Check for new entries (only if F&G is favorable)
    if fg_val <= FG_BUY_THRESHOLD:
        for pair in WS_PAIRS:
            if pair not in prices:
                continue
            if pair in state.positions:
                continue
            if idr < MIN_TRADE_RP:
                break
            
            price = prices[pair]["price"]
            if check_for_entries(pair, price, idr):
                idr -= MAX_TRADE_RP
    else:
        log.info(f"Market not in buy zone (F&G = {fg_val} > {FG_BUY_THRESHOLD})")
    
    # Save state
    state.save()
    
    # Print summary
    if state.positions:
        log.info("\n📁 Open Positions:")
        for pair, pos in state.positions.items():
            current = prices.get(pair, {}).get("price", 0)
            pnl_pct = (current - pos["entry_price"]) / pos["entry_price"] * 100 if current else 0
            log.info(f"  {pair.upper()}: Entry Rp {pos['entry_price']:,.0f} | "
                    f"PnL: {pnl_pct:+.1f}%")

# ============================================================================
# DAEMON MODE
# ============================================================================

# — PAIRED AUTO-ADJUST SYSTEM —
def rank_all_pairs() -> List[Tuple[str, int, str, float]]:
    """
    Score and rank ALL tracked pairs for trading priority.
    Returns list of (pair, score, signal, daily_pos) sorted by score descending.

    Uses cached multi_rsi (no fresh candle fetches) to avoid rate-limit bursts.
    Only falls back to a fresh get_multi_rsi() if no cache exists for a pair.
    """
    rankings = []
    for pair in ALL_TRACKED:
        if pair not in prices:
            continue
        price = prices[pair]["price"]
        if not price or price <= 0:
            continue

        # Prefer cached multi_rsi — avoids 4 REST calls per pair during ranking
        cached_mrsi = _multi_rsi_cache.get(pair, {})
        if cached_mrsi and (time.time() - cached_mrsi.get("ts", 0)) < _MULTI_RSI_TTL:
            multi_rsi = dict(cached_mrsi["rsi"])
            multi_rsi["3m"] = get_rsi(pair)  # RSI(3m) always live from WS
        else:
            # Cache cold for this pair — fetch once (candle cache will serve subsequent calls)
            multi_rsi = get_multi_rsi(pair, price)

        daily_pos = get_daily_position(pair, price)
        signal, score, reasons = get_signal(pair, price, multi_rsi)

        rankings.append((pair, score, signal, daily_pos))

    rankings.sort(key=lambda x: x[1], reverse=True)
    return rankings

def update_active_pairs() -> Tuple[List[str], List[Tuple[str, int, str, float]]]:
    """
    Auto-adjust active_pairs based on current signal scores + market regime.
    Keep pairs with open positions. Promote top-scoring pairs.
    Adjust max active based on regime.

    Returns (new_active_list, rankings) so callers can reuse rankings without
    triggering a second rank_all_pairs() call.
    """
    # Always keep pairs with open positions
    locked = set(state.positions.keys())

    # Get regime config
    regime_cfg = get_regime_trading_config()
    effective_max = int(MAX_ACTIVE_PAIRS * regime_cfg["max_active_multiplier"])
    effective_max = max(effective_max, MIN_ACTIVE_PAIRS)

    # Get rankings once — reused below and returned to caller
    rankings = rank_all_pairs()

    # Build new active list: locked + top scoring
    new_active = list(locked)
    slots = effective_max - len(locked)

    if slots > 0:
        for pair, score, signal, daily_pos in rankings:
            if pair in locked:
                continue
            # In BEAR mode: only trade pairs with score >= 3 (BUY+)
            min_score = 3 if regime_cfg["max_active_multiplier"] < 0.8 else 1
            if score >= min_score or (len(new_active) < MIN_ACTIVE_PAIRS and score >= 0):
                new_active.append(pair)
                slots -= 1
                if slots <= 0:
                    break

    # Ensure minimum
    if len(new_active) < MIN_ACTIVE_PAIRS:
        for pair, score, signal, daily_pos in rankings:
            if pair not in new_active:
                new_active.append(pair)
                if len(new_active) >= MIN_ACTIVE_PAIRS:
                    break

    # Sort: locked first, then by rank
    locked_list = [p for p in new_active if p in locked]
    unlocked_list = [p for p in new_active if p not in locked]

    unlocked_sorted = []
    for pair in unlocked_list:
        for r_pair, score, signal, daily_pos in rankings:
            if r_pair == pair:
                unlocked_sorted.append((pair, score))
                break
    unlocked_sorted.sort(key=lambda x: x[1], reverse=True)
    new_active = locked_list + [p for p, _ in unlocked_sorted]

    changed = set(new_active) != set(state.active_pairs)
    if changed:
        regime, _ = get_market_regime()
        log.info(f"[PAIR-ADJUST] Active pairs: {len(new_active)} ({regime} mode) — {', '.join(new_active)}")

    return new_active, rankings

async def daemon_pair_reassess():
    """
    Periodically re-rank all pairs and update active_pairs.
    This is the core of the autonomous pair selection system.
    Prices come from WebSocket — no REST polling needed.
    """
    while True:
        await asyncio.sleep(ANALYSIS_REASSESS_INTERVAL)
        
        try:
            # Prices are already updated via WebSocket in ws_client
            # No need to call fetch_all_prices() — that causes unnecessary REST API calls

            # update_active_pairs() returns rankings too — reuse to avoid a second
            # rank_all_pairs() call (Fix #3: eliminates double-ranking burst)
            new_active, rankings = update_active_pairs()
            state.active_pairs = new_active
            state.save()

            log.info(f"[PAIR-RANK] Top 5: " + " | ".join(
                f"{p}:{s}({sig})" for p, s, sig, _ in rankings[:5]
            ))
            
        except Exception as e:
            log.error(f"[PAIR-REASSESS] Error: {e}")

# ============================================================================
# PORTFOLIO REBALANCER
# ============================================================================

DAEMON_REBALANCE_INTERVAL = 21600   # Check rebalance every 6 hours
REBALANCE_DRIFT_THRESHOLD = 0.20    # Trigger rebalance if >20% drift from target

def get_portfolio_allocation() -> Dict[str, float]:
    """
    Calculate current portfolio allocation percentages.
    Returns {coin: pct_of_total}
    """
    balance = get_balance(use_cache=False)
    idr = balance.get("idr", 0)
    
    total = idr
    holdings = {}
    for coin, amount in balance.items():
        if coin == "idr" or amount <= 0:
            continue
        if coin not in prices:
            # Fetch price
            p = fetch_price_rest(coin)
            if not p:
                continue
            update_price(coin, p, "rest")
        
        price = prices.get(coin, {}).get("price", 0)
        if price and price > 0:
            val = amount * price
            holdings[coin] = val
            total += val
    
    if total <= 0:
        return {}
    
    return {coin: val / total for coin, val in holdings.items()}

async def daemon_rebalance():
    """
    Periodically check portfolio allocation drift and rebalance.
    Only sells over-allocated positions (no new buys for rebalance).
    Only runs in BULL or SIDEWAYS regime.
    """
    while True:
        await asyncio.sleep(DAEMON_REBALANCE_INTERVAL)
        
        try:
            regime, _ = get_market_regime()
            if regime == "BEAR":
                log.info("[REBALANCE] Skipping — BEAR regime, holding positions")
                continue
            
            if not state.positions:
                log.info("[REBALANCE] No positions to rebalance")
                continue
            
            balance = get_balance(use_cache=True)
            idr = balance.get("idr", 0)
            
            # Calculate current allocation
            alloc = get_portfolio_allocation()
            if not alloc:
                continue
            
            # Check drift per position
            num_positions = len(state.positions)
            if num_positions == 0:
                continue
            
            target_pct = 1.0 / num_positions  # Equal weight target
            
            log.info(f"[REBALANCE] Checking {num_positions} positions...")
            
            for pair, pos in state.positions.items():
                current_pct = alloc.get(pair, 0)
                drift = current_pct - target_pct
                
                if drift > REBALANCE_DRIFT_THRESHOLD:
                    # Over-allocated — consider selling excess
                    log.info(f"[REBALANCE] {pair.upper()}: {current_pct:.1%} (target: {target_pct:.1%}, drift: {drift:+.1%})")
                    # Only sell if profit is locked
                    current_price = prices.get(pair, {}).get("price", 0)
                    if current_price and current_price > pos["entry_price"]:
                        pnl_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
                        if pnl_pct >= 5:  # At least 5% profit before selling
                            log.info(f"[REBALANCE] Taking profit on {pair.upper()} ({pnl_pct:+.1f}%) to rebalance")
                            # Partial sell: 30% of position
                            sell_qty = pos["qty"] * 0.3
                            if sell_qty > 0:
                                execute_sell(pair, current_price, sell_qty, "rebalance_drift")
            
            state.save()
            
        except Exception as e:
            log.error(f"[REBALANCE] Error: {e}")

async def daemon_trade_check():
    """Periodically check for trade opportunities."""
    while True:
        await asyncio.sleep(DAEMON_TRADE_CHECK_INTERVAL)
        
        try:
            # Use cached balance to avoid rate limiting the balance API
            # (limited to ~1 req/min on Indodax)
            balance = get_balance(use_cache=True)
            idr = balance.get("idr", 0)
            
            if idr < MIN_TRADE_RP and not state.positions:
                continue
            
            # Check existing positions for TP/SL
            for pair in list(state.positions.keys()):
                if pair in prices:
                    check_open_positions(prices[pair]["price"], balance)
            
            # Check for new entries — use dynamic active_pairs
            if state.fg_value <= FG_BUY_THRESHOLD:
                for pair in state.active_pairs:
                    if pair not in prices:
                        continue
                    if pair in state.positions:
                        continue
                    if state.last_trade_time.get(pair, 0) > time.time() - TRADE_COOLDOWN:
                        continue
                    
                    price = prices[pair]["price"]
                    multi_rsi = get_multi_rsi(pair, price)
                    signal, score, reasons = get_signal(pair, price, multi_rsi)
                    
                    if signal in ["STRONG_BUY", "BUY"]:
                        # Fresh balance only when we're about to execute a trade
                        if idr >= MIN_TRADE_RP:
                            live_balance = get_balance(use_cache=False)
                            idr = live_balance.get("idr", 0)
                            if idr >= MIN_TRADE_RP and execute_buy(pair, price, idr):
                                idr -= MAX_TRADE_RP
            
            state.save()
            
        except Exception as e:
            log.error(f"Trade check error: {e}")

async def daemon_fg_fetch():
    """Periodically fetch Fear & Greed."""
    while True:
        await asyncio.sleep(DAEMON_FG_FETCH_INTERVAL)
        fetch_fear_greed()

async def daemon_morning_brief():
    """Send morning brief at 07:00 WIB."""
    while True:
        now = datetime.now()
        target = now.replace(hour=7, minute=0, second=0, microsecond=0)
        
        if now.hour >= 7:
            target = target.replace(day=now.day + 1)
        
        wait_seconds = (target - now).total_seconds()
        await asyncio.sleep(wait_seconds)
        
        # Generate brief
        log.info("\n" + "=" * 60)
        log.info(f"HERMES MORNING BRIEF — {datetime.now().strftime('%d %b %Y, %H:%M WIB')}")
        log.info("=" * 60)
        
        fetch_fear_greed()
        regime, regime_desc = get_market_regime()
        regime_emoji = {"BULL": "🐂", "BEAR": "🐻", "SIDEWAYS": "↔️"}.get(regime, "?")
        log.info(f"Fear & Greed: {state.fg_value} ({state.fg_class}) {regime_emoji} {regime}")
        
        balance = get_balance(use_cache=True)
        log.info(f"IDR Balance: Rp {balance.get('idr', 0):,.0f}")
        
        log.info("\n📊 Live Prices:")
        for pair in WS_PAIRS:
            if pair in prices:
                p = prices[pair]
                # Use cached multi_rsi to avoid 30-pair × 4-candle burst (Fix #7)
                cached_mrsi = _multi_rsi_cache.get(pair, {})
                if cached_mrsi and (time.time() - cached_mrsi.get("ts", 0)) < _MULTI_RSI_TTL:
                    multi_rsi = dict(cached_mrsi["rsi"])
                    multi_rsi["3m"] = get_rsi(pair)
                else:
                    multi_rsi = get_multi_rsi(pair, p["price"])
                signal, score, reasons = get_signal(pair, p["price"], multi_rsi)
                emoji = {"STRONG_BUY": "🟢", "BUY": "🟢", "HOLD": "🟡",
                        "SELL": "🔴", "STRONG_SELL": "🔴"}.get(signal, "⚪")
                log.info(f"  {pair.upper():6} Rp {p['price']:>15,.0f}  {emoji} {signal}")
        
        log.info("=" * 60)

async def run_daemon():
    """Run the trading daemon."""
    log.info("Hermes Trader Daemon starting...")
    log.info(f"Strategy: F&G + RSI(3) + Daily Position + Dynamic Pair Selection")
    log.info(f"Tracked pairs: {len(ALL_TRACKED)} ({', '.join(ALL_TRACKED)})")
    log.info(f"Max active pairs: {MAX_ACTIVE_PAIRS} | Reassess every: {ANALYSIS_REASSESS_INTERVAL}s")
    log.info(f"Max trade: Rp {MAX_TRADE_RP:,} | Stop Loss: {STOP_LOSS_PCT*100:.0f}% | Take Profit: {TAKE_PROFIT_PCT*100:.0f}%")
    
    # Initial F&G fetch
    fetch_fear_greed()
    log.info(f"Fear & Greed: {state.fg_value} ({state.fg_class})")
    
    # Initial price fetch
    fetch_all_prices()
    log.info(f"Loaded {len(prices)} prices")
    
    # Initial pair assessment
    if not state.active_pairs:
        state.active_pairs, _ = update_active_pairs()
        state.save()
    log.info(f"Active pairs: {', '.join(state.active_pairs)}")
    
    # Start daemon tasks (no REST polling — all prices via WS)
    await asyncio.gather(
        asyncio.create_task(ws_client.run_forever()),
        daemon_trade_check(),
        daemon_fg_fetch(),
        daemon_pair_reassess(),
        daemon_rebalance(),
        daemon_morning_brief(),
    )

# ============================================================================
# WEBSOCKET LAYER (Indodax Real-time)
# ============================================================================

WS_URL = "wss://ws3.indodax.com/ws/"
WS_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE5NDY2MTg0MTV9.UR1lBM6Eqh0yWz-PVirw1uPCxe60FdchR8eNVdsskeo"
WS_PAIRS_WS_FORMAT = [f"{p}idr" for p in WS_PAIRS]

def _ws_price_update(pair, data):
    """Callback to update global prices dict from WebSocket data."""
    prices[pair] = {
        "price": data["price"],
        "bid": data["bid"],
        "ask": data["ask"],
        "high": data.get("high"),
        "low": data.get("low"),
        "updated": time.time()
    }

class IndodaxWS:
    """Indodax WebSocket client for real-time price feeds."""

    def __init__(self, pairs=None, on_price=None):
        self.pairs = pairs or WS_PAIRS_WS_FORMAT
        self.on_price = on_price  # callback(pair, price_data)
        self.ws = None
        self.connected = False
        self.loop = None
        self.recv_task = None
        self.reconnect_delay = 5

    async def connect(self):
        self.ws = await websockets.connect(WS_URL, origin="https://indodax.com")
        self.connected = True
        self.reconnect_delay = 5
        # Auth
        await self.ws.send(json.dumps({"id": 1, "params": {"token": WS_TOKEN}}))
        await asyncio.sleep(2)
        # Subscribe to orderbook for each pair
        for pair in self.pairs:
            await self.ws.send(json.dumps({
                "id": 10 + self.pairs.index(pair),
                "method": 1,
                "params": {"channel": f"market:order-book-{pair}"}
            }))
            await asyncio.sleep(0.3)
        # Subscribe to 24h summary
        await self.ws.send(json.dumps({"id": 90, "method": 1, "params": {"channel": "market:summary-24h"}}))
        log.info(f"[WS] Connected and subscribed to {len(self.pairs)} pairs")

    async def listen(self):
        """Listen for messages indefinitely. Updates self.on_price callback."""
        while self.connected:
            try:
                msg = await self.ws.recv()
                # WS may send multiple JSON objects concatenated — split by newline
                for line in msg.strip().split('\n'):
                    if line:
                        try:
                            self._handle_message(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            except websockets.ConnectionClosed:
                log.warning("[WS] Connection closed")
                break
            except Exception as e:
                log.error(f"[WS] Recv error: {e}")
                await asyncio.sleep(1)

    def _handle_message(self, msg):
        try:
            result = msg.get("result", {})
            channel = result.get("channel", "")
            data = result.get("data", {})

            if channel == "market:summary-24h":
                summary_data = data.get("data", data)
                if isinstance(summary_data, list):
                    for item in summary_data:
                        if item and len(item) >= 6:
                            symbol = item[0].lower().replace("idr", "")
                            pair_ws = f"{symbol}idr"
                            if pair_ws in self.pairs:
                                price = float(item[4])
                                if self.on_price:
                                    self.on_price(pair_ws, {
                                        "price": price,
                                        "bid": price,
                                        "ask": price,
                                        "high": float(item[2]),
                                        "low": float(item[3]),
                                        "vol": float(item[5]),
                                        "source": "ws_summary"
                                    })
                return

            if channel.startswith("market:order-book-"):
                pair = channel.replace("market:order-book-", "")
                book_data = data.get("data", data)
                if not isinstance(book_data, dict):
                    return
                asks = book_data.get("ask", [])
                bids = book_data.get("bid", [])
                if asks and bids:
                    best_ask = float(asks[0]["price"])
                    best_bid = float(bids[0]["price"])
                    mid = (best_ask + best_bid) / 2
                    if self.on_price:
                        self.on_price(pair, {
                            "price": mid,
                            "bid": best_bid,
                            "ask": best_ask,
                            "high": best_ask,
                            "low": best_bid,
                            "source": "ws_orderbook"
                        })
        except Exception as e:
            pass

    async def run_forever(self):
        """Connect, listen, reconnect loop."""
        while True:
            try:
                await self.connect()
                await self.listen()
            except Exception as e:
                log.error(f"[WS] Error: {e}")
            if self.connected:
                self.connected = False
            log.info(f"[WS] Reconnecting in {self.reconnect_delay}s...")
            await asyncio.sleep(self.reconnect_delay)
            self.reconnect_delay = min(self.reconnect_delay * 2, 60)

    def close(self):
        self.connected = False
        if self.ws:
            try:
                self.loop.create_task(self.ws.close())
            except:
                pass

ws_client = IndodaxWS(on_price=_ws_price_update)

# ============================================================================
# MAIN
# ============================================================================

def check_pid_file():
    """Check for stale PID file to prevent duplicate daemons."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if process is still running
            try:
                os.kill(old_pid, 0)
                print(f"Daemon already running (PID {old_pid}). Exiting.")
                sys.exit(1)
            except OSError:
                # Stale PID file — process is dead
                pass
        except (ValueError, OSError):
            pass
    # Write our PID
    PID_FILE.write_text(str(os.getpid()))

def main():
    parser = argparse.ArgumentParser(description="Hermes Autonomous Crypto Trader")
    parser.add_argument("--daemon", action="store_true", help="Run as continuous daemon")
    parser.add_argument("--one-shot", action="store_true", help="Run one iteration and exit")
    parser.add_argument("--analyze", action="store_true", help="Analyze market without trading")
    
    args = parser.parse_args()
    
    if args.daemon:
        check_pid_file()
        try:
            asyncio.run(run_daemon())
        finally:
            if PID_FILE.exists():
                PID_FILE.unlink()
    elif args.analyze:
        print_analysis()
    else:
        # Default: run one shot
        run_one_shot()

if __name__ == "__main__":
    main()
