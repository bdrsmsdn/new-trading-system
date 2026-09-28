"""Ingestion and fee valuation adapters for Binance exchange fills.

Converts raw exchange trade payloads into immutable, validated FillEvents,
computes deterministic commission valuation, and ensures idempotency.
"""

from __future__ import annotations

from decimal import Decimal
import hashlib
import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

log = logging.getLogger(__name__)

from hermes.accounting.contracts import (
    AccountingRepository,
    Completeness,
    DecimalString,
    FillEvent,
    FillKey,
    RealizedOutcome,
    TradeSide,
    ValuationStatus,
    Venue,
    canonical_decimal,
)


def _hash_trade_payload(raw_payload: Mapping[str, Any]) -> str:
    """Compute deterministic SHA-256 hash of trade payload with secrets redacted."""
    sanitized: Dict[str, Any] = {}
    for k, v in sorted(raw_payload.items()):
        # Redact any potential credentials/signatures
        if k.lower() in ("signature", "apikey", "api_key", "secret", "token"):
            continue
        sanitized[k] = v
    serialized = json.dumps(sanitized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def parse_binance_fill(
    raw_trade: Mapping[str, Any],
    account_id: str = "default",
    venue: Venue = Venue.SPOT,
    valuation_map: Optional[Mapping[str, DecimalString]] = None,
) -> FillEvent:
    """Parse a raw Binance trade dictionary into a validated FillEvent.

    Supports Binance /api/v3/myTrades, WebSocket executionReport, and
    normalized trade dictionaries.
    """
    trade_id = str(raw_trade.get("id", raw_trade.get("trade_id", raw_trade.get("t", ""))))
    if not trade_id:
        raise ValueError("Trade payload missing trade ID ('id' or 'trade_id')")

    order_id = str(raw_trade.get("orderId", raw_trade.get("order_id", raw_trade.get("i", ""))))
    if not order_id:
        raise ValueError("Trade payload missing order ID ('orderId' or 'order_id')")

    symbol = str(raw_trade.get("symbol", raw_trade.get("s", ""))).upper()
    if not symbol:
        raise ValueError("Trade payload missing symbol")

    event_time_ms = int(raw_trade.get("time", raw_trade.get("event_time_ms", raw_trade.get("T", 0))))
    if event_time_ms <= 0:
        raise ValueError("Trade payload missing valid timestamp ('time' or 'event_time_ms')")

    # Determine trade side
    if "isBuyer" in raw_trade:
        side = TradeSide.BUY if raw_trade["isBuyer"] else TradeSide.SELL
    elif "side" in raw_trade:
        side_str = str(raw_trade["side"]).upper()
        side = TradeSide.BUY if side_str in ("BUY", "BID") else TradeSide.SELL
    elif "S" in raw_trade:
        side = TradeSide.BUY if str(raw_trade["S"]).upper() == "BUY" else TradeSide.SELL
    else:
        raise ValueError("Trade payload missing trade side ('isBuyer' or 'side')")

    raw_price = str(raw_trade.get("price", raw_trade.get("p", "")))
    price = canonical_decimal(raw_price, non_negative=True)
    if Decimal(price) <= 0:
        raise ValueError("Price must be strictly positive")

    raw_qty = str(raw_trade.get("qty", raw_trade.get("base_qty", raw_trade.get("q", ""))))
    base_qty = canonical_decimal(raw_qty, non_negative=True)
    if Decimal(base_qty) <= 0:
        raise ValueError("Base quantity must be strictly positive")

    # Quote quantity: prefer exchange-reported quoteQty
    if "quoteQty" in raw_trade and str(raw_trade["quoteQty"]).strip():
        quote_qty = canonical_decimal(str(raw_trade["quoteQty"]), non_negative=True)
    elif "quote_qty" in raw_trade and str(raw_trade["quote_qty"]).strip():
        quote_qty = canonical_decimal(str(raw_trade["quote_qty"]), non_negative=True)
    elif "Y" in raw_trade and str(raw_trade["Y"]).strip():
        quote_qty = canonical_decimal(str(raw_trade["Y"]), non_negative=True)
    else:
        computed = Decimal(price) * Decimal(base_qty)
        quote_qty = canonical_decimal(str(computed), non_negative=True)

    if Decimal(quote_qty) <= 0:
        raise ValueError("Quote quantity must be strictly positive")

    # Commission parsing & valuation
    commission_asset = str(
        raw_trade.get("commissionAsset", raw_trade.get("commission_asset", raw_trade.get("N", "")))
    ).upper()
    raw_commission_qty = str(
        raw_trade.get("commission", raw_trade.get("commission_qty", raw_trade.get("n", "0")))
    )
    commission_qty = canonical_decimal(raw_commission_qty if raw_commission_qty else "0", non_negative=True)

    commission_usdt: Optional[DecimalString] = None
    valuation_status = ValuationStatus.PENDING

    comm_dec = Decimal(commission_qty)
    if comm_dec == 0 or not commission_asset:
        commission_usdt = "0"
        valuation_status = ValuationStatus.VALUED
    elif commission_asset in ("USDT", "BUSD", "USD"):
        commission_usdt = commission_qty
        valuation_status = ValuationStatus.VALUED
    elif symbol.endswith("USDT") and commission_asset == symbol[:-4]:
        # Commission paid in base asset (e.g. BTC on BTCUSDT)
        comm_val = comm_dec * Decimal(price)
        commission_usdt = canonical_decimal(str(comm_val), non_negative=True)
        valuation_status = ValuationStatus.VALUED
    elif valuation_map and commission_asset in valuation_map:
        asset_price = Decimal(canonical_decimal(valuation_map[commission_asset], non_negative=True))
        comm_val = comm_dec * asset_price
        commission_usdt = canonical_decimal(str(comm_val), non_negative=True)
        valuation_status = ValuationStatus.VALUED
    else:
        # Valuation is unresolved (e.g. BNB fee without valuation map)
        commission_usdt = None
        valuation_status = ValuationStatus.UNAVAILABLE

    payload_hash = _hash_trade_payload(raw_trade)

    return FillEvent(
        schema_version=1,
        account_id=account_id,
        venue=venue,
        symbol=symbol,
        trade_id=trade_id,
        order_id=order_id,
        event_time_ms=event_time_ms,
        side=side,
        price=price,
        base_qty=base_qty,
        quote_qty=quote_qty,
        commission_asset=commission_asset,
        commission_qty=commission_qty,
        commission_usdt=commission_usdt,
        valuation_status=valuation_status,
        source_payload_hash=payload_hash,
    )


def ingest_binance_trades(
    repo: AccountingRepository,
    trades: Sequence[Mapping[str, Any]],
    account_id: str = "default",
    venue: Venue = Venue.SPOT,
    valuation_map: Optional[Mapping[str, DecimalString]] = None,
) -> Tuple[int, int]:
    """Ingest a sequence of raw trade dictionaries into the accounting repository.

    Returns (inserted_count, duplicate_count).
    """
    inserted = 0
    duplicates = 0
    for trade in trades:
        fill = parse_binance_fill(
            trade,
            account_id=account_id,
            venue=venue,
            valuation_map=valuation_map,
        )
        was_inserted = repo.ingest_fill(fill)
        if was_inserted:
            inserted += 1
        else:
            duplicates += 1
    return inserted, duplicates


def reconcile_and_allocate_fills(
    repo: AccountingRepository,
    fills: Sequence[FillEvent],
) -> List[RealizedOutcome]:
    """Ingest fills in chronological order and allocate realized outcomes for sells."""
    # Sort fills deterministically: event_time_ms, side (BUY before SELL), trade_id
    sorted_fills = sorted(
        fills,
        key=lambda f: (f.event_time_ms, 0 if f.side == TradeSide.BUY else 1, f.trade_id),
    )
    outcomes: List[RealizedOutcome] = []
    for fill in sorted_fills:
        repo.ingest_fill(fill)
        if fill.side == TradeSide.SELL:
            outcome = repo.apply_fifo_sell(fill.key)
            outcomes.append(outcome)
    return outcomes


def fetch_paginated_binance_trades(
    symbol: str,
    limit: int = 1000,
    start_from_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch all paginated trades for a symbol from Binance /api/v3/myTrades."""
    from hermes.api.auth import binance_signed_request
    all_trades: List[Dict[str, Any]] = []
    from_id = start_from_id
    sym = symbol.upper()
    if not any(sym.endswith(base) for base in ("USDT", "BTC", "BNB", "BUSD", "FDUSD", "USDC")):
        sym = f"{sym}USDT"

    while True:
        params: Dict[str, Any] = {"symbol": sym, "limit": limit}
        if from_id is not None:
            params["fromId"] = from_id
        try:
            trades = binance_signed_request("/api/v3/myTrades", params=params, method="GET")
        except Exception:
            break

        if not isinstance(trades, list) or not trades:
            break

        all_trades.extend(trades)
        if len(trades) < limit:
            break

        max_id = max(int(t.get("id", 0)) for t in trades)
        from_id = max_id + 1

    return all_trades


def sync_accounting_trades(
    repo: Optional[AccountingRepository] = None,
    symbols: Optional[Sequence[str]] = None,
    valuation_map: Optional[Mapping[str, DecimalString]] = None,
    account_id: str = "default",
    db_path: Optional[Union[str, Any]] = None,
) -> Tuple[int, int]:
    """Ingest paginated Binance myTrades for bot-owned Spot assets, value fees, and execute FIFO reconciliation.

    Returns (total_inserted, total_duplicates).
    """
    import hermes.config as cfg
    from hermes.config import ACCOUNTING_DB_PATH, ALL_TRACKED
    from hermes.accounting.schema import init_db
    from hermes.accounting.repository import SqliteAccountingRepository
    from hermes.state import state, prices

    if repo is None:
        target_db = db_path or getattr(cfg, "ACCOUNTING_DB_PATH", ACCOUNTING_DB_PATH)
        init_db(target_db)
        repo = SqliteAccountingRepository(target_db)

    target_symbols = set()
    if symbols:
        for s in symbols:
            target_symbols.add(s.upper())
    else:
        for p in ALL_TRACKED:
            target_symbols.add(f"{p.upper()}USDT" if not p.upper().endswith("USDT") else p.upper())
        for p in state.positions.keys():
            sym = p.upper() if p.upper().endswith("USDT") else f"{p.upper()}USDT"
            target_symbols.add(sym)

    val_map: Dict[str, DecimalString] = {}
    if valuation_map:
        val_map.update(valuation_map)
    else:
        for coin, pdata in prices.items():
            p = pdata.get("price")
            if p and p > 0:
                val_map[coin.upper()] = str(p)

    total_inserted = 0
    total_duplicates = 0

    for sym in sorted(target_symbols):
        trades = fetch_paginated_binance_trades(sym)
        if not trades:
            continue

        for trade in trades:
            try:
                fill = parse_binance_fill(
                    trade,
                    account_id=account_id,
                    venue=Venue.SPOT,
                    valuation_map=val_map,
                )
                was_inserted = repo.ingest_fill(fill)
                if was_inserted:
                    total_inserted += 1
                else:
                    total_duplicates += 1

                if fill.side == TradeSide.SELL:
                    repo.apply_fifo_sell(fill.key)
            except Exception as e:
                log.warning(f"[ACCOUNTING-SYNC] Trade ingestion failed for trade {trade.get('id')}: {e}")

    return total_inserted, total_duplicates
