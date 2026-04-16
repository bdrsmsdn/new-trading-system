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
                    env_vars[key.strip()] = value.strip()
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

# Trading Parameters
MAX_TRADE_RP = 10_000
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
    "doge": 0, "xrp": 2, "ton": 2, "btc": 6, "eth": 5, "bnb": 4,
    "sol": 4, "shib": 0, "ada": 2, "matic": 2, "link": 4,
    "avax": 4, "dot": 3, "near": 4, "algo": 3, "trx": 2,
    "pepe": 0, "neirocto": 0, "floki": 0,
}

# Daemon settings
DAEMON_TRADE_CHECK_INTERVAL = 60
DAEMON_FG_FETCH_INTERVAL = 300
DAEMON_REBALANCE_INTERVAL = 21600
REBALANCE_DRIFT_THRESHOLD = 0.20
