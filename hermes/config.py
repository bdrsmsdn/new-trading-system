import os
from pathlib import Path

# Paths
SCRIPT_DIR = Path(__file__).resolve().parent.parent
STATE_FILE = SCRIPT_DIR / "hermes_trader_state.json"
PRICE_CACHE = SCRIPT_DIR / "hermes_prices.json"
ENV_FILE = SCRIPT_DIR / ".env"
PID_FILE = SCRIPT_DIR / "daemon.pid"

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

TELEGRAM_BOT_TOKEN = _env.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env.get("TELEGRAM_CHAT_ID", "")

# 9router AI Agent (OpenAI-compatible API via local 9router)
ROUTER_API_KEY = _env.get("ROUTER_API_KEY", _env.get("MINIMAX_API_KEY", ""))
ROUTER_BASE_URL = _env.get("ROUTER_BASE_URL", "http://127.0.0.1:20128/v1")
ROUTER_MODEL = _env.get("ROUTER_MODEL", "ag/gemini-3.7-flash-high")
AGENT_MEMORY_FILE = SCRIPT_DIR / "hermes_agent_memory.json"

# Binance Configuration
TESTNET = _env.get("TESTNET", "false").lower() == "true"

# Trading Parameters (USDT amounts for Binance)
MAX_TRADE_USDT = 100
MIN_TRADE_USDT = 1
STOP_LOSS_PCT = 0.05            # 5.0% Stop Loss
TAKE_PROFIT_PCT = 0.10          # 10.0% Take Profit Target
TRAILING_ACTIVATION_PCT = 0.06  # Trailing Stop starts once +6.0% in profit
TRAILING_STOP_PCT = 0.025       # 2.5% trailing pullback tolerance
FEE_BUFFER = 0.03
TRADE_COOLDOWN = 60

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

# Profit Auto-Sweep to Funding Wallet (Survival & P2P IDR Fund)
AUTO_SWEEP_PROFIT_TO_FUNDING = True
PROFIT_SWEEP_MIN_USDT = 0.05

# DCA (Dollar Cost Averaging) settings
DCA_CHECK_INTERVAL = 300          # Check every 5 minutes
DCA_TRIGGER_PCT = 0.05            # Buy when price drops 5% below avg entry
DCA_AMOUNT_PCT = 0.10             # Buy 10% of IDR balance per DCA
DCA_MAX_COUNT = 5                 # Max 5 DCA buys per position
DCA_COOLDOWN_MINUTES = 30         # 30 minutes between DCA triggers
DCA_ACTIVE_PAIRS = ["DOGE", "XRP", "TON", "SOL", "BTC", "ETH"]  # Pairs to DCA

# Binance rate limit: 1200 requests/minute
_REST_BUDGET_MAX = 1000
