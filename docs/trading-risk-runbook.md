# Hermes Trading Risk & Operating Runbook

Status: Active Operational Standard & SRE Runbook  
Target System: Hermes Autonomous Trading System (Spot & Perpetual Futures)  
Parent Architecture Contract: `docs/trading-risk-contract.md`  
Scope: Operator procedures, risk invariants, checkpoint lifecycle, emergency runbooks, and canary verification.

---

## 1. Executive Summary & Core Risk Principles

The Hermes Autonomous Trading System is designed with an uncompromising **risk-first architecture**. Every operational action, algorithmic entry, and profit-taking routine is strictly subordinated to protective capital boundaries.

### Core Operating Tenets:
1. **Safety Exits Have Absolute Precedence:**
   - Protective stops (Hard Stop Loss, Trailing Stop, Profit Floor Breach, Emergency Veto) execute deterministically on the protective execution loop.
   - External data fetches, advisory LLMs, news sentiment, and technical signal labels MUST NEVER delay, weaken, or cancel a triggered exit.
2. **Signals Are Evidence, Not Unconditional Commands:**
   - Strategy labels such as `STRONG_BUY` or `STRONG_SELL` represent probabilistic indicator evidence, never immediate exchange commands.
   - An indicator label cannot override portfolio risk limits, bypass protective stops, or trigger automatic additions/averaging down into losing positions.
3. **Transparent Terminology & Disclosed Provenance:**
   - Software trigger thresholds (e.g. `profit_floor_trigger_pct`) are conditional triggers, never guarantees of profit or execution price.
   - Alerts must strictly differentiate between:
     * **Gross Price PnL:** Price movement return before trading fees and slippage.
     * **Futures ROE %:** Gross return on initial margin before funding fees and exchange commissions.
     * **Net Realized PnL:** Audited net return after deducting all exchange trading fees, borrowing fees, and funding rate settlements.
   - When deterministic logic or fallback is used, notifications must never falsely claim "AI Decision".
4. **Fail-Closed Default:**
   - Any missing, stale, crossed, or unvalued market data causes entry gates to reject new trades and causes continuation checkpoints to fail closed into deterministic profit-taking.

---

## 2. Checkpoint State Machine Lifecycle

Positions follow a nonblocking, version-scoped finite state machine (FSM) across their lifecycle:

```
[ ENTRY EXECUTED ]
        │
        ▼
   ┌──────────┐
   │ STANDARD │ ◄── (Normal monitoring; Hard SL active at -5.0%)
   └────┬─────┘
        │ Price reaches Trailing Activation (+6.0%)
        ▼
┌────────────────┐
│ TRAILING_ARMED │ ◄── (Trailing stop active; 2.5% pullback from peak)
└───────┬────────┘
        │ Price reaches Checkpoint (+10.0% PnL / ROE)
        ▼
┌────────────────┐
│ TP_EVALUATING  │ ◄── (Async momentum evaluation; 3.0s strict deadline)
└───┬────────┬───┘
    │        │
    │        │ [Floor breached during eval OR Eval timeout OR Reversal detected]
    │        ▼
    │  ┌──────────────┐
    │  │ EXIT_PENDING │ ◄── (Exit order submitted; duplicate order prevention)
    │  └──────┬───────┘
    │         │
    │ [Continuation gates pass & Trend strong]
    ▼         ▼
┌────────┐ ┌────────┐
│ RIDING │ │ CLOSED │
└───┬────┘ └────────┘
    │ (Trailing with Ratcheted Profit Floor: +8%, +15%, +25%, +35%)
    ▼
[ Trailing / Floor Breach ] ──► EXIT_PENDING ──► CLOSED
```

### State Definitions & Operational Rules:

