import time
import json
from datetime import datetime
from typing import Dict, List
from hermes.logging_setup import log
from hermes.state import state, prices, _ticker_cache
from hermes.config import PRICE_CACHE, ALL_TRACKED, MAX_TRADE_USDT
from hermes.api.rest import fetch_price_rest, fetch_ticker_full, update_price, _check_budget
from hermes.indicators.rsi import get_rsi, get_multi_rsi
from hermes.indicators.signals import get_daily_position, get_signal, get_market_regime
from hermes.indicators.fear_greed import fetch_fear_greed, fetch_polymarket_vibes
from hermes.indicators.strategy_new import get_signal_v2, analyze_pair_v2, StrategyV2
from hermes.api.orderbook import get_orderbook, format_orderbook_summary
from hermes.display.dashboard import print_portfolio_dashboard

def analyze_pair(pair: str, force_refresh: bool = False) -> Dict:
    """Analyze a single pair. Budget-aware — skips REST if budget exhausted."""
    cached = prices.get(pair, {})
    price = None
    if not force_refresh:
        age = time.time() - cached.get("ts", cached.get("updated", 0))
        if age < 120 and cached.get("price"):
            price = cached["price"]

    if not price:
        price = fetch_price_rest(pair)  # Has internal WS-first + budget guard
        if not price:
            return {}
        update_price(pair, price)

    rsi = get_rsi(pair)
    daily_pos = get_daily_position(pair, price)  # WS-only now, no REST

    # Only fetch multi-RSI if budget allows
    if _check_budget():
        multi_rsi = get_multi_rsi(pair, price)
    else:
        multi_rsi = {"3m": rsi, "1h": 50.0, "4h": 50.0}

    signal, score, reasons = get_signal(pair, price, multi_rsi)

    # Use WS high/low or ticker cache — fetch_ticker_full already does this
    ws_data = prices.get(pair, {})
    high_24h = ws_data.get("high")
    low_24h = ws_data.get("low")
    if not high_24h:
        tc = _ticker_cache.get(pair, {})
        high_24h = tc.get("high")
        low_24h = tc.get("low")

    return {
        "pair": pair,
        "price": price,
        "rsi": rsi,
        "multi_rsi": multi_rsi,
        "daily_position": daily_pos,
        "signal": signal,
        "score": score,
        "reasons": reasons,
        "high_24h": high_24h,
        "low_24h": low_24h,
    }

