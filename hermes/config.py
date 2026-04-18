import os
from pathlib import Path

# Paths
SCRIPT_DIR = Path.cwd()
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

API_KEY = _env.get("API_KEY", "")
API_SECRET = _env.get("API_SECRET", "")
# Expanded user profile properly for Windows or Linux
NONCE_FILE = Path(os.path.expanduser("~")) / ".hermes" / "trading" / ".nonce"
NONCE_FILE.parent.mkdir(parents=True, exist_ok=True)

TELEGRAM_BOT_TOKEN = _env.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _env.get("TELEGRAM_CHAT_ID", "")

# MiniMax AI Agent (Anthropic-compatible API)
MINIMAX_API_KEY = _env.get("MINIMAX_API_KEY", "")
MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
MINIMAX_MODEL = "MiniMax-M2.7"
AGENT_MEMORY_FILE = SCRIPT_DIR / "hermes_agent_memory.json"

# Trading Parameters
MAX_TRADE_RP = 1_000_000
MIN_TRADE_RP = 10_000
STOP_LOSS_PCT = 0.05
TAKE_PROFIT_PCT = 0.10
TRAILING_STOP_PCT = 0.03
TRAILING_ACTIVATION_PCT = 0.05
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

# Pair Configuration
ALL_TRACKED = [
    "doge", "xrp", "ton", "sol", "btc", "eth", "bnb",
    "pepe", "neirocto", "floki", 
    "shib", "ada", "matic", "link", "avax",
    "dot", "bonk", "dogewif", "labu", "orto",
    "near", "algo", "trx", "sand", "mana",
    "axs", "enj", "ftm", "atom", "uni",
]

WS_PAIRS = ALL_TRACKED
MAX_ACTIVE_PAIRS = 8
MIN_ACTIVE_PAIRS = 3
INITIAL_ACTIVE = ["doge", "xrp", "ton", "sol", "btc", "eth"]
ANALYSIS_REASSESS_INTERVAL = 300
WS_PRIORITY_PAIRS = {"doge", "xrp", "ton", "sol", "btc", "eth", "bnb"}

PAIR_DECIMAL_PLACES = {
    # coins with prices >= 1 IDR
    "doge": 0, "xrp": 2, "ton": 2, "btc": 6, "eth": 5, "bnb": 4,
    "sol": 4, "ada": 2, "matic": 0, "link": 4,
    "avax": 4, "dot": 3, "near": 4, "algo": 3, "trx": 2,
    "axs": 2, "enj": 2, "ftm": 2, "atom": 3, "uni": 2,
    "sand": 2, "mana": 2,
    # coins with prices < 1 IDR — Indodax pricescale (smallest unit = 1e-06 IDR)
    "pepe": 6, "neirocto": 6, "floki": 6,
    "shib": 6, "bonk": 6, "dogewif": 6, "labu": 6, "orto": 6,
}

# Daemon settings
DAEMON_TRADE_CHECK_INTERVAL = 60
DAEMON_FG_FETCH_INTERVAL = 300
DAEMON_REBALANCE_INTERVAL = 21600
REBALANCE_DRIFT_THRESHOLD = 0.20
USE_STRATEGY_V2 = True

# DCA (Dollar Cost Averaging) settings
DCA_CHECK_INTERVAL = 300          # Check every 5 minutes
DCA_TRIGGER_PCT = 0.05            # Buy when price drops 5% below avg entry
DCA_AMOUNT_PCT = 0.10             # Buy 10% of IDR balance per DCA
DCA_MAX_COUNT = 5                 # Max 5 DCA buys per position
DCA_COOLDOWN_MINUTES = 30         # 30 minutes between DCA triggers
DCA_ACTIVE_PAIRS = ["doge", "xrp", "ton", "sol", "btc", "eth"]  # Pairs to DCA
