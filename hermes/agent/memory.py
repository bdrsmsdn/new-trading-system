"""
Hermes Agent Memory — Self-learning system for trade decisions.

Logs trade decisions, tracks outcomes, extracts patterns, and stores
custom strategies the agent has created from learned patterns.
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
from hermes.logging_setup import log
from hermes.config import SCRIPT_DIR


MEMORY_FILE = SCRIPT_DIR / "hermes_agent_memory.json"


class AgentMemory:
    """Persistent memory system for self-learning trading agent."""

    def __init__(self):
        self.decisions: List[Dict] = []       # Trade decisions log
        self.outcomes: List[Dict] = []        # Trade outcomes log
        self.custom_strategies: List[Dict] = []  # Agent-created strategies
        self.learned_rules: List[Dict] = []    # Extracted patterns
        self.conversation_summaries: List[Dict] = []  # Summarized past interactions
        self.load()

    def load(self):
        """Load memory from disk."""
        if MEMORY_FILE.exists():
            try:
                data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
                self.decisions = data.get("decisions", [])
                self.outcomes = data.get("outcomes", [])
                self.custom_strategies = data.get("custom_strategies", [])
                self.learned_rules = data.get("learned_rules", [])
                self.conversation_summaries = data.get("conversation_summaries", [])
            except Exception as e:
                log.warning(f"[MEMORY] Failed to load: {e}")

    def save(self):
        """Save memory to disk."""
        try:
            data = {
                "decisions": self.decisions[-500:],  # Keep last 500
                "outcomes": self.outcomes[-500:],
                "custom_strategies": self.custom_strategies[-50:],
                "learned_rules": self.learned_rules[-100:],
                "conversation_summaries": self.conversation_summaries[-50:],
                "last_updated": datetime.now().isoformat()
            }
            MEMORY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str),
                                   encoding="utf-8")
        except Exception as e:
            log.error(f"[MEMORY] Failed to save: {e}")

    # ─── Trade Decision Logging ────────────────────────────────────────────────

    def log_trade_decision(self, pair: str, direction: str, signal_data: dict,
                           context: dict, confirmed: bool) -> str:
        """Log a trade decision made through the agent.

        Args:
            pair: Trading pair (e.g. 'doge')
            direction: 'BUY' or 'SELL'
            signal_data: Signal info (RSI, F&G, confidence, etc.)
            context: Additional context (user query, reason)
            confirmed: Whether the user confirmed the trade

        Returns:
            Decision ID for linking to outcome
        """
        decision_id = f"d_{int(time.time())}_{pair}"
        entry = {
            "id": decision_id,
            "timestamp": datetime.now().isoformat(),
            "pair": pair,
            "direction": direction,
            "signal_data": signal_data,
            "context": context,
            "confirmed": confirmed,
            "outcome_linked": False
        }
        self.decisions.append(entry)
        self.save()
        log.info(f"[MEMORY] Logged decision {decision_id}: {direction} {pair}")
        return decision_id

    def log_trade_outcome(self, pair: str, entry_price: float, exit_price: float,
                          pnl_pct: float, hold_duration_hours: float,
                          exit_reason: str) -> None:
        """Log the outcome of a trade for learning.

        Args:
            pair: Trading pair
            entry_price: Entry price in USDT
            exit_price: Exit price in USDT
            pnl_pct: Profit/loss percentage
            hold_duration_hours: How long the position was held
            exit_reason: Why it was closed (TP, SL, trailing, manual)
        """
        outcome = {
            "timestamp": datetime.now().isoformat(),
            "pair": pair,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl_pct": round(pnl_pct, 2),
            "hold_duration_hours": round(hold_duration_hours, 2),
            "exit_reason": exit_reason,
            "win": pnl_pct > 0
        }

        # Try to link to most recent decision for this pair
        for decision in reversed(self.decisions):
            if decision["pair"] == pair and not decision["outcome_linked"]:
                outcome["decision_id"] = decision["id"]
                outcome["decision_context"] = decision.get("signal_data", {})
                decision["outcome_linked"] = True
                break

        self.outcomes.append(outcome)
        self.save()
        log.info(f"[MEMORY] Logged outcome: {pair} PnL={pnl_pct:+.2f}% ({exit_reason})")

    # ─── Performance Analysis ──────────────────────────────────────────────────

    def get_performance_summary(self) -> dict:
        """Get aggregated performance statistics for self-learning."""
        if not self.outcomes:
            return {
                "total_trades": 0,
                "message": "No trade outcomes recorded yet. The agent will learn as trades are executed and closed."
            }

        total = len(self.outcomes)
        wins = sum(1 for o in self.outcomes if o.get("win"))
        losses = total - wins
        win_rate = (wins / total * 100) if total > 0 else 0

        pnls = [o.get("pnl_pct", 0) for o in self.outcomes]
        avg_pnl = sum(pnls) / len(pnls) if pnls else 0
        best_trade = max(pnls) if pnls else 0
        worst_trade = min(pnls) if pnls else 0

        # Per-pair stats
        pair_stats = {}
        for o in self.outcomes:
            pair = o.get("pair", "unknown")
            if pair not in pair_stats:
                pair_stats[pair] = {"wins": 0, "losses": 0, "total_pnl": 0}
            if o.get("win"):
                pair_stats[pair]["wins"] += 1
            else:
                pair_stats[pair]["losses"] += 1
            pair_stats[pair]["total_pnl"] += o.get("pnl_pct", 0)

        # Exit reason stats
        exit_stats = {}
        for o in self.outcomes:
            reason = o.get("exit_reason", "unknown")
            if reason not in exit_stats:
                exit_stats[reason] = {"count": 0, "avg_pnl": 0, "total_pnl": 0}
            exit_stats[reason]["count"] += 1
            exit_stats[reason]["total_pnl"] += o.get("pnl_pct", 0)
        for reason in exit_stats:
            count = exit_stats[reason]["count"]
            exit_stats[reason]["avg_pnl"] = round(exit_stats[reason]["total_pnl"] / count, 2) if count > 0 else 0

        return {
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(win_rate, 1),
            "avg_pnl_pct": round(avg_pnl, 2),
            "best_trade_pnl": round(best_trade, 2),
            "worst_trade_pnl": round(worst_trade, 2),
            "pair_performance": pair_stats,
            "exit_reason_stats": exit_stats,
            "custom_strategies_count": len(self.custom_strategies),
            "learned_rules_count": len(self.learned_rules)
        }

    # ─── Strategy Management ──────────────────────────────────────────────────

    def save_custom_strategy(self, rule: str, confidence: str = "hypothesis") -> None:
        """Save a custom strategy rule the agent has learned.

        Args:
            rule: The strategy rule description
            confidence: 'hypothesis' (untested), 'observed' (seen in data), 'confirmed' (profitable)
        """
        entry = {
            "rule": rule,
            "confidence": confidence,
            "created_at": datetime.now().isoformat(),
            "times_used": 0,
            "success_rate": None
        }
        # Don't duplicate rules
        for existing in self.custom_strategies:
            if existing["rule"].lower() == rule.lower():
                existing["confidence"] = confidence
                existing["updated_at"] = datetime.now().isoformat()
                self.save()
                return

        self.custom_strategies.append(entry)
        self.save()
        log.info(f"[MEMORY] Saved strategy: {rule} (confidence: {confidence})")

    def get_custom_strategies(self) -> List[Dict]:
        """Get all custom strategies."""
        return self.custom_strategies

    def get_learned_rules(self) -> List[Dict]:
        """Get learned rules/patterns."""
        return self.learned_rules

    # ─── Context for System Prompt ─────────────────────────────────────────────

    def get_learning_context(self) -> str:
        """Generate a context string for the AI system prompt with learned insights."""
        lines = []

        # Performance summary
        summary = self.get_performance_summary()
        if summary.get("total_trades", 0) > 0:
            lines.append("## Trading Performance (Self-Learning Data)")
            lines.append(f"- Total trades: {summary['total_trades']}")
            lines.append(f"- Win rate: {summary['win_rate_pct']}%")
            lines.append(f"- Average PnL: {summary['avg_pnl_pct']}%")
            lines.append(f"- Best trade: +{summary['best_trade_pnl']}%")
            lines.append(f"- Worst trade: {summary['worst_trade_pnl']}%")

            # Top/worst pairs
            pairs = summary.get("pair_performance", {})
            if pairs:
                sorted_pairs = sorted(pairs.items(), key=lambda x: x[1]["total_pnl"], reverse=True)
                if sorted_pairs:
                    lines.append(f"- Best pair: {sorted_pairs[0][0]} (PnL: {sorted_pairs[0][1]['total_pnl']:.1f}%)")
                    lines.append(f"- Worst pair: {sorted_pairs[-1][0]} (PnL: {sorted_pairs[-1][1]['total_pnl']:.1f}%)")
            lines.append("")

        # Custom strategies
        if self.custom_strategies:
            lines.append("## Your Custom Strategies (Rules You've Learned)")
            for s in self.custom_strategies[-10:]:  # Last 10
                lines.append(f"- [{s['confidence'].upper()}] {s['rule']}")
            lines.append("")

        # Recent decisions
        recent_decisions = self.decisions[-5:]
        if recent_decisions:
            lines.append("## Recent Decisions")
            for d in recent_decisions:
                confirmed = "✅" if d.get("confirmed") else "❌"
                lines.append(f"- {d['pair'].upper()} {d['direction']} {confirmed} ({d.get('timestamp', '')[:16]})")
            lines.append("")

        return "\n".join(lines) if lines else ""

    def save_conversation_summary(self, summary: str) -> None:
        """Save a brief summary of a conversation for long-term context."""
        self.conversation_summaries.append({
            "timestamp": datetime.now().isoformat(),
            "summary": summary
        })
        self.save()


# Global singleton
agent_memory = AgentMemory()
