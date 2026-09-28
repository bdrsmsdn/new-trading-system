"""
Deterministic Continuation Policy & Advisory AI Validation Engine.

Implements normative contracts defined in docs/trading-risk-contract.md:
- Pure deterministic continuation policy:
  * Hard data-quality, emergency, and risk gates strictly precede AI.
  * AI cannot turn a rejected gate into EXTEND, cannot widen active stops, and cannot set monetary parameters.
  * Directional symmetry: LONG support requires bullish structure; SHORT support requires bearish structure.
  * Schema validation for AI output: action enum ('TAKE_PROFIT_NOW', 'EXTEND' / 'EXTEND_AND_RIDE'),
    bounded confidence, bounded reason string. Reject NaN, infinity, negative numbers, out-of-range values.
  * Fail-closed default: if continuation data is missing or invalid, default to no extension.
- Prompt defense: treat external news headlines as raw untrusted data in quotes/tags, never as execution instructions.
"""

from __future__ import annotations

import html
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple, Union

from hermes.logging_setup import log
from hermes.trading.momentum_snapshot import (
    MomentumSnapshot,
    PositionSide,
    is_continuation_eligible,
)

# Constants & Invariants
DEFAULT_PROFIT_FLOOR_PCT = 0.08      # Fixed +8.0% minimum profit trigger
DEFAULT_TRAIL_PCT = 0.035            # 3.5% trailing stop
MIN_TRAIL_PCT = 0.01                 # 1.0% minimum trail stop
MAX_TRAIL_PCT = 0.08                 # 8.0% maximum trail stop
MAX_REASON_LENGTH = 500              # Maximum character length for explanation

ContinuationAction = Literal["EXTEND_AND_RIDE", "TAKE_PROFIT_NOW"]
ContinuationConfidence = Literal["HIGH", "MEDIUM", "LOW"]
ContinuationSource = Literal[
    "DETERMINISTIC",
    "AI_ADVISORY",
    "DETERMINISTIC_FALLBACK",
    "EMERGENCY_VETO",
    "DATA_QUALITY_FAIL_CLOSED",
]