| State | Purpose & Active Rules | Transition Triggers |
| :--- | :--- | :--- |
| **`STANDARD`** | Initial position state upon fill confirmation. Hard Stop Loss is active (default -5.0%). | Reaches +6.0% PnL / ROE $\rightarrow$ `TRAILING_ARMED`. Hard SL hit $\rightarrow$ `EXIT_PENDING`. |
| **`TRAILING_ARMED`** | Pre-TP trailing protection active. Peak price/ROE is persistently tracked. Stop triggers if price retraces 2.5% from peak. | Reaches +10.0% PnL / ROE $\rightarrow$ `TP_EVALUATING`. Retraces 2.5% from peak $\rightarrow$ `EXIT_PENDING`. |
| **`TP_EVALUATING`** | Nonblocking evaluation state. Background thread evaluates multi-timeframe RSI, orderbook imbalance, news sentiment, and advisory AI. **Hard deadline: 3.0 seconds.** | Floor breached or deadline expired $\rightarrow$ `EXIT_PENDING`. Continuation approved $\rightarrow$ `RIDING`. Take profit recommended $\rightarrow$ `EXIT_PENDING`. |
| **`RIDING`** | Extended trend-riding mode. Position is protected by dynamic trailing and a **monotonic profit floor trigger** starting at +8.0% (ratchets upward at +15%, +25%, +35%). | Floor trigger breached or trailing stop hit $\rightarrow$ `EXIT_PENDING`. |
| **`EXIT_PENDING`** | Exit order submitted to exchange. Position is locked against duplicate order dispatch, re-evaluation, or DCA additions pending fill reconciliation. | Fill confirmed via WebSocket/REST $\rightarrow$ `CLOSED`. Order rejected/failed $\rightarrow$ retry with exponential backoff. |
| **`CLOSED`** | Position fully closed and reconciled in ledger. State cleaned up. | End of lifecycle. |

### Lifecycle Invariants:
- **Version & Lifecycle Scoping:** Every evaluation carries `position_lifecycle_id` and `position_version`. If a position experiences a partial fill, addition, or manual adjustment while evaluation is in-flight, the returned decision is discarded as stale (`[TP-EVAL-STALE]`).
- **Immediate Floor Breach Exit:** While in `TP_EVALUATING`, if the live market price drops below the initial +8.0% floor, the system DOES NOT wait for the AI response to return; it immediately executes a protective exit (`[TP-EVAL-FLOOR-BREACH]`).
- **Monotonicity:** Stops and profit floors can only tighten (for LONG: trigger price can only increase; for SHORT: trigger price can only decrease). No algorithm or advisory input may widen a stop.

---

## 3. Signal Evidence vs Order Commands

Hermes enforces a strict 6-tier policy hierarchy to resolve potential conflicts between indicator signals, market conditions, and protective exits (`hermes/trading/signal_policy.py`).

```
┌─────────────────────────────────────────────────────────────┐
│ TIER 1: Reconciliation & Known Pending Order Protection     │
├─────────────────────────────────────────────────────────────┤
│ TIER 2: Deterministic Hard SL, Trailing Stop, Floor Breach  │
├─────────────────────────────────────────────────────────────┤
│ TIER 3: Confirmed Emergency Veto & Thesis Invalidation     │
├─────────────────────────────────────────────────────────────┤
│ TIER 4: Take Profit Checkpoint (+10.0% Gross Evaluation)    │
├─────────────────────────────────────────────────────────────┤
│ TIER 5: Confirmed Reversal Early Exit (2-Bar Confirmation)  │
├─────────────────────────────────────────────────────────────┤
│ TIER 6: New Entry & Position Addition (Portfolio Risk Gate) │
└─────────────────────────────────────────────────────────────┘
```

### Precedence Rules in Detail:

1. **Tier 1 (Reconciliation):** If an order is already pending execution or exchange reconciliation is active, no duplicate sell or buy order can be placed.
2. **Tier 2 (Protective Exits):** Hard Stop Loss (-5.0%), active trailing stop, or profit floor breach (+8.0% floor in RIDING/EVAL) triggers an immediate exit.
   - *Invariant:* If `STRONG_BUY` appears while a position is crossing its Stop Loss, the system MUST EXIT. It is strictly forbidden to buy more to "average down" or hold in defiance of a stop.
3. **Tier 3 (Emergency Invalidation):** Severe adverse news catalyst (`is_emergency=True`) or extreme market collapse allows immediate risk-reducing exit regardless of position PnL.
4. **Tier 4 (Checkpoint Evaluation):** At +10.0% gross gain, continuation evaluation is launched asynchronously without blocking protective stops.
5. **Tier 5 (Normal Reversal):** Legacy raw `STRONG_SELL` labels at small profits (+2%) NO LONGER trigger immediate exits. Early exit requires **confirmed structural reversal**:
   - Adverse price-structure break + momentum confirmation across **two distinct closed bars** on the strategy timeframe.
   - If bars are identical, stale, or incomplete, the position remains protected by its trailing stop rather than prematurely exiting on noise.
6. **Tier 6 (New Entries & Additions):** Buy signals must pass the centralized portfolio risk gate. Even a score of 10/10 cannot bypass cash reserve requirements or circuit breakers.

---

## 4. Centralized Portfolio Risk Gates & Circuit Breakers

