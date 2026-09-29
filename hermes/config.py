import os
from pathlib import Path
from typing import Optional, Dict, Any

# Paths
SCRIPT_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = SCRIPT_DIR / "hermes_trader_state.json"
PRICE_CACHE = SCRIPT_DIR / "hermes_prices.json"
ENV_FILE = SCRIPT_DIR / ".env"
PID_FILE = SCRIPT_DIR / "daemon.pid"
ACCOUNTING_DB_PATH = Path(os.environ.get("ACCOUNTING_DB_PATH", str(SCRIPT_DIR / "hermes_ledger.db")))
LEDGER_PATH = ACCOUNTING_DB_PATH

def load_env() -> dict:
    """Load environment variables from .env file."""
    env_vars = {}
    if ENV_FILE.exists():
        try:
            for line in ENV_FILE.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    # Strip whitespace AND any enclosing quotes
                    env_vars[key.strip()] = value.strip().strip("'\"")
        except Exception as e:
            print(f"Warning: Failed to load .env file: {e}")
    return env_vars

_env = load_env()

def parse_bool_env(key: str, default: bool = False, env_dict: Optional[Dict[str, Any]] = None) -> bool:
    """Parse a boolean environment variable from os.environ or loaded .env dictionary.

    Explicit opt-in required; defaults to False unless explicitly set to truthy string.
    """
    if env_dict is None:
        env_dict = _env
    raw = os.environ.get(key, env_dict.get(key, None))
    if isinstance(raw, bool):
        return raw
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("true", "1", "yes", "y", "t", "on")

# Accounting & Ledger Feature Gate (Explicit Opt-In Required, Default False)
ACCOUNTING_ENABLED = parse_bool_env("ACCOUNTING_ENABLED", False)

TELEGRAM_BOT_TOKEN = _env.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env.get("TELEGRAM_CHAT_ID", "")

# 9router AI Agent (OpenAI-compatible API via local 9router)
ROUTER_API_KEY = _env.get("ROUTER_API_KEY", _env.get("MINIMAX_API_KEY", ""))
ROUTER_BASE_URL = _env.get("ROUTER_BASE_URL", "http://127.0.0.1:20128/v1")
ROUTER_MODEL = _env.get("ROUTER_MODEL", "ag/gemini-3.7-flash-high")
AGENT_MEMORY_FILE = SCRIPT_DIR / "hermes_agent_memory.json"

# Binance Configuration
TESTNET = parse_bool_env("TESTNET", False)

# Trading Parameters (USDT amounts for Binance)
MAX_TRADE_USDT = 100
MIN_TRADE_USDT = 5.50              # Binance strict minNotional is 5.00 USDT — enforce 5.50 to avoid filter rejections
TARGET_PORTFOLIO_SLOTS = 4         # Divide Spot equity into ~4 balanced position slots
MIN_MEANINGFUL_TRADE_USDT = 10.0   # Preferred slot trade size when portfolio allows
STOP_LOSS_PCT = 0.05               # 5.0% Stop Loss
TAKE_PROFIT_PCT = 0.10             # 10.0% Take Profit Target
TRAILING_ACTIVATION_PCT = 0.06     # Trailing Stop starts once +6.0% in profit
TRAILING_STOP_PCT = 0.025          # 2.5% trailing pullback tolerance
FEE_BUFFER = 0.03
TRADE_COOLDOWN = 60

# Futures Trading Gate (Explicit Opt-In Required, Default False)
FUTURES_ENABLED = parse_bool_env("FUTURES_ENABLED", False)

# Centralized Portfolio Risk Gates
RISK_BUDGET_PER_TRADE_PCT = float(os.getenv("RISK_BUDGET_PER_TRADE_PCT", "0.01"))      # 1.0% max equity risk per trade (~$1.70 on $170 equity)
MAX_AGGREGATE_STOP_RISK_PCT = 0.03     # 3.0% max aggregate planned stop risk
DAILY_LOSS_CIRCUIT_BREAKER_PCT = 0.02  # 2.0% mark-to-market daily loss circuit breaker
MIN_EQUITY_USDT_RESERVE_PCT = 0.25     # 25.0% minimum tradable USDT cash reserve