@dataclass(frozen=True)
class ContinuationDecision:
    """
    Immutable, validated continuation decision payload.
    Separates deterministic policy results from advisory AI input and records provenance.
    """
    action: ContinuationAction
    confidence: ContinuationConfidence
    recommended_trail_pct: float
    guaranteed_floor_pct: float
    profit_floor_trigger_pct: float
    reason: str
    deterministic_action: ContinuationAction
    ai_action: Optional[ContinuationAction]
    gate_passed: bool
    gate_failure_reason: Optional[str]
    veto_applied: bool
    veto_reason: Optional[str]
    source: ContinuationSource
    snapshot_id: str
    ai_validation_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert decision to standardized dictionary for downstream consumers."""
        return {
            "action": self.action,
            "confidence": self.confidence,
            "recommended_trail_pct": self.recommended_trail_pct,
            "guaranteed_floor_pct": self.guaranteed_floor_pct,
            "profit_floor_trigger_pct": self.profit_floor_trigger_pct,
            "reason": self.reason,
            "deterministic_action": self.deterministic_action,
            "ai_action": self.ai_action,
            "gate_passed": self.gate_passed,
            "gate_failure_reason": self.gate_failure_reason,
            "veto_applied": self.veto_applied,
            "veto_reason": self.veto_reason,
            "source": self.source,
            "snapshot_id": self.snapshot_id,
            "ai_validation_error": self.ai_validation_error,
        }


def format_prompt_headlines(headlines: Sequence[str]) -> str:
    """
    Format and sanitize external headlines into safe XML-tagged data block for LLM prompts.
    Defends against prompt injection by escaping tags and isolating untrusted content.
    """
    if not headlines:
        return "<untrusted_external_headlines>\nNo recent news headlines available.\n</untrusted_external_headlines>"

    cleaned_lines = []
    for h in headlines[:5]:
        if not h or not isinstance(h, str):
            continue
        # Strip newlines, trim length, escape xml/html characters
        sanitized = re.sub(r"[\r\n]+", " ", h.strip())
        # Prevent breakout of the xml block
        sanitized = sanitized.replace("</untrusted_external_headlines>", "[TAG_FILTERED]")
        sanitized = sanitized.replace("<untrusted_external_headlines>", "[TAG_FILTERED]")
        sanitized = html.escape(sanitized, quote=True)
        if len(sanitized) > 200:
            sanitized = sanitized[:197] + "..."
        cleaned_lines.append(f"- {sanitized}")

    content = "\n".join(cleaned_lines) if cleaned_lines else "No recent news headlines available."
    return f"<untrusted_external_headlines>\n{content}\n</untrusted_external_headlines>"


def validate_ai_advisory_payload(raw_payload: Any) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """
    Strict schema validation for AI advisory responses.
    
    Checks:
    - JSON parsing / dict type.
    - Action enum: strictly in ('EXTEND', 'EXTEND_AND_RIDE', 'TAKE_PROFIT_NOW').
    - Confidence enum: 'HIGH', 'MEDIUM', 'LOW' (or calibrated numeric map).
    - Recommended trail pct: finite float, strictly positive, bounded in [MIN_TRAIL_PCT, MAX_TRAIL_PCT].
    - Reason: string bounded to MAX_REASON_LENGTH chars.
    
    Returns:
        (True, validated_dict, "OK") or (False, None, error_code)
    """
    if raw_payload is None:
        return False, None, "EMPTY_PAYLOAD"

    payload = raw_payload
    if isinstance(payload, str):
        cleaned_str = payload.strip()
        if "```json" in cleaned_str:
            cleaned_str = cleaned_str.split("```json")[1].split("```")[0].strip()
        elif "```" in cleaned_str:
            cleaned_str = cleaned_str.split("```")[1].split("```")[0].strip()

        try:
            payload = json.loads(cleaned_str)
        except Exception as e:
            return False, None, f"MALFORMED_JSON: {e}"

    if not isinstance(payload, dict):
        return False, None, f"PAYLOAD_NOT_A_DICT: {type(payload).__name__}"

    # 1. Action validation & canonicalization
    raw_action = payload.get("action")
    if not isinstance(raw_action, str):
        return False, None, "MISSING_OR_INVALID_ACTION_TYPE"

    action_clean = raw_action.strip().upper()
    if action_clean == "EXTEND":
        action_clean = "EXTEND_AND_RIDE"

    if action_clean not in ("EXTEND_AND_RIDE", "TAKE_PROFIT_NOW"):
        return False, None, f"INVALID_ACTION: {action_clean}"

    # 2. Confidence validation
    raw_confidence = payload.get("confidence", "MEDIUM")
    confidence_clean: ContinuationConfidence = "MEDIUM"
    if isinstance(raw_confidence, str):
        c_upper = raw_confidence.strip().upper()
        if c_upper in ("HIGH", "MEDIUM", "LOW"):
            confidence_clean = c_upper  # type: ignore
        else:
            confidence_clean = "MEDIUM"
    elif isinstance(raw_confidence, (int, float)) and not isinstance(raw_confidence, bool):
        if not math.isnan(raw_confidence) and not math.isinf(raw_confidence):
            if raw_confidence >= 0.8:
                confidence_clean = "HIGH"
            elif raw_confidence >= 0.5:
                confidence_clean = "MEDIUM"
            else:
                confidence_clean = "LOW"
    else:
        confidence_clean = "MEDIUM"

    # 3. Recommended Trail Pct validation (Finite, strictly positive, within bounds)
    raw_trail = payload.get("recommended_trail_pct", DEFAULT_TRAIL_PCT)
    if isinstance(raw_trail, bool) or not isinstance(raw_trail, (int, float)):
        return False, None, f"INVALID_TRAIL_TYPE: {type(raw_trail).__name__}"

    trail_float = float(raw_trail)
    if math.isnan(trail_float) or math.isinf(trail_float):
        return False, None, "NON_FINITE_TRAIL"

    if trail_float <= 0.0 or trail_float < MIN_TRAIL_PCT or trail_float > MAX_TRAIL_PCT:
        return False, None, f"INVALID_TRAIL_RANGE: {trail_float} outside [{MIN_TRAIL_PCT}, {MAX_TRAIL_PCT}]"

    # 4. Reason validation & bounding
    raw_reason = payload.get("reason", "AI continuation evaluation completed.")
    if not isinstance(raw_reason, str):
        raw_reason = str(raw_reason)

    clean_reason = re.sub(r"[\r\n]+", " ", raw_reason.strip())
    if len(clean_reason) > MAX_REASON_LENGTH:
        clean_reason = clean_reason[:MAX_REASON_LENGTH]

    return True, {
        "action": action_clean,
        "confidence": confidence_clean,
        "recommended_trail_pct": trail_float,
        "reason": clean_reason,
    }, "OK"


def evaluate_deterministic_continuation(
    snapshot: MomentumSnapshot,
    sentiment_data: Optional[Dict[str, Any]] = None,
) -> ContinuationDecision:
    """
    Pure deterministic evaluation of momentum gates and market structure.
    
    Gates:
    1. Data Quality Gate (Orderbook freshness, non-crossed, positive price, RSI freshness).
    2. Emergency & Sentiment Risk Veto Gate (Emergency news catalyst or severe adverse sentiment).
    3. Directional Symmetry Gate:
       - LONG requires bullish book imbalance and bullish RSI structure.
       - SHORT requires bearish book imbalance and bearish RSI structure.
    """
    # 1. Hard Data Quality Gate
    eligible, reason_code = is_continuation_eligible(snapshot)
    if not eligible:
        return ContinuationDecision(
            action="TAKE_PROFIT_NOW",
            confidence="MEDIUM",
            recommended_trail_pct=0.025,
            guaranteed_floor_pct=DEFAULT_PROFIT_FLOOR_PCT,
            profit_floor_trigger_pct=DEFAULT_PROFIT_FLOOR_PCT,
            reason=f"Data pasar tidak lengkap atau kadaluarsa ({reason_code}), amankan keuntungan +10%.",
            deterministic_action="TAKE_PROFIT_NOW",
            ai_action=None,
            gate_passed=False,
            gate_failure_reason=reason_code,
            veto_applied=False,
            veto_reason=None,
            source="DATA_QUALITY_FAIL_CLOSED",
            snapshot_id=snapshot.snapshot_id,
        )

    # 2. Hard Emergency & Sentiment Risk Gate
    sent_dict = sentiment_data or {}
    is_emergency = bool(sent_dict.get("is_emergency", False))
    sent_label = str(sent_dict.get("sentiment", "NEUTRAL")).strip().upper()
    sent_score = float(sent_dict.get("score", 0.0))

    if is_emergency:
        return ContinuationDecision(
            action="TAKE_PROFIT_NOW",
            confidence="HIGH",
            recommended_trail_pct=0.025,
            guaranteed_floor_pct=DEFAULT_PROFIT_FLOOR_PCT,
            profit_floor_trigger_pct=DEFAULT_PROFIT_FLOOR_PCT,
            reason="Kondisi darurat pasar terdeteksi (Emergency News / Sentiment Veto), amankan profit segera.",
            deterministic_action="TAKE_PROFIT_NOW",
            ai_action=None,
            gate_passed=False,
            gate_failure_reason="EMERGENCY_NEWS_VETO",
            veto_applied=True,
            veto_reason="EMERGENCY_NEWS_VETO",
            source="EMERGENCY_VETO",
            snapshot_id=snapshot.snapshot_id,
        )

    # Extract Imbalance and RSI
    imbalance = 1.0
    if snapshot.bid_base_qty and snapshot.ask_base_qty:
        try:
            b_qty = float(snapshot.bid_base_qty)
            a_qty = float(snapshot.ask_base_qty)
            if a_qty > 0:
                imbalance = b_qty / a_qty
        except (ValueError, TypeError):
            pass
    elif snapshot.bid_quote_notional and snapshot.ask_quote_notional:
        try:
            b_not = float(snapshot.bid_quote_notional)
            a_not = float(snapshot.ask_quote_notional)
            if a_not > 0:
                imbalance = b_not / a_not
        except (ValueError, TypeError):
            pass

    rsi_3m: Optional[float] = None
    for tf in snapshot.timeframe_series:
        if tf.timeframe == "3m" and tf.values:
            try:
                rsi_3m = float(tf.values[0])
            except (ValueError, TypeError, IndexError):
                pass

    rsi_str = f"{rsi_3m:.1f}" if rsi_3m is not None else "N/A"
    side: PositionSide = snapshot.side

    # 3. Directional Momentum Evaluation
    if side == "SHORT":
        # Bearish continuation criteria for SHORT
        is_bearish_ob = imbalance <= 0.89
        is_healthy_rsi_short = (rsi_3m is not None and 12.0 <= rsi_3m <= 48.0)
        is_not_bullish_news = (sent_label != "BULLISH" and sent_score < 0.5)

        if (is_bearish_ob and is_healthy_rsi_short and is_not_bullish_news) or (imbalance <= 0.75 and is_not_bullish_news):
            action: ContinuationAction = "EXTEND_AND_RIDE"
            confidence: ContinuationConfidence = "HIGH" if imbalance < 0.70 else "MEDIUM"
            reason = (
                f"Orderbook imbalance bearish ({imbalance:.2f}x) dan momentum RSI "
                f"({rsi_str}) menunjukkan daya dorong turun SHORT masih berlanjut."
            )
        else:
            action = "TAKE_PROFIT_NOW"
            confidence = "MEDIUM"
            reason = (
                f"Tekanan jual SHORT mulai melemah (Orderbook {imbalance:.2f}x, "
                f"RSI {rsi_str}), amankan keuntungan 10%."
            )
    else:
        # Bullish continuation criteria for LONG
        is_bullish_ob = imbalance >= 1.12
        is_healthy_rsi = (rsi_3m is not None and 52.0 <= rsi_3m <= 88.0)
        is_not_bearish_news = (sent_label != "BEARISH" and sent_score > -0.5)

        if (is_bullish_ob and is_healthy_rsi and is_not_bearish_news) or (imbalance >= 1.30 and is_not_bearish_news):
            action = "EXTEND_AND_RIDE"
            confidence = "HIGH" if imbalance > 1.35 else "MEDIUM"
            reason = (
                f"Orderbook imbalance sangat kuat ({imbalance:.2f}x) dan momentum RSI "
                f"({rsi_str}) menunjukkan daya dorong beli masih berlanjut."
            )
        else:
            action = "TAKE_PROFIT_NOW"
            confidence = "MEDIUM"
            reason = (
                f"Momentum beli mulai seimbang/menurun (Orderbook {imbalance:.2f}x, "
                f"RSI {rsi_str}), amankan keuntungan 10%."
            )

    return ContinuationDecision(
        action=action,
        confidence=confidence,
        recommended_trail_pct=DEFAULT_TRAIL_PCT if action == "EXTEND_AND_RIDE" else 0.025,
        guaranteed_floor_pct=DEFAULT_PROFIT_FLOOR_PCT,
        profit_floor_trigger_pct=DEFAULT_PROFIT_FLOOR_PCT,
        reason=reason,
        deterministic_action=action,
        ai_action=None,
        gate_passed=True,
        gate_failure_reason=None,
        veto_applied=False,
        veto_reason=None,
        source="DETERMINISTIC",
        snapshot_id=snapshot.snapshot_id,
    )


def decide_continuation(
    snapshot: MomentumSnapshot,
    sentiment_data: Optional[Dict[str, Any]] = None,
    ai_advisory: Optional[Any] = None,
) -> ContinuationDecision:
    """
    Decide continuation by strictly evaluating deterministic gates before considering AI advisory input.
    
    Rules:
    1. Deterministic gates (data quality, emergency risk, directional structure) strictly precede AI.
    2. If hard gates fail, AI advice is discarded / vetoed; decision fails closed to TAKE_PROFIT_NOW.
    3. If hard gates pass and valid AI advisory is provided:
       - AI action is applied.
       - Recommended trail pct is strictly clamped within [MIN_TRAIL_PCT, MAX_TRAIL_PCT].
       - Guaranteed / profit floor trigger pct is ALWAYS locked deterministically at DEFAULT_PROFIT_FLOOR_PCT.
       - AI cannot widen stops or alter monetary parameters.
    4. If AI advisory is invalid or malformed, falls back to deterministic decision without crashing.
    """
    # 1. Deterministic gates first
    det_decision = evaluate_deterministic_continuation(snapshot, sentiment_data)

    # 2. Hard gate failure or emergency veto -> AI CANNOT OVERRIDE
    if not det_decision.gate_passed:
        return det_decision

    # 3. Process AI advisory input if available
    if ai_advisory is not None:
        valid, validated_ai, err_msg = validate_ai_advisory_payload(ai_advisory)
        if not valid or validated_ai is None:
            log.warning(f"[CONTINUATION-POLICY] AI payload validation failed ({err_msg}), falling back to deterministic.")
            return ContinuationDecision(
                action=det_decision.action,
                confidence=det_decision.confidence,
                recommended_trail_pct=det_decision.recommended_trail_pct,
                guaranteed_floor_pct=DEFAULT_PROFIT_FLOOR_PCT,
                profit_floor_trigger_pct=DEFAULT_PROFIT_FLOOR_PCT,
                reason=det_decision.reason,
                deterministic_action=det_decision.action,
                ai_action=None,
                gate_passed=True,
                gate_failure_reason=None,
                veto_applied=False,
                veto_reason=None,
                source="DETERMINISTIC_FALLBACK",
                snapshot_id=snapshot.snapshot_id,
                ai_validation_error=err_msg,
            )

        ai_action = validated_ai["action"]
        ai_confidence = validated_ai["confidence"]
        ai_trail = validated_ai["recommended_trail_pct"]
        # Enforce hard clamping on trail
        clamped_trail = max(MIN_TRAIL_PCT, min(MAX_TRAIL_PCT, ai_trail))
        ai_reason = validated_ai["reason"]

        return ContinuationDecision(
            action=ai_action,
            confidence=ai_confidence,
            recommended_trail_pct=clamped_trail,
            guaranteed_floor_pct=DEFAULT_PROFIT_FLOOR_PCT,
            profit_floor_trigger_pct=DEFAULT_PROFIT_FLOOR_PCT,
            reason=ai_reason,
            deterministic_action=det_decision.action,
            ai_action=ai_action,
            gate_passed=True,
            gate_failure_reason=None,
            veto_applied=False,
            veto_reason=None,
            source="AI_ADVISORY",
            snapshot_id=snapshot.snapshot_id,
        )

    # No AI advisory provided -> use pure deterministic decision
    return det_decision
