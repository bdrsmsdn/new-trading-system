"""Durable SQLite repositories for Phase 2 accounting and rotation.

Implements:
- AccountingRepository (Fill ingestion, FIFO lot allocation, immutable snapshots)
- TransferIntentRepository (Atomic daily claim, CAS transitions, unresolved listing)
- RotationRepository (Decision append, atomic intent claim, CAS transitions)
"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, List, Mapping, Optional, Sequence, Tuple, Union
import uuid

from hermes.accounting.contracts import (
    AccountingCutover,
    AccountingRepository,
    AccountingSnapshot,
    Completeness,
    CutoverStatus,
    DistributionDecision,
    FillEvent,
    FillKey,
    Lot,
    LotAllocation,
    RealizedOutcome,
    ReconciliationStatus,
    RotationAction,
    RotationDecision,
    RotationIntent,
    RotationRepository,
    RotationStatus,
    TradeSide,
    TransferIntent,
    TransferIntentRepository,
    TransferStatus,
    ValuationStatus,
    Venue,
    canonical_decimal,
)
from hermes.accounting.schema import get_db_connection, init_db


class LedgerError(Exception):
    """Base exception for ledger operations."""
    pass


class DuplicateFillConflictError(LedgerError):
    """Raised when a fill has an existing key but conflicting payload hash."""
    pass


class ConcurrencyConflictError(LedgerError):
    """Raised when a compare-and-set lifecycle transition fails due to state mismatch."""
    pass


class ClaimConflictError(LedgerError):
    """Raised when an atomic intent claim conflicts with an existing active intent."""
    pass


def _generate_id(prefix: str) -> str:
    """Generate a collision-resistant unique identifier."""
    return f"{prefix}_{uuid.uuid4().hex}"


class SqliteAccountingRepository:
    """SQLite implementation of AccountingRepository protocol."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = str(db_path)

    def _con(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path)

    def ingest_fill(self, fill: FillEvent) -> bool:
        """Insert an unseen fill, return False for an identical duplicate.

        If a conflicting payload hash is detected for the same key, quarantines
        the fill, records a reconciliation blocker, and raises DuplicateFillConflictError.
        """
        # Validate canonical decimal representations
        canonical_decimal(fill.price, non_negative=True)
        canonical_decimal(fill.base_qty, non_negative=True)
        canonical_decimal(fill.quote_qty, non_negative=True)
        canonical_decimal(fill.commission_qty, non_negative=True)
        if fill.commission_usdt is not None:
            canonical_decimal(fill.commission_usdt, non_negative=True)

        con = self._con()
        try:
            con.execute("BEGIN IMMEDIATE;")
            cur = con.execute(
                """
                SELECT source_payload_hash, is_quarantined
                FROM fills
                WHERE account_id = ? AND venue = ? AND symbol = ? AND trade_id = ?;
                """,
                (fill.account_id, fill.venue.value, fill.symbol, fill.trade_id),
            )
            row = cur.fetchone()

            if row:
                existing_hash = row["source_payload_hash"]
                if existing_hash == fill.source_payload_hash:
                    # Identical duplicate is a no-op
                    con.execute("COMMIT;")
                    return False
                else:
                    # Conflicting duplicate! Quarantine and record blocker
                    now_ms = int(time.time() * 1000)
                    con.execute(
                        """
                        UPDATE fills
                        SET is_quarantined = 1, quarantine_reason = 'CONFLICTING_PAYLOAD_HASH'
                        WHERE account_id = ? AND venue = ? AND symbol = ? AND trade_id = ?;
                        """,
                        (fill.account_id, fill.venue.value, fill.symbol, fill.trade_id),
                    )
                    blocker_id = _generate_id("blk")
                    con.execute(
                        """
                        INSERT INTO reconciliation_blockers (
                            blocker_id, account_id, blocker_type, reference_id,
                            reason_code, details_json, is_active, created_at_ms
                        ) VALUES (?, ?, 'CONFLICTING_FILL', ?, 'CONFLICTING_PAYLOAD_HASH', ?, 1, ?);
                        """,
                        (
                            blocker_id,
                            fill.account_id,
                            f"{fill.symbol}:{fill.trade_id}",
                            json.dumps({
                                "existing_hash": existing_hash,
                                "incoming_hash": fill.source_payload_hash,
                            }),
                            now_ms,
                        ),
                    )
                    con.execute("COMMIT;")
                    raise DuplicateFillConflictError(
                        f"Fill {fill.key} has conflicting payload hash: {existing_hash} vs {fill.source_payload_hash}"
                    )

            # Insert new fill
            now_ms = int(time.time() * 1000)
            con.execute(
                """
                INSERT INTO fills (
                    schema_version, account_id, venue, symbol, trade_id, order_id,
                    event_time_ms, side, price, base_qty, quote_qty, commission_asset,
                    commission_qty, commission_usdt, valuation_status, source_payload_hash,
                    is_quarantined, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?);
                """,
                (
                    fill.schema_version,
                    fill.account_id,
                    fill.venue.value,
                    fill.symbol,
                    fill.trade_id,
                    fill.order_id,
                    fill.event_time_ms,
                    fill.side.value,
                    fill.price,
                    fill.base_qty,
                    fill.quote_qty,
                    fill.commission_asset,
                    fill.commission_qty,
                    fill.commission_usdt,
                    fill.valuation_status.value,
                    fill.source_payload_hash,
                    now_ms,
                ),
            )

            # If BUY fill, create initial lot
            if fill.side == TradeSide.BUY:
                lot_id = _generate_id("lot")
                completeness = (
                    Completeness.VERIFIED.value
                    if fill.valuation_status == ValuationStatus.VALUED
                    else Completeness.UNRESOLVED.value
                )
                con.execute(
                    """
                    INSERT INTO lots (
                        schema_version, lot_id, account_id, venue, symbol,
                        acquired_trade_id, opened_at_ms, original_base_qty,
                        remaining_base_qty, quote_cost_usdt, allocated_buy_fee_usdt,
                        completeness, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        fill.schema_version,
                        lot_id,
                        fill.account_id,
                        fill.venue.value,
                        fill.symbol,
                        fill.trade_id,
                        fill.event_time_ms,
                        fill.base_qty,
                        fill.base_qty,
                        fill.quote_qty,
                        fill.commission_usdt,
                        completeness,
                        now_ms,
                    ),
                )

            con.execute("COMMIT;")
            return True
        except Exception:
            try:
                con.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            con.close()

    def apply_fifo_sell(self, sell_key: FillKey) -> RealizedOutcome:
        """Allocate a sell and persist its outcome in one transaction."""
        con = self._con()
        try:
            con.execute("BEGIN IMMEDIATE;")

            # 1. Fetch sell fill
            cur = con.execute(
                """
                SELECT * FROM fills
                WHERE account_id = ? AND venue = ? AND symbol = ? AND trade_id = ?;
                """,
                (sell_key.account_id, sell_key.venue.value, sell_key.symbol, sell_key.trade_id),
            )
            sell_row = cur.fetchone()
            if not sell_row:
                raise ValueError(f"Sell fill not found: {sell_key}")
            if sell_row["side"] != TradeSide.SELL.value:
                raise ValueError(f"Fill {sell_key} is not a SELL")

            # Check if outcome already exists
            cur = con.execute(
                """
                SELECT * FROM realized_outcomes
                WHERE account_id = ? AND venue = ? AND symbol = ? AND sell_trade_id = ?;
                """,
                (sell_key.account_id, sell_key.venue.value, sell_key.symbol, sell_key.trade_id),
            )
            existing_outcome = cur.fetchone()
            if existing_outcome:
                # Return existing outcome with its allocations
                alloc_cur = con.execute(
                    "SELECT * FROM lot_allocations WHERE outcome_id = ? ORDER BY allocation_id ASC;",
                    (existing_outcome["outcome_id"],),
                )
                allocations: List[LotAllocation] = []
                for a in alloc_cur.fetchall():
                    allocations.append(
                        LotAllocation(
                            allocation_id=a["allocation_id"],
                            outcome_id=a["outcome_id"],
                            lot_id=a["lot_id"],
                            opening_fill_key=FillKey(
                                a["opening_account_id"],
                                Venue(a["opening_venue"]),
                                a["opening_symbol"],
                                a["opening_trade_id"],
                            ),
                            closing_fill_key=FillKey(
                                a["closing_account_id"],
                                Venue(a["closing_venue"]),
                                a["closing_symbol"],
                                a["closing_trade_id"],
                            ),
                            allocated_base_qty=a["allocated_base_qty"],
                            allocated_cost_usdt=a["allocated_cost_usdt"],
                            allocated_buy_fee_usdt=a["allocated_buy_fee_usdt"],
                            allocated_sell_fee_usdt=a["allocated_sell_fee_usdt"],
                        )
                    )
                existing_reasons: Tuple[str, ...] = tuple(
                    str(x) for x in json.loads(existing_outcome["incomplete_reason_codes_json"])
                )
                con.execute("COMMIT;")
                return RealizedOutcome(
                    schema_version=existing_outcome["schema_version"],
                    outcome_id=existing_outcome["outcome_id"],
                    account_id=existing_outcome["account_id"],
                    venue=Venue(existing_outcome["venue"]),
                    symbol=existing_outcome["symbol"],
                    sell_fill_key=sell_key,
                    sold_base_qty=existing_outcome["sold_base_qty"],
                    gross_proceeds_usdt=existing_outcome["gross_proceeds_usdt"],
                    fifo_cost_usdt=existing_outcome["fifo_cost_usdt"],
                    buy_fee_usdt=existing_outcome["buy_fee_usdt"],
                    sell_fee_usdt=existing_outcome["sell_fee_usdt"],
                    net_pnl_usdt=existing_outcome["net_pnl_usdt"],
                    completeness=Completeness(existing_outcome["completeness"]),
                    reporting_day_utc=existing_outcome["reporting_day_utc"],
                    incomplete_reason_codes=existing_reasons,
                    allocations=tuple(allocations),
                )

            # 2. Query available FIFO lots
            cur = con.execute(
                """
                SELECT * FROM lots
                WHERE account_id = ? AND venue = ? AND symbol = ? AND remaining_base_qty != '0'
                ORDER BY opened_at_ms ASC, acquired_trade_id ASC, lot_id ASC;
                """,
                (sell_key.account_id, sell_key.venue.value, sell_key.symbol),
            )
            lots_rows = cur.fetchall()

            sold_qty = Decimal(sell_row["base_qty"])
            remaining_to_allocate = sold_qty
            total_fifo_cost = Decimal("0")
            total_buy_fee = Decimal("0")
            has_unresolved_fee = False
            allocations_to_save: List[dict] = []
            now_ms = int(time.time() * 1000)
            outcome_id = _generate_id("out")

            for lot in lots_rows:
                lot_remaining = Decimal(lot["remaining_base_qty"])
                if lot_remaining <= 0:
                    continue
                lot_original = Decimal(lot["original_base_qty"])
                lot_cost = Decimal(lot["quote_cost_usdt"])

                alloc_qty = min(remaining_to_allocate, lot_remaining)
                fraction = alloc_qty / lot_original
                alloc_cost = fraction * lot_cost
                total_fifo_cost += alloc_cost

                if lot["allocated_buy_fee_usdt"] is not None:
                    alloc_buy_fee = fraction * Decimal(lot["allocated_buy_fee_usdt"])
                    total_buy_fee += alloc_buy_fee
                    alloc_buy_fee_str: Optional[str] = canonical_decimal(str(alloc_buy_fee))
                else:
                    alloc_buy_fee_str = None
                    has_unresolved_fee = True

                new_lot_remaining = lot_remaining - alloc_qty
                con.execute(
                    "UPDATE lots SET remaining_base_qty = ? WHERE lot_id = ?;",
                    (canonical_decimal(str(new_lot_remaining)), lot["lot_id"]),
                )

                alloc_id = _generate_id("alloc")
                allocations_to_save.append({
                    "allocation_id": alloc_id,
                    "outcome_id": outcome_id,
                    "lot_id": lot["lot_id"],
                    "opening_account_id": lot["account_id"],
                    "opening_venue": lot["venue"],
                    "opening_symbol": lot["symbol"],
                    "opening_trade_id": lot["acquired_trade_id"],
                    "closing_account_id": sell_row["account_id"],
                    "closing_venue": sell_row["venue"],
                    "closing_symbol": sell_row["symbol"],
                    "closing_trade_id": sell_row["trade_id"],
                    "allocated_base_qty": canonical_decimal(str(alloc_qty)),
                    "allocated_cost_usdt": canonical_decimal(str(alloc_cost)),
                    "allocated_buy_fee_usdt": alloc_buy_fee_str,
                    "allocated_sell_fee_usdt": None,  # Computed after loop if valued
                })

                remaining_to_allocate -= alloc_qty
                if remaining_to_allocate <= 0:
                    break

            # 3. Determine completeness and net PnL
            reasons: List[str] = []
            sell_fee_str: Optional[str] = None
            sell_fee = Decimal("0")
            if sell_row["commission_usdt"] is not None:
                sell_fee = Decimal(sell_row["commission_usdt"])
                sell_fee_str = canonical_decimal(str(sell_fee))
            elif sell_row["valuation_status"] != ValuationStatus.VALUED.value:
                has_unresolved_fee = True

            gross_proceeds = Decimal(sell_row["quote_qty"])

            if remaining_to_allocate > 0:
                completeness = Completeness.UNRESOLVED
                reasons.append("UNRESOLVED_LOT_SHORTAGE")
                net_pnl_str = None
                buy_fee_str = None
            elif has_unresolved_fee or sell_row["valuation_status"] != ValuationStatus.VALUED.value:
                completeness = Completeness.UNRESOLVED
                reasons.append("UNRESOLVED_FEE_VALUATION")
                net_pnl_str = None
                buy_fee_str = None
            else:
                completeness = Completeness.VERIFIED
                net_pnl = gross_proceeds - total_fifo_cost - total_buy_fee - sell_fee
                net_pnl_str = canonical_decimal(str(net_pnl))
                buy_fee_str = canonical_decimal(str(total_buy_fee))

            # Distribute sell fee across allocations proportionally
            if sell_fee_str is not None and sold_qty > 0:
                for alloc in allocations_to_save:
                    alloc_base = Decimal(alloc["allocated_base_qty"])
                    alloc_sf = (alloc_base / sold_qty) * sell_fee
                    alloc["allocated_sell_fee_usdt"] = canonical_decimal(str(alloc_sf))

            # UTC reporting day: YYYY-MM-DD
            event_sec = sell_row["event_time_ms"] / 1000.0
            gm = time.gmtime(event_sec)
            reporting_day_utc = time.strftime("%Y-%m-%d", gm)

            # Insert allocations
            for alloc in allocations_to_save:
                con.execute(
                    """
                    INSERT INTO lot_allocations (
                        allocation_id, outcome_id, lot_id, opening_account_id,
                        opening_venue, opening_symbol, opening_trade_id, closing_account_id,
                        closing_venue, closing_symbol, closing_trade_id, allocated_base_qty,
                        allocated_cost_usdt, allocated_buy_fee_usdt, allocated_sell_fee_usdt,
                        created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        alloc["allocation_id"],
                        alloc["outcome_id"],
                        alloc["lot_id"],
                        alloc["opening_account_id"],
                        alloc["opening_venue"],
                        alloc["opening_symbol"],
                        alloc["opening_trade_id"],
                        alloc["closing_account_id"],
                        alloc["closing_venue"],
                        alloc["closing_symbol"],
                        alloc["closing_trade_id"],
                        alloc["allocated_base_qty"],
                        alloc["allocated_cost_usdt"],
                        alloc["allocated_buy_fee_usdt"],
                        alloc["allocated_sell_fee_usdt"],
                        now_ms,
                    ),
                )

            # Insert realized outcome
            con.execute(
                """
                INSERT INTO realized_outcomes (
                    schema_version, outcome_id, account_id, venue, symbol,
                    sell_trade_id, sold_base_qty, gross_proceeds_usdt,
                    fifo_cost_usdt, buy_fee_usdt, sell_fee_usdt, net_pnl_usdt,
                    completeness, reporting_day_utc, incomplete_reason_codes_json,
                    created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    sell_row["schema_version"],
                    outcome_id,
                    sell_row["account_id"],
                    sell_row["venue"],
                    sell_row["symbol"],
                    sell_row["trade_id"],
                    canonical_decimal(str(sold_qty)),
                    canonical_decimal(str(gross_proceeds)),
                    canonical_decimal(str(total_fifo_cost)),
                    buy_fee_str,
                    sell_fee_str,
                    net_pnl_str,
                    completeness.value,
                    reporting_day_utc,
                    json.dumps(reasons),
                    now_ms,
                ),
            )

            con.execute("COMMIT;")

            built_allocations = [
                LotAllocation(
                    allocation_id=a["allocation_id"],
                    outcome_id=a["outcome_id"],
                    lot_id=a["lot_id"],
                    opening_fill_key=FillKey(
                        a["opening_account_id"],
                        Venue(a["opening_venue"]),
                        a["opening_symbol"],
                        a["opening_trade_id"],
                    ),
                    closing_fill_key=FillKey(
                        a["closing_account_id"],
                        Venue(a["closing_venue"]),
                        a["closing_symbol"],
                        a["closing_trade_id"],
                    ),
                    allocated_base_qty=a["allocated_base_qty"],
                    allocated_cost_usdt=a["allocated_cost_usdt"],
                    allocated_buy_fee_usdt=a["allocated_buy_fee_usdt"],
                    allocated_sell_fee_usdt=a["allocated_sell_fee_usdt"],
                )
                for a in allocations_to_save
            ]

            return RealizedOutcome(
                schema_version=sell_row["schema_version"],
                outcome_id=outcome_id,
                account_id=sell_row["account_id"],
                venue=Venue(sell_row["venue"]),
                symbol=sell_row["symbol"],
                sell_fill_key=sell_key,
                sold_base_qty=canonical_decimal(str(sold_qty)),
                gross_proceeds_usdt=canonical_decimal(str(gross_proceeds)),
                fifo_cost_usdt=canonical_decimal(str(total_fifo_cost)),
                buy_fee_usdt=buy_fee_str,
                sell_fee_usdt=sell_fee_str,
                net_pnl_usdt=net_pnl_str,
                completeness=completeness,
                reporting_day_utc=reporting_day_utc,
                incomplete_reason_codes=tuple(reasons),
                allocations=tuple(built_allocations),
            )
        except Exception:
            try:
                con.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            con.close()

    def distribution_snapshot(self, account_id: str, observed_at_ms: int) -> AccountingSnapshot:
        """Return one immutable, revisioned policy snapshot."""
        con = self._con()
        try:
            # 1. Fetch cutover
            cur = con.execute(
                """
                SELECT * FROM accounting_cutovers
                WHERE account_id = ? AND venue = 'SPOT'
                ORDER BY created_at_ms DESC LIMIT 1;
                """,
                (account_id,),
            )
            cutover_row = cur.fetchone()
            cutover_id = cutover_row["cutover_id"] if cutover_row else None
            cutover_status = CutoverStatus(cutover_row["status"]) if cutover_row else CutoverStatus.PENDING
            cutover_at_ms = cutover_row["cutover_at_ms"] if cutover_row else None

            # 2. Sum cumulative verified net realized PnL post-cutover
            pnl_query = """
                SELECT net_pnl_usdt, completeness FROM realized_outcomes
                WHERE account_id = ?
            """
            params: List[Any] = [account_id]
            if cutover_at_ms is not None:
                pnl_query += " AND created_at_ms >= ?"
                params.append(cutover_at_ms)

            cur = con.execute(pnl_query, params)
            outcomes = cur.fetchall()

            cum_pnl = Decimal("0")
            has_unresolved_outcome = False
            for r in outcomes:
                if r["completeness"] == Completeness.VERIFIED.value and r["net_pnl_usdt"] is not None:
                    cum_pnl += Decimal(r["net_pnl_usdt"])
                else:
                    has_unresolved_outcome = True

            # 3. Sum confirmed distributions post-cutover
            dist_query = """
                SELECT amount_usdt FROM transfer_intents
                WHERE account_id = ? AND status = 'CONFIRMED'
            """
            dist_params: List[Any] = [account_id]
            if cutover_at_ms is not None:
                dist_query += " AND created_at_ms >= ?"
                dist_params.append(cutover_at_ms)

            cur = con.execute(dist_query, dist_params)
            cum_dist = Decimal("0")
            for r in cur.fetchall():
                cum_dist += Decimal(r["amount_usdt"])

            # 4. Check for in-flight/unknown transfers
            cur = con.execute(
                """
                SELECT status FROM transfer_intents
                WHERE account_id = ? AND status IN ('SUBMITTING', 'UNKNOWN');
                """,
                (account_id,),
            )
            transfer_statuses = [r["status"] for r in cur.fetchall()]
            has_submitting = "SUBMITTING" in transfer_statuses
            has_unknown = "UNKNOWN" in transfer_statuses

            # 5. Check active reconciliation blockers
            cur = con.execute(
                """
                SELECT reason_code FROM reconciliation_blockers
                WHERE account_id = ? AND is_active = 1;
                """,
                (account_id,),
            )
            blocker_codes = [r["reason_code"] for r in cur.fetchall()]

            # 6. Evaluate completeness and reason codes
            reasons: List[str] = []
            if cutover_status != CutoverStatus.APPROVED:
                reasons.append("CUTOVER_NOT_APPROVED")
            if has_unresolved_outcome:
                reasons.append("ACCOUNTING_INCOMPLETE")
            if has_submitting:
                reasons.append("UNKNOWN_TRANSFER")
            if has_unknown:
                reasons.append("UNKNOWN_TRANSFER")
            for b in blocker_codes:
                if b not in reasons:
                    reasons.append(b)

            if not reasons and cutover_status == CutoverStatus.APPROVED:
                completeness = Completeness.VERIFIED
                reconciliation_status = ReconciliationStatus.RECONCILED
            elif blocker_codes or cutover_status == CutoverStatus.REJECTED:
                completeness = Completeness.UNRESOLVED
                reconciliation_status = ReconciliationStatus.MISMATCH
            else:
                completeness = Completeness.PARTIAL
                reconciliation_status = ReconciliationStatus.PENDING

            surplus = cum_pnl - cum_dist

            # Revision from data_version
            dv_cur = con.execute("PRAGMA data_version;")
            data_ver_row = dv_cur.fetchone()
            revision = data_ver_row[0] if data_ver_row else 1

            snapshot_id = _generate_id("snap")
            return AccountingSnapshot(
                snapshot_id=snapshot_id,
                ledger_revision=revision,
                account_id=account_id,
                cutover_id=cutover_id,
                cutover_status=cutover_status,
                cutover_at_ms=cutover_at_ms,
                cumulative_verified_net_pnl_usdt=canonical_decimal(str(cum_pnl)),
                cumulative_confirmed_distributions_usdt=canonical_decimal(str(cum_dist)),
                distribution_surplus_usdt=canonical_decimal(str(surplus)),
                completeness=completeness,
                reconciliation_status=reconciliation_status,
                unresolved_reason_codes=tuple(reasons),
                has_submitting_transfer=has_submitting,
                has_unknown_transfer=has_unknown,
                observed_at_ms=observed_at_ms,
            )
        finally:
            con.close()


class SqliteTransferIntentRepository:
    """SQLite implementation of TransferIntentRepository protocol."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = str(db_path)

    def _con(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path)

    def claim_daily_intent(
        self,
        decision: DistributionDecision,
        client_transfer_id: str,
        account_id: str = "default",
    ) -> TransferIntent:
        """Atomically create or return the unique daily policy intent.

        Enforces uniqueness on (account_id, policy_version, reporting_day_utc)
        and client_transfer_id.
        """
        con = self._con()
        try:
            con.execute("BEGIN IMMEDIATE;")

            # Check if intent exists for this policy day
            cur = con.execute(
                """
                SELECT * FROM transfer_intents
                WHERE account_id = ? AND policy_version = ? AND reporting_day_utc = ?;
                """,
                (account_id, decision.policy_version, decision.reporting_day_utc),
            )
            row = cur.fetchone()
            if row:
                con.execute("COMMIT;")
                return self._row_to_intent(row)

            # Check if client_transfer_id already used
            cur = con.execute(
                "SELECT * FROM transfer_intents WHERE client_transfer_id = ?;",
                (client_transfer_id,),
            )
            row = cur.fetchone()
            if row:
                con.execute("COMMIT;")
                return self._row_to_intent(row)

            now_ms = int(time.time() * 1000)
            intent_id = _generate_id("xfer")
            amount_usdt = decision.amount_usdt or "1"
            canonical_decimal(amount_usdt, non_negative=True)

            snapshot_json = json.dumps({
                "decision_id": decision.decision_id,
                "ledger_snapshot_id": decision.ledger_snapshot_id,
                "ledger_revision": decision.ledger_revision,
                "distribution_surplus_usdt": decision.distribution_surplus_usdt,
                "required_reserve_usdt": decision.required_reserve_usdt,
                "reason_codes": decision.reason_codes,
                "decided_at_ms": decision.decided_at_ms,
            })

            con.execute(
                """
                INSERT INTO transfer_intents (
                    schema_version, intent_id, account_id, policy_version,
                    reporting_day_utc, client_transfer_id, amount_usdt,
                    policy_snapshot_json, status, exchange_tran_id,
                    created_at_ms, updated_at_ms, confirmed_at_ms, last_error_code
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, 'PLANNED', NULL, ?, ?, NULL, NULL);
                """,
                (
                    intent_id,
                    account_id,
                    decision.policy_version,
                    decision.reporting_day_utc,
                    client_transfer_id,
                    amount_usdt,
                    snapshot_json,
                    now_ms,
                    now_ms,
                ),
            )

            log_id = _generate_id("xlog")
            con.execute(
                """
                INSERT INTO transfer_reconciliation_logs (
                    log_id, intent_id, from_status, to_status, evidence_json, created_at_ms
                ) VALUES (?, ?, NULL, 'PLANNED', '{}', ?);
                """,
                (log_id, intent_id, now_ms),
            )

            con.execute("COMMIT;")

            return TransferIntent(
                schema_version=1,
                intent_id=intent_id,
                account_id=account_id,
                policy_version=decision.policy_version,
                reporting_day_utc=decision.reporting_day_utc,
                client_transfer_id=client_transfer_id,
                amount_usdt=amount_usdt,
                policy_snapshot_json=snapshot_json,
                status=TransferStatus.PLANNED,
                exchange_tran_id=None,
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
                confirmed_at_ms=None,
                last_error_code=None,
            )
        except Exception:
            try:
                con.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            con.close()

    def transition_transfer(
        self,
        intent_id: str,
        expected_status: TransferStatus,
        new_status: TransferStatus,
        evidence: Mapping[str, Any],
    ) -> TransferIntent:
        """Compare-and-set a transfer state and append audit evidence."""
        con = self._con()
        try:
            con.execute("BEGIN IMMEDIATE;")

            cur = con.execute("SELECT * FROM transfer_intents WHERE intent_id = ?;", (intent_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Transfer intent not found: {intent_id}")

            current_status = row["status"]
            if current_status != expected_status.value:
                raise ConcurrencyConflictError(
                    f"Transfer intent {intent_id} status mismatch: expected {expected_status.value}, current is {current_status}"
                )

            now_ms = int(time.time() * 1000)
            exchange_tran_id = evidence.get("exchange_tran_id", row["exchange_tran_id"])
            error_code = evidence.get("error_code", row["last_error_code"])
            confirmed_at_ms = (
                now_ms if new_status == TransferStatus.CONFIRMED else row["confirmed_at_ms"]
            )

            con.execute(
                """
                UPDATE transfer_intents
                SET status = ?, updated_at_ms = ?, confirmed_at_ms = ?,
                    exchange_tran_id = ?, last_error_code = ?
                WHERE intent_id = ? AND status = ?;
                """,
                (
                    new_status.value,
                    now_ms,
                    confirmed_at_ms,
                    exchange_tran_id,
                    error_code,
                    intent_id,
                    expected_status.value,
                ),
            )

            log_id = _generate_id("xlog")
            con.execute(
                """
                INSERT INTO transfer_reconciliation_logs (
                    log_id, intent_id, from_status, to_status, evidence_json, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (log_id, intent_id, expected_status.value, new_status.value, json.dumps(dict(evidence)), now_ms),
            )

            con.execute("COMMIT;")

            return TransferIntent(
                schema_version=row["schema_version"],
                intent_id=intent_id,
                account_id=row["account_id"],
                policy_version=row["policy_version"],
                reporting_day_utc=row["reporting_day_utc"],
                client_transfer_id=row["client_transfer_id"],
                amount_usdt=row["amount_usdt"],
                policy_snapshot_json=row["policy_snapshot_json"],
                status=new_status,
                exchange_tran_id=exchange_tran_id,
                created_at_ms=row["created_at_ms"],
                updated_at_ms=now_ms,
                confirmed_at_ms=confirmed_at_ms,
                last_error_code=error_code,
            )
        except Exception:
            try:
                con.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            con.close()

    def unresolved_transfers(self, account_id: str) -> Sequence[TransferIntent]:
        """Return SUBMITTING and UNKNOWN intents requiring read-back."""
        con = self._con()
        try:
            cur = con.execute(
                """
                SELECT * FROM transfer_intents
                WHERE account_id = ? AND status IN ('SUBMITTING', 'UNKNOWN')
                ORDER BY created_at_ms ASC;
                """,
                (account_id,),
            )
            return [self._row_to_intent(r) for r in cur.fetchall()]
        finally:
            con.close()

    @staticmethod
    def _row_to_intent(row: sqlite3.Row) -> TransferIntent:
        return TransferIntent(
            schema_version=row["schema_version"],
            intent_id=row["intent_id"],
            account_id=row["account_id"],
            policy_version=row["policy_version"],
            reporting_day_utc=row["reporting_day_utc"],
            client_transfer_id=row["client_transfer_id"],
            amount_usdt=row["amount_usdt"],
            policy_snapshot_json=row["policy_snapshot_json"],
            status=TransferStatus(row["status"]),
            exchange_tran_id=row["exchange_tran_id"],
            created_at_ms=row["created_at_ms"],
            updated_at_ms=row["updated_at_ms"],
            confirmed_at_ms=row["confirmed_at_ms"],
            last_error_code=row["last_error_code"],
        )


class SqliteRotationRepository:
    """SQLite implementation of RotationRepository protocol."""

    def __init__(self, db_path: Union[str, Path]) -> None:
        self.db_path = str(db_path)

    def _con(self) -> sqlite3.Connection:
        return get_db_connection(self.db_path)

    def append_decision(self, decision: RotationDecision) -> None:
        """Persist an immutable live or shadow decision."""
        con = self._con()
        try:
            con.execute("BEGIN IMMEDIATE;")
            now_ms = int(time.time() * 1000)
            con.execute(
                """
                INSERT OR IGNORE INTO rotation_decisions (
                    schema_version, decision_id, account_id, held_symbol, candidate_symbol,
                    position_lifecycle_id, position_version, model_version,
                    held_snapshot_id, candidate_snapshot_id, held_score, candidate_score,
                    score_edge, estimated_roundtrip_cost_usdt, estimated_cost_fraction,
                    expected_net_benefit_usdt, risk_decision_id, action,
                    reason_codes_json, decided_at_ms, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    decision.schema_version,
                    decision.decision_id,
                    decision.account_id,
                    decision.held_symbol,
                    decision.candidate_symbol,
                    decision.position_lifecycle_id,
                    decision.position_version,
                    decision.model_version,
                    decision.held_snapshot_id,
                    decision.candidate_snapshot_id,
                    decision.held_score,
                    decision.candidate_score,
                    decision.score_edge,
                    decision.estimated_roundtrip_cost_usdt,
                    decision.estimated_cost_fraction,
                    decision.expected_net_benefit_usdt,
                    decision.risk_decision_id,
                    decision.action.value,
                    json.dumps(decision.reason_codes),
                    decision.decided_at_ms,
                    now_ms,
                ),
            )
            con.execute("COMMIT;")
        except Exception:
            try:
                con.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            con.close()

    def claim_rotation_intent(self, decision: RotationDecision) -> RotationIntent:
        """Atomically enforce unique decision/lifecycle and attempt limits."""
        con = self._con()
        try:
            con.execute("BEGIN IMMEDIATE;")

            # 1. Idempotency: check if intent already exists for this decision_id
            cur = con.execute("SELECT * FROM rotation_intents WHERE decision_id = ?;", (decision.decision_id,))
            row = cur.fetchone()
            if row:
                con.execute("COMMIT;")
                return self._row_to_intent(row)

            # 2. Check for active non-terminal intent for this position lifecycle
            cur = con.execute(
                """
                SELECT * FROM rotation_intents
                WHERE position_lifecycle_id = ? AND status IN (
                    'PLANNED', 'SELL_SUBMITTING', 'SELL_UNKNOWN', 'SELL_FILLED',
                    'BUY_SUBMITTING', 'BUY_UNKNOWN'
                );
                """,
                (decision.position_lifecycle_id,),
            )
            active_row = cur.fetchone()
            if active_row:
                con.execute("COMMIT;")
                raise ClaimConflictError(
                    f"Position lifecycle {decision.position_lifecycle_id} already has active rotation intent {active_row['intent_id']} in status {active_row['status']}"
                )

            now_ms = int(time.time() * 1000)
            intent_id = _generate_id("rot_int")
            budget = decision.expected_net_benefit_usdt or "0"
            canonical_decimal(budget, non_negative=True)

            con.execute(
                """
                INSERT INTO rotation_intents (
                    schema_version, intent_id, account_id, position_lifecycle_id,
                    decision_id, sell_order_id, actual_freed_usdt, buy_order_id,
                    approved_replacement_budget_usdt, status, created_at_ms,
                    updated_at_ms, last_error_code
                ) VALUES (1, ?, ?, ?, ?, NULL, NULL, NULL, ?, 'PLANNED', ?, ?, NULL);
                """,
                (
                    intent_id,
                    decision.account_id,
                    decision.position_lifecycle_id,
                    decision.decision_id,
                    budget,
                    now_ms,
                    now_ms,
                ),
            )

            log_id = _generate_id("rot_log")
            con.execute(
                """
                INSERT INTO rotation_audit_logs (
                    log_id, intent_id, from_status, to_status, evidence_json, created_at_ms
                ) VALUES (?, ?, NULL, 'PLANNED', '{}', ?);
                """,
                (log_id, intent_id, now_ms),
            )

            con.execute("COMMIT;")

            return RotationIntent(
                schema_version=1,
                intent_id=intent_id,
                account_id=decision.account_id,
                position_lifecycle_id=decision.position_lifecycle_id,
                decision_id=decision.decision_id,
                sell_order_id=None,
                actual_freed_usdt=None,
                buy_order_id=None,
                approved_replacement_budget_usdt=budget,
                status=RotationStatus.PLANNED,
                created_at_ms=now_ms,
                updated_at_ms=now_ms,
                last_error_code=None,
            )
        except Exception:
            try:
                con.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            con.close()

    def transition_rotation(
        self,
        intent_id: str,
        expected_status: RotationStatus,
        new_status: RotationStatus,
        evidence: Mapping[str, Any],
    ) -> RotationIntent:
        """Compare-and-set rotation state and append audit evidence."""
        con = self._con()
        try:
            con.execute("BEGIN IMMEDIATE;")

            cur = con.execute("SELECT * FROM rotation_intents WHERE intent_id = ?;", (intent_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Rotation intent not found: {intent_id}")

            current_status = row["status"]
            if current_status != expected_status.value:
                raise ConcurrencyConflictError(
                    f"Rotation intent {intent_id} status mismatch: expected {expected_status.value}, current is {current_status}"
                )

            now_ms = int(time.time() * 1000)
            sell_order_id = evidence.get("sell_order_id", row["sell_order_id"])
            actual_freed = evidence.get("actual_freed_usdt", row["actual_freed_usdt"])
            if actual_freed is not None:
                actual_freed = canonical_decimal(actual_freed, non_negative=True)
            buy_order_id = evidence.get("buy_order_id", row["buy_order_id"])
            error_code = evidence.get("error_code", row["last_error_code"])

            con.execute(
                """
                UPDATE rotation_intents
                SET status = ?, updated_at_ms = ?, sell_order_id = ?,
                    actual_freed_usdt = ?, buy_order_id = ?, last_error_code = ?
                WHERE intent_id = ? AND status = ?;
                """,
                (
                    new_status.value,
                    now_ms,
                    sell_order_id,
                    actual_freed,
                    buy_order_id,
                    error_code,
                    intent_id,
                    expected_status.value,
                ),
            )

            log_id = _generate_id("rot_log")
            con.execute(
                """
                INSERT INTO rotation_audit_logs (
                    log_id, intent_id, from_status, to_status, evidence_json, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (log_id, intent_id, expected_status.value, new_status.value, json.dumps(dict(evidence)), now_ms),
            )

            con.execute("COMMIT;")

            return RotationIntent(
                schema_version=row["schema_version"],
                intent_id=intent_id,
                account_id=row["account_id"],
                position_lifecycle_id=row["position_lifecycle_id"],
                decision_id=row["decision_id"],
                sell_order_id=sell_order_id,
                actual_freed_usdt=actual_freed,
                buy_order_id=buy_order_id,
                approved_replacement_budget_usdt=row["approved_replacement_budget_usdt"],
                status=new_status,
                created_at_ms=row["created_at_ms"],
                updated_at_ms=now_ms,
                last_error_code=error_code,
            )
        except Exception:
            try:
                con.execute("ROLLBACK;")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            con.close()

    @staticmethod
    def _row_to_intent(row: sqlite3.Row) -> RotationIntent:
        return RotationIntent(
            schema_version=row["schema_version"],
            intent_id=row["intent_id"],
            account_id=row["account_id"],
            position_lifecycle_id=row["position_lifecycle_id"],
            decision_id=row["decision_id"],
            sell_order_id=row["sell_order_id"],
            actual_freed_usdt=row["actual_freed_usdt"],
            buy_order_id=row["buy_order_id"],
            approved_replacement_budget_usdt=row["approved_replacement_budget_usdt"],
            status=RotationStatus(row["status"]),
            created_at_ms=row["created_at_ms"],
            updated_at_ms=row["updated_at_ms"],
            last_error_code=row["last_error_code"],
        )


class LedgerRepository:
    """Unified access to accounting, transfer, and rotation repositories."""

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        if db_path is None:
            from hermes.config import ACCOUNTING_DB_PATH
            db_path = ACCOUNTING_DB_PATH
        self.db_path = str(db_path)
        init_db(self.db_path)
        self.accounting = SqliteAccountingRepository(self.db_path)
        self.transfers = SqliteTransferIntentRepository(self.db_path)
        self.rotation = SqliteRotationRepository(self.db_path)
