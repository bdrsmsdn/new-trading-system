"""Crypto News & AI-Driven Sentiment Indicator.

Powered by NS3.AI feeds (trusted by Binance News & CoinGecko).
Provides real-time news retrieval, automated sentiment scoring,
and safety circuit-breakers for the autonomous trading engine.
"""

import time
import re
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional, List, Dict, Tuple
from hermes.logging_setup import log

# In-memory cache for news sentiment to avoid hammering the API
# Structure: {cache_key: (timestamp, data)}
_NEWS_CACHE: Dict[str, Tuple[float, List[Dict]]] = {}
_SENTIMENT_CACHE: Dict[str, Tuple[float, Dict]] = {}
CACHE_TTL = 180  # 3 minutes cache

BULLISH_KEYWORDS = {
    "surge", "surges", "surging", "climb", "climbs", "climbing", "rally", "rallies",
    "rallying", "record", "high", "inflow", "inflows", "gain", "gains", "jump", "jumps",
    "soar", "soars", "bullish", "adoption", "partner", "partnership", "launch",
    "launched", "upgrade", "integrates", "approved", "approval", "outperform",
    "expansion", "accumulation", "all-time high", "ath", "listing", "listed"
}

BEARISH_KEYWORDS = {
    "hack", "hacked", "hacker", "exploit", "exploiter", "exploited", "stolen", "drain",
    "drained", "halt", "halts", "halted", "pause", "pauses", "paused", "drop", "drops",
    "dropping", "plunge", "plunges", "crash", "crashes", "slump", "slumps", "fall",
    "falls", "falling", "ban", "banned", "lawsuit", "sue", "sues", "strike", "strikes",
    "war", "deficit", "reject", "rejected", "fraud", "scam", "sanction", "arrest",
    "shutdown", "depeg", "liquidation", "bearish", "warning", "warns", "outflow",
    "outflows", "dump", "dumping", "insolvent", "bankrupt"
}

EMERGENCY_KEYWORDS = {
    "hack", "hacked", "exploit", "exploiter", "stolen", "depeg",
    "insolvent", "bankrupt", "indictment"
}


def _clean_text(text: str) -> str:
    """Strip XML/HTML tags and normalize whitespace."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    return " ".join(clean.split())


def _fetch_xml(url: str, timeout: int = 8) -> Optional[ET.Element]:
    """Fetch URL and parse XML safely."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            return ET.fromstring(data)
    except Exception as e:
        log.debug(f"[NEWS] Failed to fetch {url}: {e}")
        return None


def fetch_crypto_news(crypto: Optional[str] = None, limit: int = 5, min_level: int = 2) -> List[Dict]:
    """Fetch recent news articles from NS3 feeds.
    
    If crypto is provided, filters for that asset; otherwise fetches top ranking news.
    """
    coin_key = crypto.upper() if crypto else "GLOBAL"
    cache_key = f"{coin_key}_{limit}_{min_level}"
    now = time.time()

    if cache_key in _NEWS_CACHE:
        ts, cached_data = _NEWS_CACHE[cache_key]
        if now - ts < CACHE_TTL:
            return cached_data

    articles = []

    if crypto:
        coin = crypto.upper().replace("USDT", "").replace("-PERP", "")
        # Try coin-specific news feed
        url = f"https://api.ns3.ai/feed/news-data?lang=en&crypto={coin}&excludeLevels=4&limit={limit * 2}"
        root = _fetch_xml(url)
        if root is not None:
            for item in root.findall(".//item"):
                lvl_str = item.findtext("level") or "3"
                try:
                    lvl = int(lvl_str)
                except ValueError:
                    lvl = 3
                if lvl > min_level and min_level <= 2:
                    # If user wanted level 1-2 only, skip level 3+
                    continue

                title = _clean_text(item.findtext("title") or "")
                desc = _clean_text(item.findtext("description") or "")
                pub_date = item.findtext("pubDate") or ""
                link = item.findtext("link") or ""
                coins = (item.findtext("mentionedCoins") or "").split(",")

                if title:
                    articles.append({
                        "title": title,
                        "description": desc,
                        "pub_date": pub_date,
                        "level": lvl,
                        "coins": [c.strip() for c in coins if c.strip()],
                        "link": link
                    })
                if len(articles) >= limit:
                    break

    # If no coin-specific articles found or global requested, fetch top ranking news
    if not articles:
        url = "https://api.ns3.ai/feed/news-ranking?lang=en"
        root = _fetch_xml(url)
        if root is not None:
            for item in root.findall(".//item"):
                title = _clean_text(item.findtext("title") or "")
                desc = _clean_text(item.findtext("description") or "")
                pub_date = item.findtext("pubDate") or ""
                link = item.findtext("link") or ""
                rank_str = item.findtext("rank") or "0"
                coins = (item.findtext("mentionedCoins") or "").split(",")

                try:
                    rank = int(rank_str)
                except ValueError:
                    rank = 0

                if title:
                    articles.append({
                        "title": title,
                        "description": desc,
                        "pub_date": pub_date,
                        "level": 2 if rank > 0 else 3,
                        "rank": rank,
                        "coins": [c.strip() for c in coins if c.strip()],
                        "link": link
                    })
                if len(articles) >= limit:
                    break

    _NEWS_CACHE[cache_key] = (now, articles)
    return articles


