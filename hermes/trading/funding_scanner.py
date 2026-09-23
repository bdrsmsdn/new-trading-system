"""Binance Futures Funding Rate Scanner.
Scans USDⓈ-M Futures perpetual contracts for high positive funding rates.
Enables delta-neutral cash-and-carry arbitrage (Long Spot + Short Futures 1x).
"""
import urllib.request
import json
from typing import List, Dict

def get_top_funding_rates(limit: int = 5, min_apr: float = 50.0) -> List[Dict]:
    """Fetch top funding rates from Binance Futures."""
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return []

    rates = []
    for item in data:
        symbol = item.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        try:
            last_funding = float(item.get("lastFundingRate", 0))
            apr = last_funding * 3 * 365 * 100
            if apr >= min_apr:
                rates.append({
                    "symbol": symbol,
                    "funding_rate_8h": last_funding * 100,
                    "apr": apr,
                    "mark_price": float(item.get("markPrice", 0))
                })
        except (ValueError, TypeError):
            continue

    rates.sort(key=lambda x: x["funding_rate_8h"], reverse=True)
    return rates[:limit]

if __name__ == "__main__":
    top = get_top_funding_rates()
    print("Top Binance Funding Rate Arbitrage Opportunities:")
    for t in top:
        print(f"- {t['symbol']}: +{t['funding_rate_8h']:.4f}% / 8h (APR: {t['apr']:.1f}%) | Price: ${t['mark_price']}")
