import json
import requests
from typing import Tuple, List, Dict
from hermes.logging_setup import log
from hermes.state import state
from hermes.api.rest import _throttled_public_get

def fetch_fear_greed() -> Tuple[int, str]:
    """Fetch Fear & Greed index from alternative.me via throttled public GET."""
    body = _throttled_public_get("https://api.alternative.me/fng/?limit=1")
    if body is None:
        log.warning("F&G fetch failed: rate limited or error, using cached value")
        return state.fg_value, state.fg_class
    try:
        data = json.loads(body)
        fg_value = int(data["data"][0]["value"])
        fg_class = data["data"][0]["value_classification"]
        state.fg_value = fg_value
        state.fg_class = fg_class
        return fg_value, fg_class
    except Exception as e:
        log.warning(f"F&G parse failed: {e}, using cached value")
        return state.fg_value, state.fg_class

def fetch_polymarket_vibes() -> List[Dict]:
    """Fetch top 3 crypto-related prediction markets from Polymarket."""
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
        vibes = []
        crypto_keywords = ["bitcoin", "btc", "ethereum", "eth", "crypto", "solana",
                          "sol", "dogecoin", "doge", "xrp", "bnb", "cardano", "ada",
                          "ton ", "toncoin", "pepe", "floki", "solana"]
        
        for m in markets:
            question = m.get("question", "").lower()
            if not any(kw in question for kw in crypto_keywords):
                continue
            
            closed = m.get("closed", "")
            if str(closed).lower() == "true":
                continue
            
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