def score_news_article(title: str, desc: str = "") -> Tuple[float, List[str], List[str], List[str]]:
    """Score a single news headline/description for sentiment polarity.
    
    Returns: (score, bull_keywords, bear_keywords, emergency_keywords)
    Score range: roughly -1.0 to +1.0
    """
    text = f"{title} {desc}".lower()
    words = set(re.findall(r"\b\w+\b", text))

    bull_hits = list(words.intersection(BULLISH_KEYWORDS))
    bear_hits = list(words.intersection(BEARISH_KEYWORDS))
    em_hits = list(words.intersection(EMERGENCY_KEYWORDS))

    diff = len(bull_hits) - len(bear_hits)
    total = len(bull_hits) + len(bear_hits)

    if total == 0:
        score = 0.0
    else:
        score = max(-1.0, min(1.0, diff / max(total, 2)))

    return score, bull_hits, bear_hits, em_hits


def get_news_sentiment(crypto: Optional[str] = None) -> Dict:
    """Analyze overall sentiment from recent news.
    
    Returns:
        {
            "crypto": "SOL" or "GLOBAL",
            "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL",
            "score": float (-1.0 to 1.0),
            "is_emergency": bool,
            "emergency_reason": str,
            "count": int,
            "articles": List[Dict]
        }
    """
    coin_key = crypto.upper().replace("USDT", "").replace("-PERP", "") if crypto else "GLOBAL"
    now = time.time()

    if coin_key in _SENTIMENT_CACHE:
        ts, cached_result = _SENTIMENT_CACHE[coin_key]
        if now - ts < CACHE_TTL:
            return cached_result

    articles = fetch_crypto_news(crypto=coin_key if coin_key != "GLOBAL" else None, limit=6, min_level=3)
    if not articles:
        result = {
            "crypto": coin_key,
            "sentiment": "NEUTRAL",
            "score": 0.0,
            "is_emergency": False,
            "emergency_reason": "",
            "count": 0,
            "articles": []
        }
        _SENTIMENT_CACHE[coin_key] = (now, result)
        return result

    scores = []
    is_emergency = False
    emergency_reason = ""
    scored_articles = []

    for art in articles:
        sc, bulls, bears, ems = score_news_article(art["title"], art["description"])
        art_with_score = dict(art)
        art_with_score["score"] = sc
        art_with_score["bulls"] = bulls
        art_with_score["bears"] = bears
        scored_articles.append(art_with_score)
        scores.append(sc)

        # Check emergency: level 1 or emergency keywords with negative score
        if art.get("level") == 1 or (len(ems) > 0 and sc < 0):
            is_emergency = True
            if not emergency_reason:
                emergency_reason = art["title"]

    avg_score = sum(scores) / len(scores) if scores else 0.0

    if avg_score >= 0.15:
        sentiment = "BULLISH"
    elif avg_score <= -0.15:
        sentiment = "BEARISH"
    else:
        sentiment = "NEUTRAL"

    result = {
        "crypto": coin_key,
        "sentiment": sentiment,
        "score": round(avg_score, 2),
        "is_emergency": is_emergency,
        "emergency_reason": emergency_reason,
        "count": len(articles),
        "articles": scored_articles
    }

    _SENTIMENT_CACHE[coin_key] = (now, result)
    return result


