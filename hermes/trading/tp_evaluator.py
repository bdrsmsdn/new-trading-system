"""
Hermes Dynamic Take Profit & Momentum Re-evaluation Engine.

When a position reaches the target profit threshold (+10.0% PnL or ROE),
this module evaluates whether to:
1. EXTEND_AND_RIDE: Lock in a guaranteed profit floor (e.g. +8.0%) and trail with a dynamic stop to ride large pumps (+20% to +40%+).
2. TAKE_PROFIT_NOW: Close the position immediately at +10% if momentum is exhausting or sell pressure is heavy.

Powered by multi-timeframe RSI, orderbook imbalance, real-time news sentiment,
and fast AI reasoning via 9router (ag/gemini-3.7-flash-high).
"""

import json
import time
from typing import Dict, Any, Tuple
from hermes.logging_setup import log
from hermes.config import ROUTER_API_KEY, ROUTER_BASE_URL, ROUTER_MODEL
from hermes.state import state, _multi_rsi_cache
from hermes.indicators.rsi import get_rsi, get_multi_rsi
from hermes.api.orderbook import get_orderbook
from hermes.indicators.news_sentiment import get_news_sentiment
from hermes.notifications.telegram import telegram_send


def evaluate_tp_momentum(
    pair: str,
    current_price: float,
    entry_price: float,
    pnl_pct: float,
    is_futures: bool = False,
    leverage: int = 1
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
            "reason": str,
            "sentiment_summary": str,
            "orderbook_imbalance": float,
            "rsi_3m": float
        }
    """
    clean_pair = pair.upper().replace("USDT", "").replace("-PERP", "")
    
    # 1. Gather Technical & Order Flow Data
    # RSI (3m, 1h)
    rsi_3m = 50.0
    rsi_1h = 50.0
    try:
        rsi_3m = get_rsi(clean_pair)
        cached_mrsi = _multi_rsi_cache.get(clean_pair, {})
        if cached_mrsi and "rsi" in cached_mrsi:
            rsi_1h = cached_mrsi["rsi"].get("1h", 50.0)
    except Exception as e:
        log.debug(f"[TP-EVAL] RSI fetch failed: {e}")

    # Orderbook Imbalance
    ob_imbalance = 1.0
    bid_vol = 0.0
    ask_vol = 0.0
    try:
        ob = get_orderbook(clean_pair)
        ob_imbalance = float(ob.get("imbalance", 1.0))
        bid_vol = float(ob.get("bid_volume", 0.0))
        ask_vol = float(ob.get("ask_volume", 0.0))
    except Exception as e:
        log.debug(f"[TP-EVAL] Orderbook fetch failed: {e}")

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

    # 2. Fast AI Decision (Timeout 20s)
    ai_result = None
    if ROUTER_API_KEY:
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
Pair: {clean_pair}USDT {'(Binance Futures ' + str(leverage) + 'x)' if is_futures else '(Binance Spot)'}
Entry Price: ${entry_price:.6f}
Current Price: ${current_price:.6f}
Gain: +{pnl_pct * 100:.1f}%
Market Sentiment: F&G {fg_val} ({fg_cls})

Live Market Data:
- RSI 3m: {rsi_3m:.1f} (1h: {rsi_1h:.1f})
- Orderbook Bid/Ask Imbalance: {ob_imbalance:.2f} (Bids: ${bid_vol:,.0f} vs Asks: ${ask_vol:,.0f})
- Coin News Sentiment: {sent_label} (Score: {sent_score:+.2f})
- Recent Headlines:
{news_str}

DECISION RULES:
1. EXTEND_AND_RIDE: If buyer momentum is strong (Orderbook imbalance >= 1.10, RSI healthy/bullish, no severe bearish news). We will lock profit floor at +8.0% and trail to capture pumps to +20%~+40%+.
2. TAKE_PROFIT_NOW: If orderbook has heavy sell walls (< 0.90), RSI severe overbought divergence (>88 or dropping fast), or negative news catalyst.

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
                log.info(f"[TP-EVAL-AI] {clean_pair} -> {ai_result.get('action')} ({ai_result.get('confidence')}): {ai_result.get('reason')}")
        except Exception as e:
            log.warning(f"[TP-EVAL-AI] AI call failed or timed out ({e}), using technical fallback")

    # 3. Deterministic Technical Fallback (if AI unavailable or failed)
    if not ai_result:
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
        "reason": reason,
        "sentiment_summary": f"{sent_label} ({sent_score:+.2f})" if sent_score != 0 else sent_label,
        "orderbook_imbalance": ob_imbalance,
        "rsi_3m": rsi_3m
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
    leverage: int = 1
) -> None:
    """Send Telegram alert when a position hits +10% and is EXTENDED to ride the trend."""
    clean_pair = pair.upper().replace("-PERP", "")
    type_str = f"FUTURES ({leverage}x)" if is_futures else "SPOT"

    msg = (
        f"🚀 *HERMES DYNAMIC TP EXTENSION — {clean_pair}*\n"
        f"Tipe: *{type_str}* | Target +10.0% Tercapai! 🎯\n"
        f"────────────────────\n"
        f"📈 Gain Saat Ini: *+{pnl_pct * 100:.2f}%*\n"
        f"💵 Entry: *${entry_price:.4f}* → Sekarang: *${current_price:.4f}*\n"
        f"────────────────────\n"
        f"🧠 *AI Decision:* **EXTEND & RIDE THE TREND 🚀**\n"
        f"📊 *Analisis:* {reason}\n"
        f"📰 *Sentimen News:* {sentiment_str}\n"
        f"────────────────────\n"
        f"🔒 *Profit Floor Terkunci:* *+{floor_pct * 100:.1f}%* (${floor_price:.4f})\n"
        f"🛡️ *Trailing Stop:* *{trail_pct * 100:.1f}%* di bawah puncak tertinggi\n"
        f"────────────────────\n"
        f"ℹ️ *Bot menahan posisi karena momentum sangat kuat. Stop loss sudah dinaikkan di atas harga beli (+{floor_pct * 100:.0f}%), sehingga posisi ini dijamin profit sambil mengejar puncak!* 🚀"
    )
    telegram_send(msg)
