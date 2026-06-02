"""
news_fetcher.py — RSS + NewsAPI headlines, filtered for India F&O relevance

Sources:
  - Economic Times Markets RSS
  - Moneycontrol RSS
  - NewsAPI (if key provided)

Returns top 10 relevant headlines as plain strings.
"""
import re
from datetime import datetime, timezone

import feedparser
import requests

from config.settings import NEWS_API_KEY
from config.logger import get_logger

log = get_logger("news_fetcher")

RSS_FEEDS = [
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.livemint.com/rss/markets",
]

# Keywords that make a headline relevant to F&O trading
RELEVANT_KEYWORDS = [
    # Indices
    "nifty", "banknifty", "bank nifty", "sensex", "nifty it", "nifty pharma", "nifty auto",
    "nifty metal", "nifty energy", "nifty private bank",
    # Market participants
    "fii", "dii", "fpi", "mutual fund", "portfolio",
    # Central bank & macro
    "rbi", "inflation", "cpi", "gdp", "iip", "fed", "ecb", "rate cut", "rate hike",
    # Commodities & forex
    "crude", "oil", "natural gas", "gold", "silver", "copper", "zinc", "steel",
    "rupee", "inr", "usd", "dollar", "forex", "currency",
    # Market dynamics
    "vix", "volatility", "expiry", "f&o", "options", "futures", "derivative",
    "oi", "open interest", "delivery", "short covering", "short buildup",
    # Global factors
    "iran", "war", "ceasefire", "geopolitical", "trump", "imf", "world bank",
    # India-specific
    "sgx", "gift nifty", "circuit", "halt", "trading halt", "suspension",
    "election", "budget", "fiscal", "repo", "msf", "monsoon", "quarterly", "earnings",
    # Sectors (stocks)
    "it", "pharma", "finance", "bank", "fmcg", "auto", "metal", "energy", "realty",
    # Technical
    "support", "resistance", "breakout", "reversal", "chart", "trend",
]

def _is_relevant(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in RELEVANT_KEYWORDS)


def fetch_rss_headlines() -> list[str]:
    headlines = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:20]:
                title = entry.get("title", "").strip()
                if title and _is_relevant(title):
                    headlines.append(title)
        except Exception as e:
            log.warning("RSS feed error (%s): %s", url, e)
    return headlines


def fetch_newsapi_headlines() -> list[str]:
    if not NEWS_API_KEY:
        return []
    try:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": "NSE OR \"Indian market\" OR Nifty OR BankNifty OR crude OR RBI OR rupee OR \"F&O\" OR options OR futures OR stocks",
                "language": "en",
                "sortBy": "publishedAt",
                "pageSize": 30,
                "apiKey": NEWS_API_KEY,
            },
            timeout=8,
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        return [
            a["title"]
            for a in articles
            if a.get("title") and _is_relevant(a["title"])
        ]
    except Exception as e:
        log.warning("NewsAPI error: %s", e)
        return []


import time as _time
_HEADLINES_TTL_SEC = 120
_headlines_cache: tuple[list[str], float] | None = None


def get_top_headlines(limit: int = 10) -> list[str]:
    """Return up to `limit` relevant headlines from all sources, deduplicated.

    Cached ~120s so event-driven generation doesn't refetch RSS every scan.
    """
    global _headlines_cache
    if _headlines_cache and (_time.time() - _headlines_cache[1]) < _HEADLINES_TTL_SEC:
        return _headlines_cache[0][:limit]
    all_headlines = fetch_rss_headlines() + fetch_newsapi_headlines()
    seen = set()
    unique = []
    for h in all_headlines:
        norm = re.sub(r"\s+", " ", h).strip().lower()
        if norm not in seen:
            seen.add(norm)
            unique.append(h)
    if unique:
        _headlines_cache = (unique, _time.time())
    result = unique[:limit]
    log.info("Fetched %d relevant headlines", len(result))
    return result