def is_news_safe_to_buy(pair: str) -> Tuple[bool, str]:
    """Safety filter / circuit-breaker for trading execution.
    
    Verifies that neither global crypto market nor pair-specific news
    has an active systemic emergency or strong bearish sentiment shock.
    """
    coin = pair.upper().replace("USDT", "").replace("-PERP", "")

    # 1. Check global market systemic shocks
    global_senti = get_news_sentiment(None)
    for art in global_senti.get("articles", []):
        # Level 1 is true systemic shock
        if art.get("level") == 1:
            return False, f"Global Level-1 Shock: {art.get('title')}"
        
        # Check for major systemic words (depeg, binance halt, tether)
        title_lower = art.get("title", "").lower()
        if any(term in title_lower for term in ["depeg", "tether halt", "usdt depeg", "usdc depeg", "binance halt", "binance insolv"]):
            return False, f"Systemic Shock: {art.get('title')}"

    if global_senti.get("score", 0) <= -0.50:
        return False, f"Market-wide severe bearish news panic ({global_senti.get('score'):+.2f})"

    # 2. Check coin-specific sentiment
    coin_senti = get_news_sentiment(coin)
    if coin_senti.get("is_emergency"):
        # Check if the emergency article actually mentions or pertains to this coin
        for art in coin_senti.get("articles", []):
            if art.get("score", 0) < 0:
                t_lower = art.get("title", "").lower()
                c_lower = coin.lower()
                if c_lower in t_lower or any(c_lower == c.lower() for c in art.get("coins", [])):
                    if any(w in t_lower for w in EMERGENCY_KEYWORDS):
                        return False, f"{coin} Emergency News: {art.get('title')}"

    if coin_senti.get("score", 0) <= -0.40:
        return False, f"{coin} news sentiment strongly bearish ({coin_senti.get('score'):+.2f})"

    return True, f"News sentiment safe (Global: {global_senti.get('score'):+.2f}, {coin}: {coin_senti.get('score'):+.2f})"


def format_news_telegram(crypto: Optional[str] = None) -> str:
    """Format news & sentiment for Telegram bot display."""
    target = crypto.upper().replace("USDT", "").replace("-PERP", "") if crypto else None
    data = get_news_sentiment(target)

    label = f"*{target}*" if target else "*GLOBAL CRYPTO*"
    sentiment = data["sentiment"]
    score = data["score"]

    s_emoji = "🟢" if sentiment == "BULLISH" else ("🔴" if sentiment == "BEARISH" else "⚪")

    lines = [
        f"📰 {s_emoji} {label} *NEWS SENTIMENT*",
        f"Sentiment: *{sentiment}* ({score:+.2f})",
    ]

    if data.get("is_emergency"):
        lines.append(f"🚨 *ALERT:* _{data.get('emergency_reason')}_")

    lines.append("")
    lines.append("📌 *Top Headlines:*")

    for i, art in enumerate(data.get("articles", [])[:4], 1):
        title = art.get("title", "")
        sc = art.get("score", 0.0)
        sc_icon = "📈" if sc > 0 else ("📉" if sc < 0 else "⚖️")
        lvl = art.get("level", 2)
        lines.append(f"{i}. {sc_icon} [L{lvl}] *{title}*")
        desc = art.get("description", "")
        if desc:
            lines.append(f"   _{desc[:120]}..._")

    return "\n".join(lines)
