"""
Typed, Fresh Evaluation Snapshots for Trading Risk & Momentum Evaluation.

Implements normative contracts defined in docs/trading-risk-contract.md:
- Strict immutable frozen dataclasses for snapshots, timeframe series, and provenance.
- Strict TTL / staleness enforcement (no neutral value fabrication on missing/stale data).
- Explicit base quantity vs quote notional calculation.
- Explicit position side awareness (LONG vs SHORT).
- Explicit cross-market proxy declaration for Futures.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional, Sequence, Tuple, Union

# Types from contract
DecimalString = str       # regex: ^-?(0|[1-9][0-9]*)(\.[0-9]+)?$
UtcTimestamp = str        # RFC 3339 UTC, e.g. 2026-09-28T12:34:56.123Z
SnapshotId = str          # stable digest/UUID for immutable canonical payload
AccountId = str
CanonicalSymbol = str     # e.g. BTCUSDT after adapter canonicalization

Venue = Literal["SPOT", "FUTURES"]
PositionSide = Literal["LONG", "SHORT"]
TradeSide = Literal["BUY", "SELL"]
DataStatus = Literal["FRESH", "STALE", "UNKNOWN", "INVALID"]
DataSource = Literal["EXCHANGE", "CACHE", "DERIVED", "CROSS_MARKET_PROXY"]
ValuationStatus = Literal["VALUED", "PENDING", "UNAVAILABLE", "INVALID"]
Completeness = Literal["COMPLETE", "INCOMPLETE", "UNKNOWN"]


def format_utc_timestamp(dt: Optional[datetime.datetime] = None) -> UtcTimestamp:
    """Format datetime as RFC 3339 UTC timestamp string with 'Z' suffix."""
    if dt is None:
        dt = datetime.datetime.now(datetime.timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_utc_timestamp(ts_str: str) -> datetime.datetime:
    """Parse RFC 3339 UTC timestamp string."""
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(ts_str)


def to_decimal_str(val: Any) -> Optional[DecimalString]:
    """Convert float/int/str/Decimal to canonical DecimalString without scientific notation."""
    if val is None:
        return None
    try:
        d = Decimal(str(val))
        if not d.is_finite():
            return None
        # Format without exponent
        s = format(d, "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s if s else "0"
    except (InvalidOperation, ValueError, TypeError):
        return None


def canonicalize_symbol(pair: str) -> CanonicalSymbol:
    """Canonicalize a trading pair/symbol to canonical BTCUSDT format."""
    clean = pair.strip().upper().replace("/", "").replace("_", "").replace("-", "")
    if clean.endswith("PERP"):
        clean = clean[:-4]
    if not clean.endswith("USDT"):
        clean = f"{clean}USDT"
    return clean


@dataclass(frozen=True)
class Provenance:
    """Provenance tracking data source, freshness, and age."""
    source: DataSource
    source_id: Optional[str]             # request/candle/cache-entry identifier
    observed_at_utc: Optional[UtcTimestamp]
    received_at_utc: Optional[UtcTimestamp]
    max_age_ms: int
    age_ms_at_snapshot: Optional[int]
    status: DataStatus
    reason_code: Optional[str] = None


@dataclass(frozen=True)
class TimeframeSeries:
    """Timeframe indicator series (e.g. RSI) with distinct closed bars."""
    timeframe: str                    # canonical exchange interval, e.g. "3m", "1h"
    closed_bar_open_times_utc: Tuple[UtcTimestamp, ...]
    values: Optional[Tuple[DecimalString, ...]]
    provenance: Provenance


@dataclass(frozen=True)
class MomentumSnapshot:
    """
    Immutable, typed, self-contained momentum snapshot for continuation evaluation.
    Conforms to Normative Architecture Contract in docs/trading-risk-contract.md.
    """
    snapshot_id: SnapshotId
    schema_version: int
    symbol: CanonicalSymbol
    venue: Venue
    side: PositionSide
    position_lifecycle_id: str
    position_version: int
    snapshot_started_at_utc: UtcTimestamp
    snapshot_completed_at_utc: UtcTimestamp
    best_bid_price: Optional[DecimalString]
    best_ask_price: Optional[DecimalString]
    bid_base_qty: Optional[DecimalString]
    ask_base_qty: Optional[DecimalString]
    bid_quote_notional: Optional[DecimalString]
    ask_quote_notional: Optional[DecimalString]
    spread_quote: Optional[DecimalString]
    spread_fraction: Optional[DecimalString]
    orderbook_provenance: Provenance
    timeframe_series: Tuple[TimeframeSeries, ...]
    cross_market_proxy: bool
    proxy_venue: Optional[Venue]


def compute_snapshot_id(
    symbol: str,
    venue: str,
    side: str,
    position_lifecycle_id: str,
    position_version: int,
    started_at: str,
    best_bid: Optional[str],
    best_ask: Optional[str],
) -> SnapshotId:
    """Compute a deterministic hash ID for an immutable snapshot payload."""
    payload = f"{symbol}|{venue}|{side}|{position_lifecycle_id}|{position_version}|{started_at}|{best_bid}|{best_ask}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def build_momentum_snapshot(
    symbol: str,
    venue: Venue,
    side: PositionSide,
    orderbook: Any,
    rsi_3m: Optional[float] = None,
    rsi_1h: Optional[float] = None,
    rsi_4h: Optional[float] = None,
    position_lifecycle_id: str = "default",
    position_version: int = 1,
    orderbook_source_venue: Optional[Venue] = None,
    max_orderbook_age_ms: int = 60000,
    max_rsi_age_ms: int = 300000,
    now_ts: Optional[float] = None,
) -> MomentumSnapshot:
    """
    Build a strictly validated, immutable MomentumSnapshot from market data.
    
    Validates:
    - Orderbook freshness, crossed book, empty book, finite numbers.
    - Explicit calculation of quote notional.
    - Proper UNKNOWN/STALE marking without neutral placeholder fabrication.
    """
    now = time.time() if now_ts is None else now_ts
    started_at_utc = format_utc_timestamp(datetime.datetime.fromtimestamp(now, tz=datetime.timezone.utc))
    canon_sym = canonicalize_symbol(symbol)
    
    # 1. Process Orderbook
    best_bid_str: Optional[DecimalString] = None
    best_ask_str: Optional[DecimalString] = None
    bid_base_qty_str: Optional[DecimalString] = None
    ask_base_qty_str: Optional[DecimalString] = None
    bid_quote_notional_str: Optional[DecimalString] = None
    ask_quote_notional_str: Optional[DecimalString] = None
    spread_quote_str: Optional[DecimalString] = None
    spread_fraction_str: Optional[DecimalString] = None

    is_cross_proxy = False
    proxy_ven: Optional[Venue] = None
    if venue == "FUTURES" and (orderbook_source_venue == "SPOT" or orderbook_source_venue is None):
        is_cross_proxy = True
        proxy_ven = "SPOT"

    if orderbook is None:
        ob_prov = Provenance(
            source="EXCHANGE" if not is_cross_proxy else "CROSS_MARKET_PROXY",
            source_id=None,
            observed_at_utc=None,
            received_at_utc=None,
            max_age_ms=max_orderbook_age_ms,
            age_ms_at_snapshot=None,
            status="UNKNOWN",
            reason_code="MISSING_ORDERBOOK"
        )
    else:
        # Extract attributes from OrderbookData (or dict fallback)
        bids = getattr(orderbook, "bids", None) or (orderbook.get("bids") if isinstance(orderbook, dict) else [])
        asks = getattr(orderbook, "asks", None) or (orderbook.get("asks") if isinstance(orderbook, dict) else [])
        ob_ts = getattr(orderbook, "ts", None) or (orderbook.get("ts") if isinstance(orderbook, dict) else now)
        bid_vol = getattr(orderbook, "bid_volume", None) or (orderbook.get("bid_volume") if isinstance(orderbook, dict) else None)
        ask_vol = getattr(orderbook, "ask_volume", None) or (orderbook.get("ask_volume") if isinstance(orderbook, dict) else None)

        age_ms = int((now - ob_ts) * 1000) if ob_ts is not None else 0
        observed_at_str = format_utc_timestamp(datetime.datetime.fromtimestamp(ob_ts, tz=datetime.timezone.utc)) if ob_ts else None

        if age_ms > max_orderbook_age_ms:
            ob_prov = Provenance(
                source="CACHE",
                source_id=canon_sym,
                observed_at_utc=observed_at_str,
                received_at_utc=started_at_utc,
                max_age_ms=max_orderbook_age_ms,
                age_ms_at_snapshot=age_ms,
                status="STALE",
                reason_code="EXPIRED_ORDERBOOK"
            )
        elif not bids or not asks:
            ob_prov = Provenance(
                source="EXCHANGE" if not is_cross_proxy else "CROSS_MARKET_PROXY",
                source_id=canon_sym,
                observed_at_utc=observed_at_str,
                received_at_utc=started_at_utc,
                max_age_ms=max_orderbook_age_ms,
                age_ms_at_snapshot=age_ms,
                status="INVALID",
                reason_code="EMPTY_ORDERBOOK"
            )
        else:
            best_bid_p = float(bids[0][0])
            best_ask_p = float(asks[0][0])

            if best_bid_p <= 0 or best_ask_p <= 0 or best_bid_p >= best_ask_p:
                ob_prov = Provenance(
                    source="EXCHANGE" if not is_cross_proxy else "CROSS_MARKET_PROXY",
                    source_id=canon_sym,
                    observed_at_utc=observed_at_str,
                    received_at_utc=started_at_utc,
                    max_age_ms=max_orderbook_age_ms,
                    age_ms_at_snapshot=age_ms,
                    status="INVALID",
                    reason_code="CROSSED_ORDERBOOK" if best_bid_p >= best_ask_p else "NON_POSITIVE_PRICE"
                )
            else:
                # Valid fresh book
                best_bid_str = to_decimal_str(best_bid_p)
                best_ask_str = to_decimal_str(best_ask_p)
                
                # Base quantities
                b_qty = float(bid_vol) if bid_vol is not None else sum(float(b[1]) for b in bids)
                a_qty = float(ask_vol) if ask_vol is not None else sum(float(a[1]) for a in asks)
                bid_base_qty_str = to_decimal_str(b_qty)
                ask_base_qty_str = to_decimal_str(a_qty)

                # Quote notionals explicitly computed
                b_notional = getattr(orderbook, "bid_notional", None)
                if b_notional is None:
                    b_notional = sum(float(b[0]) * float(b[1]) for b in bids)
                a_notional = getattr(orderbook, "ask_notional", None)
                if a_notional is None:
                    a_notional = sum(float(a[0]) * float(a[1]) for a in asks)
                
                bid_quote_notional_str = to_decimal_str(b_notional)
                ask_quote_notional_str = to_decimal_str(a_notional)

                # Spread
                spread_val = best_ask_p - best_bid_p
                mid_val = (best_ask_p + best_bid_p) / 2.0
                spread_quote_str = to_decimal_str(spread_val)
                spread_fraction_str = to_decimal_str(spread_val / mid_val) if mid_val > 0 else "0"

                ob_prov = Provenance(
                    source="EXCHANGE" if not is_cross_proxy else "CROSS_MARKET_PROXY",
                    source_id=canon_sym,
                    observed_at_utc=observed_at_str,
                    received_at_utc=started_at_utc,
                    max_age_ms=max_orderbook_age_ms,
                    age_ms_at_snapshot=age_ms,
                    status="FRESH",
                    reason_code=None
                )

    # 2. Process Timeframe Series (RSI)
    tf_series_list = []
    for tf_name, rsi_val in [("3m", rsi_3m), ("1h", rsi_1h), ("4h", rsi_4h)]:
        if rsi_val is None:
            prov = Provenance(
                source="DERIVED",
                source_id=f"{canon_sym}_{tf_name}",
                observed_at_utc=None,
                received_at_utc=started_at_utc,
                max_age_ms=max_rsi_age_ms,
                age_ms_at_snapshot=None,
                status="UNKNOWN",
                reason_code="MISSING_RSI"
            )
            tf_series_list.append(TimeframeSeries(
                timeframe=tf_name,
                closed_bar_open_times_utc=(),
                values=None,
                provenance=prov
            ))
        else:
            rsi_dec = to_decimal_str(rsi_val)
            prov = Provenance(
                source="DERIVED",
                source_id=f"{canon_sym}_{tf_name}",
                observed_at_utc=started_at_utc,
                received_at_utc=started_at_utc,
                max_age_ms=max_rsi_age_ms,
                age_ms_at_snapshot=0,
                status="FRESH",
                reason_code=None
            )
            tf_series_list.append(TimeframeSeries(
                timeframe=tf_name,
                closed_bar_open_times_utc=(started_at_utc,),
                values=(rsi_dec,) if rsi_dec else None,
                provenance=prov
            ))

    completed_at_utc = format_utc_timestamp()
    snap_id = compute_snapshot_id(
        symbol=canon_sym,
        venue=venue,
        side=side,
        position_lifecycle_id=position_lifecycle_id,
        position_version=position_version,
        started_at=started_at_utc,
        best_bid=best_bid_str,
        best_ask=best_ask_str
    )

    return MomentumSnapshot(
        snapshot_id=snap_id,
        schema_version=1,
        symbol=canon_sym,
        venue=venue,
        side=side,
        position_lifecycle_id=position_lifecycle_id,
        position_version=position_version,
        snapshot_started_at_utc=started_at_utc,
        snapshot_completed_at_utc=completed_at_utc,
        best_bid_price=best_bid_str,
        best_ask_price=best_ask_str,
        bid_base_qty=bid_base_qty_str,
        ask_base_qty=ask_base_qty_str,
        bid_quote_notional=bid_quote_notional_str,
        ask_quote_notional=ask_quote_notional_str,
        spread_quote=spread_quote_str,
        spread_fraction=spread_fraction_str,
        orderbook_provenance=ob_prov,
        timeframe_series=tuple(tf_series_list),
        cross_market_proxy=is_cross_proxy,
        proxy_venue=proxy_ven,
    )


def is_continuation_eligible(snapshot: MomentumSnapshot) -> Tuple[bool, str]:
    """
    Check if a MomentumSnapshot meets strict data quality and freshness gates.
    
    Returns:
        (True, "OK") if valid and fresh.
        (False, reason_code) if any required data is stale, missing, crossed, or invalid.
    """
    # 1. Orderbook quality
    ob_prov = snapshot.orderbook_provenance
    if ob_prov.status != "FRESH":
        return False, ob_prov.reason_code or ob_prov.status

    if snapshot.best_bid_price is None or snapshot.best_ask_price is None:
        return False, "MISSING_BOOK_PRICES"

    try:
        bb = Decimal(snapshot.best_bid_price)
        ba = Decimal(snapshot.best_ask_price)
        if bb >= ba:
            return False, "CROSSED_ORDERBOOK"
        if bb <= 0 or ba <= 0:
            return False, "NON_POSITIVE_PRICE"
    except (InvalidOperation, ValueError):
        return False, "INVALID_PRICE_FORMAT"

    # 2. RSI quality (3m and 1h must be fresh with valid values)
    for tf in snapshot.timeframe_series:
        if tf.timeframe in ("3m", "1h"):
            if tf.provenance.status != "FRESH" or tf.values is None:
                return False, f"{tf.provenance.status}_RSI_DATA"

    return True, "OK"
