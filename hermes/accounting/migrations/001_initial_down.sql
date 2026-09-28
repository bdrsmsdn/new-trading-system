-- Rollback Migration: 001_initial_down.sql
-- Description: Rollback initial schema for Phase 2 durable accounting and rotation ledger
-- Safe, clean drop in dependency order

DROP TABLE IF EXISTS rotation_audit_logs;
DROP TABLE IF EXISTS rotation_intents;
DROP TABLE IF EXISTS rotation_decisions;
DROP TABLE IF EXISTS transfer_reconciliation_logs;
DROP TABLE IF EXISTS transfer_intents;
DROP TABLE IF EXISTS external_cash_flows;
DROP TABLE IF EXISTS lot_allocations;
DROP TABLE IF EXISTS realized_outcomes;
DROP TABLE IF EXISTS lots;
DROP TABLE IF EXISTS fills;
DROP TABLE IF EXISTS reconciliation_blockers;
DROP TABLE IF EXISTS accounting_cutovers;
DROP TABLE IF EXISTS schema_migrations;
