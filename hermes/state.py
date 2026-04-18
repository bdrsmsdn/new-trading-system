import json
from typing import Dict, List, Optional
from hermes.config import STATE_FILE, ALL_TRACKED, INITIAL_ACTIVE
from hermes.logging_setup import log

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
        self.dry_run = False
        self._last_regime = "SIDEWAYS"  # track for regime change detection
        self.load()
    
    def load(self):
        """Load state from disk."""
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self.positions = data.get("positions", {})
                self.last_trade_time = data.get("last_trade_time", {})
                self.active_pairs = data.get("active_pairs", INITIAL_ACTIVE.copy())
                self.dry_run = data.get("dry_run", False)
                self._last_regime = data.get("last_regime", "SIDEWAYS")
            except Exception as e:
                log.warning(f"Failed to load state: {e}")
                self.active_pairs = INITIAL_ACTIVE.copy()
        
        # Initialize RSI state for all pairs
        for pair in ALL_TRACKED:
            if pair not in self.rsi_state:
                self.rsi_state[pair] = {
                    "avg_gain": 0, "avg_loss": 0, 
                    "last_price": 0, "initialized": False
                }
        
        # Ensure active_pairs always has minimum
        if not self.active_pairs:
            self.active_pairs = INITIAL_ACTIVE.copy()
    
    def save(self):
        """Save state to disk."""
        try:
            STATE_FILE.write_text(json.dumps({
                "positions": self.positions,
                "last_trade_time": self.last_trade_time,
                "active_pairs": self.active_pairs,
                "dry_run": self.dry_run,
                "last_regime": self._last_regime,
            }, indent=2))
        except Exception as e:
            log.error(f"Failed to save state: {e}")

# Global singleton
state = State()

# Global caches
prices = {}
_ticker_cache = {}
_candle_cache = {}
_multi_rsi_cache = {}