All order-generating subsystems (Standard Signals, Spike Detection, DCA, Capital Rotation, and Agent Tools) MUST route proposed entries through the unified `PortfolioRiskManager` (`hermes/trading/portfolio_risk.py`).

### Operational Risk Boundaries:
- **Per-Trade Risk Budget:** Maximum **0.5% of total portfolio equity** risked to the initial stop loss.
- **Aggregate Stop Risk:** Maximum **2.0% of portfolio equity** across all open positions' planned stop distances.
- **Cash Reserve Requirement:** Minimum **25.0% of tradable equity** must remain uncommitted in free Spot USDT.
- **Daily Drawdown Circuit Breaker:** Maximum **2.0% mark-to-market daily loss** from the midnight UTC equity baseline.
  - If daily realized + unrealized losses reach 2.0%, the circuit breaker trips (`CIRCUIT_BREAKER_ACTIVE`).
  - **Effect:** All new trade entries, DCA purchases, and capital rotations are immediately halted.
  - **Exits Remain Fully Operational:** Protective stops and take-profit executions continue without interruption.
- **Perpetual Futures Opt-In Policy:**
  - `FUTURES_ENABLED = False` by default.
  - Automatic fallback from Spot capital shortage to 3x Futures leverage is permanently disabled.
  - Futures entries require explicit operational configuration and independent margin limits.

---

## 5. Emergency Incident Runbooks

### Incident 1: Failed Exit Order / Exchange Rejection
**Symptoms:** Log entries `[SELL-FAILED]`, `LOT_SIZE filter failure`, `MIN_NOTIONAL`, or API rejection.  
**Automated Response:**
1. Position state transitions to `EXIT_PENDING`.
2. Error is logged in `pos["pending_exit_error"]` and `pos["last_sell_attempt"]`.
3. Position is NOT deleted from state and NOT allowed to be re-evaluated into holding.
4. The daemon retries execution with exponential backoff while verifying fresh balance from exchange.

**Operator Action:**
1. Check live exchange order history to verify if a partial fill occurred:
   ```bash
   python3 -m hermes.cli check-balance
   ```
2. If dust remains below exchange minimum ($5.00 USDT), use dust conversion or log remaining position.
3. If order failed due to LOT_SIZE precision or price tick size, verify `hermes/api/binance_spot.py` lot filter rules.

---

### Incident 2: Exchange Latency or Outage (>3000ms / 5xx Errors)
**Symptoms:** Log entry `[TP-DEADLINE-EXPIRED] ... evaluation exceeded 3s deadline` or REST timeouts.  
**Automated Response:**
1. Position evaluation fails closed: aborts continuation evaluation and triggers immediate market Take Profit.
2. If exchange REST API is completely unreachable, local quote engine holds stops until connectivity resumes.
3. Failsafe: Never double-submit orders on network timeout; inspect open orders first (`check_open_orders`).

**Operator Action:**
1. Verify Binance system status: `https://www.binance.com/en/support/announcement/system-maintenance`
2. Check local network egress from host:
   ```bash
   curl -I --connect-timeout 3 https://api.binance.com/api/v3/ping
   ```

---

### Incident 3: Daily Circuit Breaker Tripped
**Symptoms:** Telegram warning or logs `🛑 [CIRCUIT-BREAKER] Tripped: Daily loss $XX.XX reached circuit breaker threshold (2.0%)`.  
**System State:** New buys halted. Existing open positions remain monitored for TP/SL.  
**Operator Action:**
1. Do NOT rush to override or reset the circuit breaker during volatile or cascading market dumps.
2. Inspect open positions and unrealized equity:
   ```bash
   python3 -m hermes.cli positions
   ```
3. Verify if daily loss baseline is accurate (`hermes/trading/portfolio_risk.py` tracks baseline from 00:00 UTC).
4. If market conditions stabilize and PO approves manual reset:
   ```python
   from hermes.trading.portfolio_risk import get_portfolio_risk_manager
   get_portfolio_risk_manager().reset_circuit_breaker()
   ```

---

### Incident 4: Daemon Restart / Process Crash Recovery
**Symptoms:** Daemon process restarted or host rebooted.  
**System State:**
1. State is rehydrated from persistent storage (`hermes_state.json` / SQLite ledger).
2. Any position marked `TP_EVALUATING` during crash resumes with deadline check: if expired, exits immediately.
3. Any position in `RIDING` retains its saved `profit_floor_trigger_pct` (or legacy `guaranteed_floor_pct`).
4. Trailing stop high-water peak prices are preserved in persistent state, preventing loss of protection.

