"""Orderbook depth analysis for Binance trading pairs."""
import json
import time
import subprocess
import inspect
from typing import Optional, Dict, Tuple
from dataclasses import dataclass
from hermes.logging_setup import log
from hermes.api.rest import _check_budget, _consume_budget, _throttled_public_get

_ORDERBOOK_TTL = 60  # 60 seconds cache


@dataclass
class OrderbookData:
    """Orderbook data structure."""
    bids: list  # List of [price, volume] descending by price
    asks: list  # List of [price, volume] ascending by price
    bid_volume: float
    ask_volume: float
    imbalance: float  # bid_vol / ask_vol ratio
    spread: float
    spread_pct: float
    thick_bid_level: Optional[float]  # Price level with thick bids (support)
    thick_ask_level: Optional[float]  # Price level with thick asks (resistance)
    ts: float
    bid_notional: float = 0.0  # Total quote notional of bids (USDT)
    ask_notional: float = 0.0  # Total quote notional of asks (USDT)

    def is_fresh(self, max_age: float = 60.0) -> bool:
        """Check if orderbook data is within TTL."""
        return (time.time() - self.ts) <= max_age


# Orderbook cache
_orderbook_cache: Dict[str, OrderbookData] = {}


def _fetch_orderbook_raw(pair: str) -> Optional[str]:
    """Fetch raw orderbook from Binance public API.

    Args:
        pair: Trading pair (e.g., 'DOGE', 'BTC')

    Returns:
        Raw JSON response or None on error
    """
    if not _check_budget():
        return None

    # Binance depth endpoint
    url = f"https://api.binance.com/api/v3/depth?symbol={pair.upper()}USDT&limit=20"

    try:
        result = subprocess.run(
            ["curl", "-s", "-A", "Mozilla/5.0", "-w", "\n%{http_code}", url],
            capture_output=True, text=True, timeout=10
        )
        _consume_budget()

        parts = result.stdout.rsplit("\n", 1)
        body = parts[0] if len(parts) == 2 else result.stdout
        status_code = parts[1].strip() if len(parts) == 2 else "200"

        if status_code == "429":
            log.warning(f"[ORDERBOOK] HTTP 429 from Binance — skipping fetch")
            return None

        return body
    except Exception as e:
        log.debug(f"[ORDERBOOK] Fetch failed for {pair}: {e}")
        return None


def parse_orderbook(response: str, pair: str) -> Optional[OrderbookData]:
    """Parse Binance orderbook response.

    Args:
        response: Raw JSON response
        pair: Trading pair for logging

    Returns:
        OrderbookData or None on parse error
    """
    try:
        data = json.loads(response)

        # Binance format: {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}
        # prices and quantities are strings
        bids_raw = data.get("bids", [])
        asks_raw = data.get("asks", [])

        # Parse bids: [[price_str, volume_str], ...]
        bids = []
        bid_vol = 0.0
        for item in bids_raw[:20]:
            if len(item) >= 2:
                price = float(item[0])
                vol = float(item[1])
                bids.append([price, vol])
                bid_vol += vol

        # Parse asks: [[price_str, volume_str], ...]
        asks = []
        ask_vol = 0.0
        for item in asks_raw[:20]:
            if len(item) >= 2:
                price = float(item[0])
                vol = float(item[1])
                asks.append([price, vol])
                ask_vol += vol

        # Calculate spread
        best_bid = bids[0][0] if bids else 0
        best_ask = asks[0][0] if asks else 0
        spread = best_ask - best_bid if best_bid and best_ask else 0
        mid_price = (best_bid + best_ask) / 2 if best_bid and best_ask else 0
        spread_pct = (spread / mid_price * 100) if mid_price else 0

        # Calculate imbalance
        imbalance = bid_vol / ask_vol if ask_vol > 0 else 1.0

        # Calculate quote notionals
        bid_notional = sum(p * v for p, v in bids)
        ask_notional = sum(p * v for p, v in asks)

        # Find thick levels
        thick_bid_level = _find_thick_level(bids, side="bid")
        thick_ask_level = _find_thick_level(asks, side="ask")

        return OrderbookData(
            bids=bids, asks=asks,
            bid_volume=bid_vol, ask_volume=ask_vol,
            imbalance=imbalance, spread=spread, spread_pct=spread_pct,
            thick_bid_level=thick_bid_level, thick_ask_level=thick_ask_level,
            ts=time.time(),
            bid_notional=bid_notional,
            ask_notional=ask_notional,
        )
    except Exception as e:
        log.debug(f"[ORDERBOOK] Parse error for {pair}: {e}")
        return None