# Capital Rotation & Opportunity Cost Parameters (Explicit Opt-In Required, Default False)
ROTATION_ENABLED = parse_bool_env("ROTATION_ENABLED", False)
ROTATION_SHADOW_MODE = parse_bool_env("ROTATION_SHADOW_MODE", True)
ROTATION_MIN_HOLD_SECS = 1800      # 30 mins holding time minimum before rotating out
ROTATION_SCORE_DELTA = 3           # Candidate must score at least 3 points higher than stagnant position
ROTATION_MAX_PNL_PCT = 0.02        # Only rotate out positions with <= +2.0% profit (never cut winners!)
ROTATION_MIN_PNL_PCT = -0.045      # Positions with PnL between -4.5% and +2.0% are eligible
ROTATION_COOLDOWN_SECS = 1800      # Max 1 capital rotation every 30 minutes

# Strategy Thresholds
FG_BUY_THRESHOLD = 30
FG_STRONG_BUY = 20
FG_SELL_THRESHOLD = 70
RSI_BUY_THRESHOLD = 35
RSI_STRONG_BUY = 30
RSI_SELL_THRESHOLD = 65
DAILY_POS_BUY = 30
DAILY_POS_SELL = 70

# Pair Configuration (Binance uses UPPERCASE symbols)
ALL_TRACKED = [
    "DOGE", "XRP", "TON", "SOL", "BTC", "ETH", "BNB",
    "PEPE", "NEIROCTO", "FLOKI",
    "SHIB", "ADA", "MATIC", "LINK", "AVAX",
    "DOT", "BONK", "DOGEWIF", "LABU", "ORTO",
    "NEAR", "ALGO", "TRX", "SAND", "MANA",
    "AXS", "ENJ", "FTM", "ATOM", "UNI",
]

WS_PAIRS = ALL_TRACKED
MAX_ACTIVE_PAIRS = 8
MIN_ACTIVE_PAIRS = 3
INITIAL_ACTIVE = ["DOGE", "XRP", "TON", "SOL", "BTC", "ETH"]
ANALYSIS_REASSESS_INTERVAL = 300
WS_PRIORITY_PAIRS = {"DOGE", "XRP", "TON", "SOL", "BTC", "ETH", "BNB"}

PAIR_DECIMAL_PLACES = {
    # Binance precision (quantity decimal places)
    "DOGE": 0, "XRP": 1, "TON": 3, "BTC": 6, "ETH": 5, "BNB": 4,
    "SOL": 4, "ADA": 1, "MATIC": 4, "LINK": 4,
    "AVAX": 4, "DOT": 2, "NEAR": 3, "ALGO": 2, "TRX": 4,
    "AXS": 2, "ENJ": 2, "FTM": 2, "ATOM": 2, "UNI": 2,
    "SAND": 2, "MANA": 2,
    # Low-value coins needing more precision
    "PEPE": 0, "NEIROCTO": 0, "FLOKI": 0,
    "SHIB": 0, "BONK": 0, "DOGEWIF": 0, "LABU": 0, "ORTO": 0,
}

# Daemon settings
DAEMON_TRADE_CHECK_INTERVAL = 5
DAEMON_FG_FETCH_INTERVAL = 300
DAEMON_REBALANCE_INTERVAL = 21600
REBALANCE_DRIFT_THRESHOLD = 0.20
USE_STRATEGY_V2 = True
SPIKE_TRIGGER_PCT = 0.012  # 1.2% instant spike triggers immediate V2 evaluation

# Profit Auto-Sweep & Daily Profit Collection
# DAILY COLLECTION MODEL: Disabled by default (DAILY_PROFIT_COLLECTION=False)
# pending full net loss/fill/fee accounting ledger implementation (T3/T5).
# Legacy per-trade sweep (AUTO_SWEEP_PROFIT_TO_FUNDING) remains False.
# Explicit env opt-in is required to enable collection.
AUTO_SWEEP_PROFIT_TO_FUNDING = parse_bool_env("AUTO_SWEEP_PROFIT_TO_FUNDING", False)
PROFIT_SWEEP_MIN_USDT = 0.05
DAILY_PROFIT_COLLECTION = parse_bool_env("DAILY_PROFIT_COLLECTION", False)
DAILY_PROFIT_TARGET_USDT = 1.0

# DCA (Dollar Cost Averaging) settings
DCA_CHECK_INTERVAL = 300          # Check every 5 minutes
DCA_TRIGGER_PCT = 0.05            # Buy when price drops 5% below avg entry
DCA_AMOUNT_PCT = 0.10             # Buy 10% of IDR balance per DCA
DCA_MAX_COUNT = 5                 # Max 5 DCA buys per position
DCA_COOLDOWN_MINUTES = 30         # 30 minutes between DCA triggers
DCA_ACTIVE_PAIRS = ["DOGE", "XRP", "TON", "SOL", "BTC", "ETH"]  # Pairs to DCA

# Binance rate limit: 1200 requests/minute
_REST_BUDGET_MAX = 1000