def print_analysis(get_balance_func, pair=None) -> List[Dict]:
    """Print market analysis with portfolio dashboard. Budget-aware."""
    try:
        if PRICE_CACHE.exists():
            cached = json.loads(PRICE_CACHE.read_text())
            for p, data in cached.items():
                prices[p] = data
                if p not in state.price_history:
                    state.price_history[p] = []
                if data.get("price"):
                    state.price_history[p] = [data["price"]]
    except Exception:
        pass

    # NO separate ticker pre-fetch loop — analyze_pair handles it via WS/cache

    log.info("=" * 60)
    log.info(f"Hermes Trader Analysis — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} WIB")
    log.info("=" * 60)
    
    fg_val, fg_class = fetch_fear_greed()  # Uses requests directly, not Indodax
    regime, regime_desc = get_market_regime()
    regime_emoji = {"BULL": "🐂", "BEAR": "🐻", "SIDEWAYS": "↔️"}.get(regime, "?")
    log.info(f"Fear & Greed: {fg_val} ({fg_class}) {regime_emoji} {regime}")
    
    if not pair:
        vibes = fetch_polymarket_vibes()
        if vibes:
            log.info("\n🔮 Polymarket Vibes:")
            for v in vibes:
                pct = v["yes_price"] * 100
                log.info(f"  • {v['question'][:60]}...")
                log.info(f"    YES: {pct:.0f}% | NO: {100-pct:.0f}%")
        else:
            log.info("\n🔮 Polymarket Vibes: unavailable")
    
    balance = get_balance_func(use_cache=False)
    log.info(f"IDR Balance: Rp {balance.get('idr', 0):,.0f}")
    
    from hermes.api.rest import get_rest_budget_status
    budget = get_rest_budget_status()
    log.info(f"REST Budget: {budget['remaining']}/{budget['max']} remaining")
    
    log.info("\n📊 Pair Analysis:")
    log.info("-" * 60)
    
    all_analyses = []
    
    if pair:
        priority_pairs = [pair]
    else:
        priority_pairs = list(state.active_pairs) + [p for p in state.positions if p not in state.active_pairs]
        for p in ALL_TRACKED:
            if p not in priority_pairs:
                priority_pairs.append(p)
    
    for p in priority_pairs:
        analysis = analyze_pair(p)
        if analysis:
            all_analyses.append(analysis)
    
    all_analyses.sort(key=lambda x: x["score"], reverse=True)
    
    log.info("\n🎯 TOP SIGNALS:")
    shown = 0
    for analysis in all_analyses:
        if analysis["signal"] in ["STRONG_BUY", "BUY", "WEAK_BUY", "SELL", "STRONG_SELL"]:
            log.info(f"\n{analysis['pair'].upper():8} {analysis['signal']:12} (score: {analysis['score']:+d})")
            log.info(f"  Price: Rp {analysis['price']:>15,.0f}  RSI(3): {analysis['rsi']:.1f}  Daily Pos: {analysis['daily_position']:.0f}%")
            log.info(f"  Reasons: {', '.join(analysis['reasons'])}")
            if analysis.get('high_24h'):
                log.info(f"  24h Range: Rp {analysis['low_24h']:,.0f} - {analysis['high_24h']:,.0f}")
            shown += 1
    if shown == 0:
        log.info("  No strong signals (all HOLD)")
    
    log.info("\n📋 FULL RANKING:")
    log.info(f"{'PAIR':<8} {'SIGNAL':<12} {'SCORE':>6}  {'PRICE':>15}  {'RSI':>6}  {'DPOS':>5}")
    log.info("-" * 70)
    for analysis in all_analyses:
        emoji = {"STRONG_BUY": "🟢+", "BUY": "🟢", "WEAK_BUY": "🟡", 
                "HOLD": "⚪", "SELL": "🔴", "STRONG_SELL": "🔴-"}.get(analysis["signal"], "?")
        log.info(f"{analysis['pair'].upper():8} {emoji} {analysis['signal']:<9} {analysis['score']:+4d}  "
                f"Rp {analysis['price']:>14,.0f}  {analysis['rsi']:>5.1f}  {analysis['daily_position']:>5.0f}%")
    
    if state.positions:
        log.info("\n📁 Open Positions:")
        for pair_name, pos in state.positions.items():
            current = prices.get(pair_name, {}).get("price", 0)
            pnl_pct = (current - pos["entry_price"]) / pos["entry_price"] * 100 if current else 0
            log.info(f"  {pair_name.upper()}: Entry Rp {pos['entry_price']:,.0f} | "
                    f"Current Rp {current:,.0f} ({pnl_pct:+.1f}%) | "
                    f"Peak Rp {pos.get('peak_price', pos['entry_price']):,.0f} | "
                    f"SL: Rp {pos['stop_loss']:,.0f} | TP: Rp {pos['take_profit']:,.0f}")
    
    if not pair:
        print_portfolio_dashboard(get_balance_func)

    budget = get_rest_budget_status()
    log.info(f"\n📡 REST Budget after analysis: {budget['remaining']}/{budget['max']} remaining")    
    log.info("\n" + "=" * 60)
    return all_analyses


# ─────────────────────────────────────────────────────────────────────────────
# Strategy V2 Analysis (RSI + EMA Crossover + Orderbook)
# ─────────────────────────────────────────────────────────────────────────────

def analyze_pair_v2_display(pair: str, capital: float = 0.0) -> Dict:
    """Analyze a single pair using Strategy V2 (RSI + EMA + Orderbook).
    
    Args:
        pair: Trading pair
        capital: Available capital in IDR
    
    Returns:
        Dict with complete signal data
    """
    result = analyze_pair_v2(pair, capital)
    
    # Add price from cache if not in signal
    if not result.get("signal", {}).get("entry_price"):
        cached = prices.get(pair, {})
        price = cached.get("price")
        if price:
            result["price"] = price
        else:
            result["price"] = fetch_price_rest(pair)
    
    return result


