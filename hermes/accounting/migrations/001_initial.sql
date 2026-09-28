-- Migration: 001_initial.sql
-- Description: Create initial schema for Phase 2 durable accounting and rotation ledger
-- Safe, idempotent, non-destructive migration

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at_ms INTEGER NOT NULL,
    checksum TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounting_cutovers (
    cutover_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    venue TEXT NOT NULL CHECK(venue IN ('SPOT', 'FUTURES')),
    cutover_at_ms INTEGER NOT NULL,
    baseline_reference TEXT NOT NULL,
    backfill_from_ms INTEGER,
    backfill_through_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'APPROVED', 'REJECTED')),
    approved_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cutovers_lookup 
ON accounting_cutovers(account_id, venue, status);

CREATE TABLE IF NOT EXISTS fills (
    schema_version INTEGER NOT NULL DEFAULT 1,
    account_id TEXT NOT NULL,
    venue TEXT NOT NULL CHECK(venue IN ('SPOT', 'FUTURES')),
    symbol TEXT NOT NULL,
    trade_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    event_time_ms INTEGER NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
    price TEXT NOT NULL,
    base_qty TEXT NOT NULL,
    quote_qty TEXT NOT NULL,
    commission_asset TEXT NOT NULL,
    commission_qty TEXT NOT NULL,
    commission_usdt TEXT,
    valuation_status TEXT NOT NULL CHECK(valuation_status IN ('VALUED', 'PENDING', 'UNAVAILABLE', 'INVALID')),
    source_payload_hash TEXT NOT NULL,
    is_quarantined INTEGER NOT NULL DEFAULT 0,
    quarantine_reason TEXT,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (account_id, venue, symbol, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_fills_symbol_time 
ON fills(symbol, event_time_ms);

CREATE INDEX IF NOT EXISTS idx_fills_time 
ON fills(event_time_ms);

CREATE INDEX IF NOT EXISTS idx_fills_order 
ON fills(order_id);

CREATE INDEX IF NOT EXISTS idx_fills_side 
ON fills(side);

CREATE TABLE IF NOT EXISTS lots (
    schema_version INTEGER NOT NULL DEFAULT 1,
    lot_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    venue TEXT NOT NULL CHECK(venue IN ('SPOT', 'FUTURES')),
    symbol TEXT NOT NULL,
    acquired_trade_id TEXT NOT NULL,
    opened_at_ms INTEGER NOT NULL,
    original_base_qty TEXT NOT NULL,
    remaining_base_qty TEXT NOT NULL,
    quote_cost_usdt TEXT NOT NULL,
    allocated_buy_fee_usdt TEXT,
    completeness TEXT NOT NULL CHECK(completeness IN ('VERIFIED', 'PARTIAL', 'UNRESOLVED')),
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY (account_id, venue, symbol, acquired_trade_id)
        REFERENCES fills(account_id, venue, symbol, trade_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_lots_fifo 
ON lots(account_id, venue, symbol, opened_at_ms, acquired_trade_id, lot_id);

CREATE INDEX IF NOT EXISTS idx_lots_remaining 
ON lots(symbol, remaining_base_qty);

CREATE TABLE IF NOT EXISTS realized_outcomes (
    schema_version INTEGER NOT NULL DEFAULT 1,
    outcome_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    venue TEXT NOT NULL CHECK(venue IN ('SPOT', 'FUTURES')),
    symbol TEXT NOT NULL,
    sell_trade_id TEXT NOT NULL,
    sold_base_qty TEXT NOT NULL,
    gross_proceeds_usdt TEXT NOT NULL,
    fifo_cost_usdt TEXT NOT NULL,
    buy_fee_usdt TEXT,
    sell_fee_usdt TEXT,
    net_pnl_usdt TEXT,
    completeness TEXT NOT NULL CHECK(completeness IN ('VERIFIED', 'PARTIAL', 'UNRESOLVED')),
    reporting_day_utc TEXT NOT NULL,
    incomplete_reason_codes_json TEXT NOT NULL DEFAULT '[]',
    created_at_ms INTEGER NOT NULL,
    UNIQUE (account_id, venue, symbol, sell_trade_id),
    FOREIGN KEY (account_id, venue, symbol, sell_trade_id)
        REFERENCES fills(account_id, venue, symbol, trade_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_realized_outcomes_reporting_day 
ON realized_outcomes(reporting_day_utc);

CREATE INDEX IF NOT EXISTS idx_realized_outcomes_completeness 
ON realized_outcomes(completeness);

CREATE INDEX IF NOT EXISTS idx_realized_outcomes_account_symbol 
ON realized_outcomes(account_id, symbol);

CREATE INDEX IF NOT EXISTS idx_realized_outcomes_pnl 
ON realized_outcomes(net_pnl_usdt);

CREATE TABLE IF NOT EXISTS lot_allocations (
    allocation_id TEXT PRIMARY KEY,
    outcome_id TEXT NOT NULL,
    lot_id TEXT NOT NULL,
    opening_account_id TEXT NOT NULL,
    opening_venue TEXT NOT NULL CHECK(opening_venue IN ('SPOT', 'FUTURES')),
    opening_symbol TEXT NOT NULL,
    opening_trade_id TEXT NOT NULL,
    closing_account_id TEXT NOT NULL,
    closing_venue TEXT NOT NULL CHECK(closing_venue IN ('SPOT', 'FUTURES')),
    closing_symbol TEXT NOT NULL,
    closing_trade_id TEXT NOT NULL,
    allocated_base_qty TEXT NOT NULL,
    allocated_cost_usdt TEXT NOT NULL,
    allocated_buy_fee_usdt TEXT,
    allocated_sell_fee_usdt TEXT,
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY (outcome_id)
        REFERENCES realized_outcomes(outcome_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (lot_id)
        REFERENCES lots(lot_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_allocations_outcome 
ON lot_allocations(outcome_id);

CREATE INDEX IF NOT EXISTS idx_allocations_lot 
ON lot_allocations(lot_id);

CREATE TABLE IF NOT EXISTS external_cash_flows (
    flow_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    asset TEXT NOT NULL,
    amount TEXT NOT NULL,
    flow_type TEXT NOT NULL CHECK(flow_type IN ('DEPOSIT', 'WITHDRAWAL', 'TRANSFER_SPOT_TO_FUNDING', 'TRANSFER_FUNDING_TO_SPOT', 'OTHER')),
    event_time_ms INTEGER NOT NULL,
    tx_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cash_flows_time 
ON external_cash_flows(event_time_ms);

CREATE INDEX IF NOT EXISTS idx_cash_flows_type 
ON external_cash_flows(flow_type);

CREATE INDEX IF NOT EXISTS idx_cash_flows_asset 
ON external_cash_flows(asset);

CREATE TABLE IF NOT EXISTS transfer_intents (
    schema_version INTEGER NOT NULL DEFAULT 1,
    intent_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    reporting_day_utc TEXT NOT NULL,
    client_transfer_id TEXT NOT NULL UNIQUE,
    amount_usdt TEXT NOT NULL,
    policy_snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PLANNED', 'SUBMITTING', 'UNKNOWN', 'CONFIRMED', 'FAILED_FINAL', 'QUARANTINED')),
    exchange_tran_id TEXT UNIQUE,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    confirmed_at_ms INTEGER,
    last_error_code TEXT,
    UNIQUE (account_id, policy_version, reporting_day_utc)
);

CREATE INDEX IF NOT EXISTS idx_transfers_status 
ON transfer_intents(status);

CREATE INDEX IF NOT EXISTS idx_transfers_day 
ON transfer_intents(reporting_day_utc);

CREATE INDEX IF NOT EXISTS idx_transfers_account_status 
ON transfer_intents(account_id, status);

CREATE TABLE IF NOT EXISTS transfer_reconciliation_logs (
    log_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY (intent_id)
        REFERENCES transfer_intents(intent_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_transfer_logs_intent 
ON transfer_reconciliation_logs(intent_id, created_at_ms);

CREATE TABLE IF NOT EXISTS rotation_decisions (
    schema_version INTEGER NOT NULL DEFAULT 1,
    decision_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    held_symbol TEXT NOT NULL,
    candidate_symbol TEXT NOT NULL,
    position_lifecycle_id TEXT NOT NULL,
    position_version INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    held_snapshot_id TEXT NOT NULL,
    candidate_snapshot_id TEXT NOT NULL,
    held_score TEXT NOT NULL,
    candidate_score TEXT NOT NULL,
    score_edge TEXT NOT NULL,
    estimated_roundtrip_cost_usdt TEXT,
    estimated_cost_fraction TEXT,
    expected_net_benefit_usdt TEXT,
    risk_decision_id TEXT,
    action TEXT NOT NULL CHECK(action IN ('APPROVE', 'NO_ROTATION', 'BLOCKED')),
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    decided_at_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rotation_decisions_lifecycle 
ON rotation_decisions(position_lifecycle_id);

CREATE INDEX IF NOT EXISTS idx_rotation_decisions_decided_at 
ON rotation_decisions(decided_at_ms);

CREATE INDEX IF NOT EXISTS idx_rotation_decisions_action 
ON rotation_decisions(action);

CREATE TABLE IF NOT EXISTS rotation_intents (
    schema_version INTEGER NOT NULL DEFAULT 1,
    intent_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    position_lifecycle_id TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE,
    sell_order_id TEXT,
    actual_freed_usdt TEXT,
    buy_order_id TEXT,
    approved_replacement_budget_usdt TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PLANNED', 'SELL_SUBMITTING', 'SELL_UNKNOWN', 'SELL_FILLED', 'BUY_SUBMITTING', 'BUY_UNKNOWN', 'COMPLETED', 'CASH_RECOVERY', 'QUARANTINED')),
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    last_error_code TEXT,
    FOREIGN KEY (decision_id)
        REFERENCES rotation_decisions(decision_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_rotation_intents_lifecycle_status 
ON rotation_intents(position_lifecycle_id, status);

CREATE INDEX IF NOT EXISTS idx_rotation_intents_created_at 
ON rotation_intents(created_at_ms);

CREATE INDEX IF NOT EXISTS idx_rotation_intents_status 
ON rotation_intents(status);

CREATE TABLE IF NOT EXISTS rotation_audit_logs (
    log_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY (intent_id)
        REFERENCES rotation_intents(intent_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_rotation_logs_intent 
ON rotation_audit_logs(intent_id, created_at_ms);

CREATE TABLE IF NOT EXISTS reconciliation_blockers (
    blocker_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    blocker_type TEXT NOT NULL,
    reference_id TEXT,
    reason_code TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at_ms INTEGER NOT NULL,
    resolved_at_ms INTEGER
);

CREATE INDEX IF NOT EXISTS idx_blockers_lookup 
ON reconciliation_blockers(account_id, is_active);