def _find_thick_level(orders: list, side: str, top_n: int = 5) -> Optional[float]:
    """Find the price level with thick orders (cumulative volume).
    
    Args:
        orders: List of [price, volume] orders
        side: 'bid' or 'ask'
        top_n: Number of top levels to consider
    
    Returns:
        Price level with thickest cumulative volume, or None
    """
    if not orders:
        return None
    
    # For bids, sort descending by price (already sorted)
    # For asks, sort ascending by price (already sorted)
    levels = orders[:top_n]
    
    if not levels:
        return None
    
    # Find level with maximum cumulative volume up to that level
    if side == "bid":
        # For bids, higher price = stronger support
        thickest_price = max(levels, key=lambda x: x[1])[0]
    else:
        # For asks, lower price = stronger resistance
        thickest_price = min(levels, key=lambda x: x[1])[0]
    
    return thickest_price


def get_orderbook(
    pair: str,
    use_cache: bool = True,
    max_age: float = _ORDERBOOK_TTL,
    allow_stale: bool = False
) -> Optional[OrderbookData]:
    """Get orderbook data for a trading pair.
    
    Args:
        pair: Trading pair (e.g., 'doge', 'btc')
        use_cache: Whether to use cached data if fresh
        max_age: Maximum age in seconds to consider cache fresh (default _ORDERBOOK_TTL = 60)
        allow_stale: If True, return stale cache on fetch failure; if False, return None if stale.
    
    Returns:
        OrderbookData or None on error
    """
    global _orderbook_cache
    
    # Check cache
    if use_cache:
        cached = _orderbook_cache.get(pair)
        if cached and (time.time() - cached.ts) <= max_age:
            return cached
    
    # Fetch fresh
    response = _fetch_orderbook_raw(pair)
    if response is None:
        cached = _orderbook_cache.get(pair)
        if cached and (allow_stale or (time.time() - cached.ts) <= max_age):
            return cached
        return None
    
    ob_data = parse_orderbook(response, pair)
    if ob_data:
        _orderbook_cache[pair] = ob_data
    
    return ob_data


def analyze_orderbook_imbalance(
    pair: str,
    threshold: float = 1.5
) -> Tuple[Optional[OrderbookData], str]:
    """Analyze orderbook imbalance for a pair.
    
    Args:
        pair: Trading pair
        threshold: Imbalance ratio to consider significant (default 1.5)
    
    Returns:
        Tuple of (OrderbookData, signal)
        signal: 'BUY' if bids thicker than asks, 'SELL' if asks thicker, 'NEUTRAL' otherwise
    """
    ob = get_orderbook(pair)
    
    if ob is None:
        return None, "UNKNOWN"
    
    imbalance = ob.imbalance
    
    if imbalance > threshold:
        return ob, "BUY"  # Thick bids = support = potential buy pressure
    elif imbalance < (1.0 / threshold):
        return ob, "SELL"  # Thick asks = resistance = potential sell pressure
    else:
        return ob, "NEUTRAL"


def orderbook_confirms_signal(
    ob: OrderbookData,
    signal_type: str,
    imbalance_threshold: float = 1.3
) -> bool:
    """Check if orderbook imbalance confirms a trading signal.
    
    Args:
        ob: OrderbookData
        signal_type: 'LONG' or 'SHORT'
        imbalance_threshold: Minimum imbalance to confirm
    
    Returns:
        True if orderbook confirms the signal
    """
    if ob is None:
        return False
    
    imbalance = ob.imbalance
    
    if signal_type == "LONG":
        # For LONG: want thick bids (imbalance > threshold)
        return imbalance > imbalance_threshold
    elif signal_type == "SHORT":
        # For SHORT: want thick asks (imbalance < 1/threshold)
        return imbalance < (1.0 / imbalance_threshold)
    
    return False


def format_orderbook_summary(ob: OrderbookData) -> str:
    """Format orderbook data as human-readable summary.
    
    Args:
        ob: OrderbookData
    
    Returns:
        Formatted string summary
    """
    if ob is None:
        return "Orderbook: N/A"
    
    imbalance_status = "BULLISH" if ob.imbalance > 1.3 else ("BEARISH" if ob.imbalance < 0.7 else "NEUTRAL")
    
    summary = [
        f"Bid Vol: {ob.bid_volume:.4f} | Ask Vol: {ob.ask_volume:.4f}",
        f"Imbalance: {ob.imbalance:.2f} ({imbalance_status})",
        f"Spread: {ob.spread:.4f} ({ob.spread_pct:.3f}%)",
    ]
    
    if ob.thick_bid_level:
        summary.append(f"Thick Bid (Support): {ob.thick_bid_level:.4f}")
    if ob.thick_ask_level:
        summary.append(f"Thick Ask (Resistance): {ob.thick_ask_level:.4f}")
    
    return " | ".join(summary)


# ── Async versions for daemon use ──

async def get_orderbook_async(pair: str, use_cache: bool = True) -> Optional[OrderbookData]:
    """Async version of get_orderbook.
    
    Uses asyncio.to_thread to avoid blocking the event loop.
    """
    import asyncio
    
    if use_cache:
        cached = _orderbook_cache.get(pair)
        if cached and (time.time() - cached.ts) < _ORDERBOOK_TTL:
            return cached
    
    # Run in thread pool
    loop = asyncio.get_event_loop()
    ob_data = await loop.run_in_executor(None, get_orderbook, pair, False)
    
    return ob_data
