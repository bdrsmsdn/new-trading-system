import time
import json
from datetime import datetime
from typing import Dict, List
from hermes.logging_setup import log
from hermes.state import state, prices, _ticker_cache
from hermes.config import PRICE_CACHE, ALL_TRACKED
from hermes.api.rest import fetch_price_rest, fetch_ticker_full, update_price, _check_budget
from hermes.indicators.rsi import get_rsi, get_multi_rsi
from hermes.indicators.signals import get_daily_position, get_signal, get_market_regime
from hermes.indicators.fear_greed import fetch_fear_greed, fetch_polymarket_vibes
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