**Operator Action:**
1. Verify daemon is healthy and running:
   ```bash
   ps aux | grep "[h]ermes"
   ```
2. Verify positions and persistent state integrity:
   ```bash
   python3 -c "from hermes.state import state; print(f'Active positions: {len(state.positions)}')"
   ```

---

## 6. Notification Terminology Standards

To maintain investor trust and compliance with accurate risk disclosures, all notification messages must adhere to the following terminology standards:

| Prohibited Misleading Phrasing | Approved Transparent Phrasing | Rationale |
| :--- | :--- | :--- |
| "Profit Terkunci", "Floor Terkunci" | **"Trigger Profit Floor", "Profit Floor Trigger"** | Software stops trigger market/limit orders; fills depend on orderbook liquidity and are never locked or guaranteed. |
| "Dijamin Profit", "Garansi Cuan" | **"Target Profit Tercapai", "Proteksi Bersyarat"** | Exchange execution always entails slippage and execution risk. |
| "Untung Bersih" (for gross price diff) | **"Gross Price PnL (Sebelum Fee)"** | Gross price movement does not account for exchange trading fees or commissions. |
| "Untung Bersih (ROE)" | **"Futures ROE (Gross Return on Margin)"** | ROE excludes exchange commissions and periodic funding rate debits. |
| "Rasional AI" (when deterministic fallback used) | **"Kebijakan Teknis Deterministik" / "Fallback Deterministik"** | Always disclose true decision provenance; never fabricate AI involvement. |
| "Modal pokok aman di Spot" | **"Modal trading aktif tetap berada di Spot"** | Crypto spot holdings fluctuate in market value; principal is not capital-guaranteed. |

---

## 7. Canary Deployment & Verification Checklist

Before releasing updates or restarting production trading services, operators must complete this verification checklist:

### Pre-Deployment Checks:
- [ ] **Isolated Test Suite Clean:**
  ```bash
  python3 -m unittest discover -s tests -p 'test_*.py' -v
  ```
  *Requirement:* 100% tests pass (0 failures, 0 errors, unmocked network calls blocked by test isolation).
- [ ] **Clean Code Compilation:**
  ```bash
  python3 -m compileall -q hermes tests
  ```
  *Requirement:* Zero syntax or compilation errors.
- [ ] **Production State Untouched:**
  Verify that test runs did not write to production ledger, state files, or make unauthorized external API calls.
- [ ] **Configuration Audited:**
  * `FUTURES_ENABLED = False` (unless explicit PO signoff obtained).
  * `PORTFOLIO_MAX_TRADE_RISK_PCT = 0.005` (0.5%).
  * `PORTFOLIO_MAX_TOTAL_STOP_RISK_PCT = 0.02` (2.0%).
  * `PORTFOLIO_CIRCUIT_BREAKER_DAILY_LOSS_PCT = 0.02` (2.0%).
  * `PORTFOLIO_MIN_USDT_RESERVE_RATIO = 0.25` (25.0%).

### Deployment Execution:
- [ ] **Snapshot Live State & Database:**
  Create a timestamped backup of current persistent state files before deploying new code.
- [ ] **Process Supervision:**
  Ensure live daemon process (or container) is cleanly managed and existing open positions have their protective stops active.
- [ ] **Post-Deployment Log Inspection (First 30 minutes):**
  * Check for `[RISK-GATE]` evaluation on entry signals.
  * Confirm `[TP-CHECKPOINT]` nonblocking behavior when positions hit +10.0%.
  * Verify Telegram alerts display transparent terminology ("Gross Price PnL", "Profit Floor Trigger", accurate decision source).

---

## 8. Rollback Procedures

If an unexpected regression or exchange integration anomaly occurs:

1. **Safety Exit Preservation:**
   - NEVER roll back to legacy code that contains known protective stop bugs (such as trailing stop bypass or swallowed orderbook exceptions).
   - If rolling back strategy or rotation logic, retain the core safety modules (`exit_policy.py`, `portfolio_risk.py`, `signal_policy.py`).
2. **Safe Fallback Configuration:**
   - To immediately halt all new trading while keeping existing positions safely monitored, set:
     ```python
     # In hermes/config.py
     EMERGENCY_HALT_ENTRIES = True
     ```
   - In this mode, entries, DCAs, and rotations are rejected by the risk gate, while Stop Loss, Trailing Stop, and TP checkpoint exits continue running uninterrupted.