def print_analysis_v2(get_balance_func, pair: str = None) -> List[Dict]:
    """Run Strategy V2 analysis (RSI + EMA + Orderbook).
    
    Args:
        get_balance_func: Function to get balance
        pair: Specific pair to analyze, or None for all
    
    Returns:
        List of analysis results
    """
    # Load cached prices
    try:
        if PRICE_CACHE.exists():
            cached = json.loads(PRICE_CACHE.read_text())
            for p, data in cached.items():
                prices[p] = data
    except Exception:
        pass
    
    # Get balance for position sizing
    balance = get_balance_func(use_cache=True)
    capital = balance.get("usdt", MAX_TRADE_USDT)
    
    log.info("=" * 70)
    log.info(f"STRATEGY V2 ANALYSIS — RSI+EMA Cross + Orderbook")
    log.info(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} WIB")
    log.info("=" * 70)
    
    # Determine pairs to analyze
    if pair:
        pairs_to_analyze = [pair]
    else:
        pairs_to_analyze = list(state.active_pairs) + [p for p in state.positions if p not in state.active_pairs]
        for p in ALL_TRACKED:
            if p not in pairs_to_analyze:
                pairs_to_analyze.append(p)
    
    results = []
    
    for p in pairs_to_analyze:
        try:
            analysis = analyze_pair_v2_display(p, capital)
            if analysis:
                results.append(analysis)
        except Exception as e:
            log.debug(f"Error analyzing {p}: {e}")
    
    # Sort by signal confidence
    def signal_priority(r):
        sig = r.get("signal", {}).get("signal_type", "NO TRADE SETUP")
        conf = r.get("signal", {}).get("signal_confidence", "Low")
        if sig == "LONG":
            return (0, -["Low", "Medium", "High"].index(conf) if conf in ["Low", "Medium", "High"] else 1)
        elif sig == "SHORT":
            return (1, -["Low", "Medium", "High"].index(conf) if conf in ["Low", "Medium", "High"] else 1)
        return (2, 0)
    
    results.sort(key=signal_priority)
    
    # Display results
    log.info(f"\n📊 V2 Signals ({len(results)} pairs analyzed):")
    log.info("-" * 70)
    
    for r in results:
        sig_data = r.get("signal", {})
        sig_type = sig_data.get("signal_type", "NO TRADE SETUP")
        pair_name = r.get("pair", "UNKNOWN").upper()
        
        # Signal header
        if sig_type == "LONG":
            emoji = "🟢 LONG"
        elif sig_type == "SHORT":
            emoji = "🔴 SHORT"
        else:
            emoji = "⚪ NO SETUP"
        
        log.info(f"\n{pair_name:8} {emoji}")
        
        if sig_type != "NO TRADE SETUP":
            # Price and entry
            entry = sig_data.get("entry_price", 0)
            sl = sig_data.get("stop_loss", 0)
            tp1 = sig_data.get("take_profit_1", 0)
            tp2 = sig_data.get("take_profit_2", 0)
            tp3 = sig_data.get("take_profit_3", 0)
            
            log.info(f"  Entry: Rp {entry:>14,.4f} | SL: Rp {sl:>14,.4f}")
            log.info(f"  TP1:  Rp {tp1:>14,.4f} (1:1)  TP2: Rp {tp2:>14,.4f} (1:2)")
            log.info(f"  TP3:  Rp {tp3:>14,.4f} (1:3)")
            
            # Indicators
            rsi = sig_data.get("rsi_value", 0)
            ema9 = sig_data.get("ema_9", 0)
            ema21 = sig_data.get("ema_21", 0)
            bias = sig_data.get("trend_bias", "neutral")
            conf = sig_data.get("signal_confidence", "Low")
            
            log.info(f"  RSI: {rsi:>6.1f}  EMA9: {ema9:>12.4f}  EMA21: {ema21:>12.4f}")
            log.info(f"  Bias: {bias:>8} | Confidence: {conf}")
            
            # Orderbook
            ob = r.get("orderbook")
            if ob:
                imb = ob.get("imbalance", 1.0)
                ob_status = "BULLISH" if imb > 1.3 else ("BEARISH" if imb < 0.7 else "NEUTRAL")
                log.info(f"  Orderbook: Imbalance={imb:.2f} ({ob_status})")
            
            # Risk
            risk_pct = sig_data.get("risk_percent", 1.0)
            pos_size = sig_data.get("position_size", 0)
            log.info(f"  Risk: {risk_pct:.1f}% | Position Size: {pos_size:.4f}")
        
        # Reason
        reason = sig_data.get("reason", "No signal")
        log.info(f"  Reason: {reason}")
    
    # Summary
    longs = [r for r in results if r.get("signal", {}).get("signal_type") == "LONG"]
    shorts = [r for r in results if r.get("signal", {}).get("signal_type") == "SHORT"]
    
    log.info("\n" + "-" * 70)
    log.info(f"📈 LONG signals: {len(longs)}  |  📉 SHORT signals: {len(shorts)}")
    log.info("=" * 70)
    
    return results
