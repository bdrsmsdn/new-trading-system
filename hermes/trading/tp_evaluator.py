"""
Hermes Dynamic Take Profit & Momentum Re-evaluation Engine.

When a position reaches the target profit threshold (+10.0% PnL or ROE),
this module evaluates whether to:
1. EXTEND_AND_RIDE: Lock in a profit floor (e.g. +8.0%) and trail with a dynamic stop to ride large trends.
2. TAKE_PROFIT_NOW: Close the position immediately at +10% if momentum is exhausting or sell/buy pressure is heavy.

Powered by multi-timeframe RSI, typed orderbook snapshot, real-time news sentiment,
and fast AI reasoning via 9router.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional, Tuple
from hermes.logging_setup import log
from hermes.config import ROUTER_API_KEY, ROUTER_BASE_URL, ROUTER_MODEL
from hermes.state import state, _multi_rsi_cache
from hermes.indicators.rsi import get_rsi, get_multi_rsi, _MULTI_RSI_TTL
from hermes.api.orderbook import get_orderbook, OrderbookData, _ORDERBOOK_TTL
from hermes.indicators.news_sentiment import get_news_sentiment
from hermes.notifications.telegram import telegram_send
from hermes.utils import format_price
from hermes.trading.momentum_snapshot import (
    MomentumSnapshot,
    PositionSide,
    Venue,
    build_momentum_snapshot,
    canonicalize_symbol,
    is_continuation_eligible,
)


def evaluate_tp_momentum(
    pair: str,
    current_price: float,
    entry_price: float,
    pnl_pct: float,
    is_futures: bool = False,
    leverage: int = 1,
    side: str = "LONG",
    position_lifecycle_id: str = "default",
    position_version: int = 1,
) -> Dict[str, Any]:
    """
    Evaluates market momentum, orderbook, news sentiment, and AI reasoning
    at the +10% Take Profit checkpoint.

    Returns:
        {
            "action": "EXTEND_AND_RIDE" | "TAKE_PROFIT_NOW",
            "confidence": "HIGH" | "MEDIUM" | "LOW",
            "recommended_trail_pct": float (e.g. 0.035),
            "guaranteed_floor_pct": float (e.g. 0.08),
            "profit_floor_trigger_pct": float (e.g. 0.08),
            "reason": str,
            "sentiment_summary": str,
            "orderbook_imbalance": Optional[float],
            "orderbook_status": str,
            "rsi_3m": Optional[float],
            "side": str,
            "snapshot_id": str
        }
    """
    clean_pair = pair.upper().replace("USDT", "").replace("-PERP", "")
    side_normalized: PositionSide = "SHORT" if side.upper() == "SHORT" else "LONG"
    venue: Venue = "FUTURES" if is_futures else "SPOT"
    
    # 1. Gather Technical & Order Flow Data with Freshness / TTL Checks
    # RSI (3m, 1h)
    rsi_3m: Optional[float] = None
    rsi_1h: Optional[float] = None
    try:
        r3 = get_rsi(clean_pair)
        if r3 is not None and isinstance(r3, (int, float)):
            rsi_3m = float(r3)
        cached_mrsi = _multi_rsi_cache.get(clean_pair, {})
        if cached_mrsi and isinstance(cached_mrsi, dict):
            cached_ts = cached_mrsi.get("ts", 0)
            if (time.time() - cached_ts) <= _MULTI_RSI_TTL and "rsi" in cached_mrsi:
                r1 = cached_mrsi["rsi"].get("1h")
                if r1 is not None and isinstance(r1, (int, float)):
                    rsi_1h = float(r1)
    except Exception as e:
        log.debug(f"[TP-EVAL] RSI fetch failed: {e}")

    # Orderbook Imbalance & Base Quantity vs Quote Notional
    ob_imbalance: Optional[float] = None
    bid_vol: float = 0.0
    ask_vol: float = 0.0
    bid_notional: float = 0.0
    ask_notional: float = 0.0
    orderbook_status = "UNKNOWN"
    ob = None

    try:
        ob = get_orderbook(clean_pair, use_cache=True, max_age=_ORDERBOOK_TTL, allow_stale=False)
    except Exception as e:
        log.debug(f"[TP-EVAL] Orderbook fetch failed: {e}")

    if ob is not None:
        if isinstance(ob, OrderbookData):
            if ob.is_fresh(max_age=_ORDERBOOK_TTL):
                ob_imbalance = float(ob.imbalance)
                bid_vol = float(ob.bid_volume)
                ask_vol = float(ob.ask_volume)
                bid_notional = float(getattr(ob, "bid_notional", 0.0))
                ask_notional = float(getattr(ob, "ask_notional", 0.0))
                orderbook_status = "FRESH"
            else:
                orderbook_status = "STALE"
        elif isinstance(ob, dict):
            ob_ts = ob.get("ts", time.time())
            if (time.time() - ob_ts) <= _ORDERBOOK_TTL:
                ob_imbalance = float(ob.get("imbalance", 1.0))
                bid_vol = float(ob.get("bid_volume", 0.0))
                ask_vol = float(ob.get("ask_volume", 0.0))
                bid_notional = float(ob.get("bid_notional", 0.0))
                ask_notional = float(ob.get("ask_notional", 0.0))
                orderbook_status = "FRESH"
            else:
                orderbook_status = "STALE"
    else:
        orderbook_status = "UNKNOWN"

    # Build typed snapshot
    snapshot = build_momentum_snapshot(
        symbol=clean_pair,
        venue=venue,
        side=side_normalized,
        orderbook=ob if orderbook_status == "FRESH" else None,
        rsi_3m=rsi_3m,
        rsi_1h=rsi_1h,
        position_lifecycle_id=position_lifecycle_id,
        position_version=position_version,
    )

    # News & Sentiment
    sentiment_data = {"sentiment": "NEUTRAL", "score": 0.0, "articles": []}
    try:
        sentiment_data = get_news_sentiment(clean_pair)
    except Exception as e:
        log.debug(f"[TP-EVAL] News sentiment fetch failed: {e}")

    sent_label = sentiment_data.get("sentiment", "NEUTRAL")
    sent_score = sentiment_data.get("score", 0.0)
    articles = sentiment_data.get("articles", [])
    news_titles = [a.get("title", "") for a in articles[:3] if a.get("title")]
    news_str = "\n".join([f"- {t}" for t in news_titles]) if news_titles else "Tidak ada berita besar spesifik baru."

    # Fear & Greed
    fg_val = getattr(state, "fg_value", 50)
    fg_cls = getattr(state, "fg_class", "Neutral")

    # Format orderbook text accurately with base asset and quote notional
    if ob_imbalance is not None:
        if bid_notional > 0 and ask_notional > 0:
            ob_text = f"{ob_imbalance:.2f}x (Bids: ${bid_notional:,.0f} USDT [{bid_vol:,.1f} {clean_pair}] vs Asks: ${ask_notional:,.0f} USDT [{ask_vol:,.1f} {clean_pair}])"
        else:
            ob_text = f"{ob_imbalance:.2f}x (Bids: {bid_vol:,.1f} {clean_pair} vs Asks: {ask_vol:,.1f} {clean_pair})"
    else:
        ob_text = f"UNKNOWN / {orderbook_status} (No fresh depth data available)"

    rsi_3m_str = f"{rsi_3m:.1f}" if rsi_3m is not None else "UNKNOWN"
    rsi_1h_str = f"{rsi_1h:.1f}" if rsi_1h is not None else "UNKNOWN"

    # 2. Fast AI Decision (Timeout 20s)
    ai_result = None
    if ROUTER_API_KEY and orderbook_status == "FRESH" and rsi_3m is not None:
        try:
            import openai
            client = openai.OpenAI(
                api_key=ROUTER_API_KEY,
                base_url=ROUTER_BASE_URL,
                timeout=20.0
            )

            system_prompt = (
                "You are the Hermes Autonomous Trading Profit & Momentum Evaluator. "
                "A crypto position has hit its primary +10% target. "
                "Determine whether momentum is strong enough to extend and ride the pump with a profit floor lock, "
                "or if profit should be taken immediately. "
                "Respond ONLY with valid JSON matching the schema."
            )

            user_prompt = f"""
