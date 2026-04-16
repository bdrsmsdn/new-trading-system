import time
from typing import Dict
from hermes.logging_setup import log
from hermes.state import state, prices, _ticker_cache

def print_portfolio_dashboard(get_balance_func) -> Dict:
    """Display comprehensive portfolio dashboard and return data.
    Uses WS prices ONLY — no REST fallback to avoid 429."""
    log.info("\n" + "=" * 60)
    log.info("📊 PORTFOLIO DASHBOARD")
    log.info("=" * 60)
    
    balance = get_balance_func(use_cache=False)
    idr_balance = balance.get("idr", 0)
    
    holdings = {}
    for coin, amount in balance.items():
        if coin == "idr" or amount <= 0:
            continue
        holdings[coin] = amount
    
    coin_values = {}
    total_holdings_value = 0

    for coin, amount in holdings.items():
        ws_data = prices.get(coin, {})
        price = ws_data.get("price")
        if price and price > 0:
            value = amount * price
            coin_values[coin] = {
                "amount": amount,
                "price": price,
                "value": value,
                "value_idr": value
            }
            total_holdings_value += value
        else:
            # No WS price available — show as unavailable, do NOT call REST
            coin_values[coin] = {
                "amount": amount,
                "price": 0,
                "value": 0,
                "value_idr": 0
            }
    
    total_portfolio_value = idr_balance + total_holdings_value
    
    log.info(f"\n💰 TOTAL PORTFOLIO VALUE: Rp {total_portfolio_value:,.0f}")
    log.info(f"   IDR Balance:         Rp {idr_balance:,.0f}")
    log.info(f"   Holdings Value:      Rp {total_holdings_value:,.0f}")
    
    position_data = []
    if state.positions:
        log.info("\n📁 OPEN POSITIONS:")
        for pair, pos in state.positions.items():
            current_price_data = coin_values.get(pair, {})
            current_price = current_price_data.get("price")
            if not current_price:
                ws_data = prices.get(pair, {})
                current_price = ws_data.get("price", 0)
            entry = pos["entry_price"]
            qty = pos.get("qty", 0)
            current_value = qty * current_price if current_price else 0
            entry_value = qty * entry
            pnl = current_value - entry_value
            pnl_pct = (current_price - entry) / entry * 100 if entry > 0 and current_price else 0
            
            position_data.append({
                "pair": pair,
                "entry": entry,
                "current": current_price,
                "qty": qty,
                "entry_value": entry_value,
                "current_value": current_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct
            })
        
        position_data.sort(key=lambda x: x["pnl_pct"], reverse=True)
        
        for pos in position_data:
            emoji = "🟢" if pos["pnl_pct"] >= 0 else "🔴"
            pnl_str = f"{pos['pnl_pct']:+.1f}%"
            pnl_val_str = f"{pos['pnl']:+,.0f}"
            log.info(f"  {emoji} {pos['pair'].upper()}: Entry Rp {pos['entry']:,.0f} | "
                    f"Current Rp {pos['current']:,.0f} | "
                    f"Value Rp {pos['current_value']:,.0f} | "
                    f"PnL: {pnl_str} (Rp {pnl_val_str})")
    
    gainers = []
    if coin_values:
        log.info("\n🏆 TOP GAINERS (24h - from price data):")
        for coin, data in coin_values.items():
            if data["price"] > 0:
                ws_data = prices.get(coin, {})
                low = ws_data.get("low")
                if not low:
                    ticker_data = _ticker_cache.get(coin, {})
                    low = ticker_data.get("low") if ticker_data else None
                daily_change = ((data["price"] - low) / low * 100) if low and low > 0 else 0
                gainers.append({
                    "coin": coin,
                    "price": data["price"],
                    "value": data["value"],
                    "daily_change": daily_change
                })
        
        gainers.sort(key=lambda x: x["daily_change"], reverse=True)
        
        for g in gainers[:3]:
            log.info(f"  🟢 {g['coin'].upper()}: Rp {g['price']:,.0f} | "
                    f"Value: Rp {g['value']:,.0f} | "
                    f"24h: {g['daily_change']:+.1f}%")
        
        log.info("\n🔴 TOP LOSERS (24h - from price data):")
        for g in gainers[-3:]:
            log.info(f"  🔴 {g['coin'].upper()}: Rp {g['price']:,.0f} | "
                    f"Value: Rp {g['value']:,.0f} | "
                    f"24h: {g['daily_change']:+.1f}%")
    
    alloc_data = []
    if total_holdings_value > 0:
        log.info("\n📈 ALLOCATION BREAKDOWN:")
        for coin, data in coin_values.items():
            if data["value"] > 0:
                alloc_pct = (data["value"] / total_holdings_value) * 100
                alloc_data.append({
                    "coin": coin,
                    "value": data["value"],
                    "pct": alloc_pct
                })
        
        alloc_data.sort(key=lambda x: x["pct"], reverse=True)
        
        for a in alloc_data:
            bar_len = int(a["pct"] / 2)
            bar = "█" * bar_len
            log.info(f"  {a['coin'].upper():8} Rp {a['value']:>15,.0f}  {a['pct']:5.1f}%  {bar}")
        
        idr_pct = (idr_balance / total_portfolio_value) * 100 if total_portfolio_value > 0 else 0
        bar_len = int(idr_pct / 2)
        bar = "█" * bar_len
        log.info(f"  {'IDR':8} Rp {idr_balance:>15,.0f}  {idr_pct:5.1f}%  {bar}")
    
    log.info("\n" + "=" * 60)
    
    return {
        "total_value": total_portfolio_value,
        "idr_balance": idr_balance,
        "holdings_value": total_holdings_value,
        "positions": position_data,
        "allocations": alloc_data,
        "gainers": gainers
    }