Pair: {clean_pair}USDT {'(Binance Futures ' + str(leverage) + 'x ' + side_normalized + ')' if is_futures else '(Binance Spot ' + side_normalized + ')'}
Entry Price: {format_price(entry_price)}
Current Price: {format_price(current_price)}
Gain: +{pnl_pct * 100:.1f}%
Position Side: {side_normalized}
Market Sentiment: F&G {fg_val} ({fg_cls})

Live Market Data:
- RSI 3m: {rsi_3m_str} (1h: {rsi_1h_str})
- Orderbook Bid/Ask Imbalance: {ob_text}
- Coin News Sentiment: {sent_label} (Score: {sent_score:+.2f})
- Recent Headlines:
{news_str}

DECISION RULES:
1. EXTEND_AND_RIDE: If momentum aligned with position side ({side_normalized}) is strong (For LONG: Orderbook imbalance >= 1.10, RSI healthy/bullish, no severe bearish news. For SHORT: Orderbook imbalance <= 0.90, RSI healthy/bearish, no severe bullish news). We will lock profit floor at +8.0% and trail.
2. TAKE_PROFIT_NOW: If orderbook shows opposing walls, RSI divergence/exhaustion, negative news catalyst, or stale/missing data.

Output JSON format ONLY:
{{
  "action": "EXTEND_AND_RIDE" or "TAKE_PROFIT_NOW",
  "confidence": "HIGH" or "MEDIUM" or "LOW",
  "recommended_trail_pct": 0.035,
  "reason": "1-2 kalimat ringkas dan jelas dalam Bahasa Indonesia mengapa memilih aksi ini"
}}
"""
            resp = client.chat.completions.create(
                model=ROUTER_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.2,
                max_tokens=250
            )
            raw_content = resp.choices[0].message.content.strip()
            # Clean markdown codeblocks if any
            if "```json" in raw_content:
                raw_content = raw_content.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_content:
                raw_content = raw_content.split("```")[1].split("```")[0].strip()

            parsed = json.loads(raw_content)
            if parsed.get("action") in ["EXTEND_AND_RIDE", "TAKE_PROFIT_NOW"]:
                ai_result = parsed
                log.info(f"[TP-EVAL-AI] {clean_pair} ({side_normalized}) -> {ai_result.get('action')} ({ai_result.get('confidence')}): {ai_result.get('reason')}")
        except Exception as e:
            log.warning(f"[TP-EVAL-AI] AI call failed or timed out ({e}), using technical fallback")

    # 3. Deterministic Technical Fallback (if AI unavailable, failed, or data not fresh)
    if not ai_result:
        # Check fail-closed continuation eligibility
        if orderbook_status != "FRESH" or ob_imbalance is None or rsi_3m is None:
            reason_stale = f"Data pasar tidak lengkap atau kadaluarsa (Orderbook: {orderbook_status}, RSI: {rsi_3m_str}), amankan keuntungan +10%."
            ai_result = {
                "action": "TAKE_PROFIT_NOW",
                "confidence": "MEDIUM",
                "recommended_trail_pct": 0.025,
                "reason": reason_stale
            }
        else:
            if side_normalized == "SHORT":
                is_bearish_ob = ob_imbalance <= 0.89
                is_healthy_rsi_short = 12.0 <= rsi_3m <= 48.0
                is_not_bullish_news = sent_label != "BULLISH" and not sentiment_data.get("is_emergency", False)

                if (is_bearish_ob and is_healthy_rsi_short and is_not_bullish_news) or (ob_imbalance <= 0.75 and is_not_bullish_news):
                    ai_result = {
                        "action": "EXTEND_AND_RIDE",
                        "confidence": "HIGH" if ob_imbalance < 0.70 else "MEDIUM",
                        "recommended_trail_pct": 0.035,
                        "reason": f"Orderbook imbalance bearish ({ob_imbalance:.2f}x) dan momentum RSI ({rsi_3m:.1f}) menunjukkan daya dorong turun SHORT masih berlanjut."
                    }
                else:
                    ai_result = {
                        "action": "TAKE_PROFIT_NOW",
                        "confidence": "MEDIUM",
                        "recommended_trail_pct": 0.025,
                        "reason": f"Tekanan jual SHORT mulai melemah (Orderbook {ob_imbalance:.2f}x, RSI {rsi_3m:.1f}), amankan keuntungan 10%."
                    }
            else:
                # LONG position
                is_bullish_ob = ob_imbalance >= 1.12
                is_healthy_rsi = 52.0 <= rsi_3m <= 88.0
                is_not_bearish_news = sent_label != "BEARISH" and not sentiment_data.get("is_emergency", False)

                if (is_bullish_ob and is_healthy_rsi and is_not_bearish_news) or (ob_imbalance >= 1.30 and is_not_bearish_news):
                    ai_result = {
                        "action": "EXTEND_AND_RIDE",
                        "confidence": "HIGH" if ob_imbalance > 1.35 else "MEDIUM",
                        "recommended_trail_pct": 0.035,
                        "reason": f"Orderbook imbalance sangat kuat ({ob_imbalance:.2f}x) dan momentum RSI ({rsi_3m:.1f}) menunjukkan daya dorong beli masih berlanjut."
                    }
                else:
                    ai_result = {
                        "action": "TAKE_PROFIT_NOW",
                        "confidence": "MEDIUM",
                        "recommended_trail_pct": 0.025,
                        "reason": f"Momentum beli mulai seimbang/menurun (Orderbook {ob_imbalance:.2f}x, RSI {rsi_3m:.1f}), amankan keuntungan 10%."
                    }

    # Ensure defaults
    action = ai_result.get("action", "TAKE_PROFIT_NOW")
    confidence = ai_result.get("confidence", "MEDIUM")
    trail_pct = float(ai_result.get("recommended_trail_pct", 0.035))
    reason = ai_result.get("reason", "Evaluasi momentum selesai.")
    floor_pct = 0.08  # Lock +8.0% minimum profit

    return {
        "action": action,
        "confidence": confidence,
        "recommended_trail_pct": trail_pct,
        "guaranteed_floor_pct": floor_pct,
        "profit_floor_trigger_pct": floor_pct,
        "reason": reason,
        "sentiment_summary": f"{sent_label} ({sent_score:+.2f})" if sent_score != 0 else sent_label,
        "orderbook_imbalance": ob_imbalance,
        "orderbook_status": orderbook_status,
        "rsi_3m": rsi_3m,
        "side": side_normalized,
        "snapshot_id": snapshot.snapshot_id,
    }


def send_tp_extension_alert(
    pair: str,
    current_price: float,
    entry_price: float,
    pnl_pct: float,
    floor_price: float,
    floor_pct: float,
    trail_pct: float,
    reason: str,
    sentiment_str: str,
    is_futures: bool = False,
    leverage: int = 1,
    side: str = "LONG",
) -> None:
    """Send Telegram alert when a position hits +10% and is EXTENDED to ride the trend."""
    clean_pair = pair.upper().replace("-PERP", "")
    type_str = f"FUTURES ({leverage}x {side.upper()})" if is_futures else f"SPOT ({side.upper()})"

    msg = (
        f"🚀 *HERMES DYNAMIC TP EXTENSION — {clean_pair}*\n"
        f"Tipe: *{type_str}* | Target +10.0% Tercapai! 🎯\n"
        f"────────────────────\n"
        f"📈 Gain Saat Ini: *+{pnl_pct * 100:.2f}%*\n"
        f"💵 Entry: *{format_price(entry_price)}* → Sekarang: *{format_price(current_price)}*\n"
        f"────────────────────\n"
        f"🧠 *AI Decision:* **EXTEND & RIDE THE TREND 🚀**\n"
        f"📊 *Analisis:* {reason}\n"
        f"📰 *Sentimen News:* {sentiment_str}\n"
        f"────────────────────\n"
        f"🔒 *Profit Floor Terkunci:* *+{floor_pct * 100:.1f}%* ({format_price(floor_price)})\n"
        f"🛡️ *Trailing Stop:* *{trail_pct * 100:.1f}%* di bawah puncak tertinggi\n"
        f"────────────────────\n"
        f"ℹ️ *Bot menahan posisi karena momentum sangat kuat. Stop loss sudah dinaikkan di atas harga beli (+{floor_pct * 100:.0f}%), sehingga posisi ini mengunci profit floor sambil mengejar puncak!* 🚀"
    )
    telegram_send(msg)
