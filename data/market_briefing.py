"""
market_briefing.py -- Daily morning briefing with global cues, prices, FII/DII flows

Collects:
- Global market indices (Nifty, VIX, etc.)
- Commodity prices (Gold, Oil, Crude)
- FII/DII flows
- Market news and sentiment
- AI-powered market outlook
"""
import yfinance as yf
from datetime import datetime
from core.indstocks_auth import get_session
from data.news_fetcher import fetch_rss_headlines
from config.logger import get_logger

log = get_logger("market_briefing")


def fetch_global_prices() -> dict:
    """Fetch gold, oil, crude, and forex prices."""
    prices = {}

    tickers = {
        "gold": "GC=F",
        "crude_wti": "CL=F",
        "crude_brent": "BZ=F",
        "us_10y": "^TNX",
        "dxy": "DX-Y.NYB",
    }

    for key, symbol in tickers.items():
        try:
            data = yf.Ticker(symbol).history(period="5d", interval="1d")
            if not data.empty:
                current = float(data["Close"].iloc[-1])
                prev = float(data["Close"].iloc[-2]) if len(data) > 1 else current
                change = ((current - prev) / prev * 100) if prev != 0 else 0
                prices[key] = {
                    "price": current,
                    "change": change,
                    "direction": "↑" if change > 0 else "↓" if change < 0 else "→"
                }
        except Exception as e:
            log.warning(f"Failed to fetch {key}: {e}")

    return prices


def fetch_market_indices(session) -> dict:
    """Fetch Nifty, BankNifty, VIX from INDstocks."""
    indices = {}

    try:
        resp = session.get(
            "/market/quotes/ltp",
            params={"security_id": "13,25,1", "exchange_segment": "NSE_INDEX"}
        )

        id_to_name = {"13": "NIFTY50", "25": "BANKNIFTY", "1": "VIX"}
        quotes = resp.get("data", [])

        for q in quotes:
            sec_id = str(q.get("security_id", ""))
            name = id_to_name.get(sec_id)
            if name:
                ltp = float(q.get("last_traded_price", 0))
                indices[name] = ltp
    except Exception as e:
        log.warning(f"Failed to fetch indices: {e}")

    return indices


def fetch_fii_dii_flows(session) -> dict:
    """Fetch FII/DII flows if available via INDstocks."""
    flows = {}

    try:
        # Try to fetch FII/DII data from INDstocks
        resp = session.get("/market/fii-dii", params={})
        if resp and isinstance(resp, dict):
            flows = resp
    except Exception as e:
        log.warning(f"FII/DII fetch not available: {e}")

    return flows


def fetch_market_news() -> list:
    """Fetch latest market news headlines."""
    try:
        headlines = fetch_rss_headlines()
        return headlines[:10]  # Top 10 headlines
    except Exception as e:
        log.warning(f"Failed to fetch news: {e}")
        return []


async def get_market_outlook(session) -> str:
    """Get AI-powered market outlook based on current conditions."""
    try:
        from signals.claude_engine import _client, GEMINI_MODEL
        from google import genai

        # Collect current data
        indices = fetch_market_indices(session)
        prices = fetch_global_prices()
        news = fetch_market_news()

        context = f"""
Market Snapshot:
- Nifty50: {indices.get('NIFTY50', 'N/A')}
- BankNifty: {indices.get('BANKNIFTY', 'N/A')}
- VIX: {indices.get('VIX', 'N/A')}

Global Commodities:
- Gold: ${prices.get('gold', {}).get('price', 'N/A')} ({prices.get('gold', {}).get('change', 0):.2f}%)
- Crude WTI: ${prices.get('crude_wti', {}).get('price', 'N/A')} ({prices.get('crude_wti', {}).get('change', 0):.2f}%)
- Crude Brent: ${prices.get('crude_brent', {}).get('price', 'N/A')} ({prices.get('crude_brent', {}).get('change', 0):.2f}%)
- DXY: {prices.get('dxy', {}).get('price', 'N/A')} ({prices.get('dxy', {}).get('change', 0):.2f}%)

Recent Headlines:
{chr(10).join(f"• {h}" for h in news[:5])}

Based on this data, provide a 2-3 line market outlook for today for Indian F&O traders.
Focus on: key support/resistance levels, expected volatility, potential trading opportunities.
Be concise and actionable.
"""

        response = _client.models.generate_content(
            model=GEMINI_MODEL,
            contents=context,
            config=genai.types.GenerateContentConfig(
                max_output_tokens=200,
                temperature=0.3,
            ),
        )

        return response.text
    except Exception as e:
        log.warning(f"Failed to generate market outlook: {e}")
        return "Unable to generate outlook at this time."


def format_midday_briefing(session) -> str:
    """Format mid-day briefing with commodity updates and geopolitical news."""
    try:
        from datetime import datetime

        indices = fetch_market_indices(session)
        prices = fetch_global_prices()
        news = fetch_market_news()

        msg = "<b>💼 MID-DAY MARKET UPDATE</b>\n"
        msg += f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M IST')}</i>\n\n"

        # Current Indices
        msg += "<b>📊 LIVE INDICES</b>\n"
        msg += f"  NIFTY50: {indices.get('NIFTY50', 'N/A')}\n"
        msg += f"  BANKNIFTY: {indices.get('BANKNIFTY', 'N/A')}\n"
        msg += f"  VIX: {indices.get('VIX', 'N/A')}\n\n"

        # Key Commodities
        msg += "<b>🌍 COMMODITY SNAPSHOT</b>\n"

        if "gold" in prices:
            g = prices["gold"]
            msg += f"  🥇 Gold: ${g['price']:.2f} {g['direction']} ({g['change']:+.2f}%)\n"

        if "crude_wti" in prices:
            c = prices["crude_wti"]
            msg += f"  🛢️ Crude WTI: ${c['price']:.2f} {c['direction']} ({c['change']:+.2f}%)\n"

        if "dxy" in prices:
            d = prices["dxy"]
            msg += f"  💵 DXY (USD Index): {d['price']:.2f} {d['direction']} ({d['change']:+.2f}%)\n"

        msg += "\n"

        # Filter for geopolitical and important news
        msg += "<b>🔴 BREAKING NEWS</b>\n"
        important_keywords = ["war", "geopolitical", "rbi", "fed", "iran", "ceasefire", "crisis", "ban", "alert", "surge", "crash", "emergency"]

        important_news = []
        for headline in news:
            if any(kw in headline.lower() for kw in important_keywords):
                important_news.append(headline)

        if important_news:
            for headline in important_news[:5]:  # Top 5 important news
                msg += f"  • {headline}\n"
        else:
            # If no important news, show top general news
            for headline in news[:5]:
                msg += f"  • {headline}\n"

        msg += "\n" + "━" * 60 + "\n"
        msg += "<i>Next briefing at 3:30 PM</i>"

        return msg

    except Exception as e:
        log.error("Error formatting mid-day briefing: %s", e)
        return f"<b>Error creating mid-day briefing:</b> {str(e)}"


def format_briefing(session, outlook: str = "") -> str:
    """Format all briefing data as a Telegram message."""
    indices = fetch_market_indices(session)
    prices = fetch_global_prices()
    news = fetch_market_news()

    msg = "<b>📊 MORNING MARKET BRIEFING</b>\n"
    msg += f"<i>{datetime.now().strftime('%Y-%m-%d %H:%M IST')}</i>\n\n"

    # Market Indices
    msg += "<b>📈 INDICES</b>\n"
    msg += f"  NIFTY50: {indices.get('NIFTY50', 'N/A')}\n"
    msg += f"  BANKNIFTY: {indices.get('BANKNIFTY', 'N/A')}\n"
    msg += f"  VIX: {indices.get('VIX', 'N/A')}\n\n"

    # Global Commodities
    msg += "<b>🌍 GLOBAL COMMODITIES</b>\n"

    if "gold" in prices:
        g = prices["gold"]
        msg += f"  🥇 Gold: ${g['price']:.2f} {g['direction']} ({g['change']:+.2f}%)\n"

    if "crude_wti" in prices:
        c = prices["crude_wti"]
        msg += f"  🛢️ Crude WTI: ${c['price']:.2f} {c['direction']} ({c['change']:+.2f}%)\n"

    if "crude_brent" in prices:
        b = prices["crude_brent"]
        msg += f"  ⛽ Crude Brent: ${b['price']:.2f} {b['direction']} ({b['change']:+.2f}%)\n"

    if "dxy" in prices:
        d = prices["dxy"]
        msg += f"  💵 DXY: {d['price']:.2f} {d['direction']} ({d['change']:+.2f}%)\n"

    msg += "\n"

    # News Headlines
    if news:
        msg += "<b>📰 TOP HEADLINES</b>\n"
        for headline in news[:5]:
            msg += f"  • {headline[:70]}{'...' if len(headline) > 70 else ''}\n"
        msg += "\n"

    # Market Outlook
    if outlook:
        msg += "<b>🎯 TODAY'S OUTLOOK</b>\n"
        msg += f"<i>{outlook}</i>\n\n"

    msg += "━" * 50
    msg += "\n<i>Ready for trading? Check /pnl for yesterday's summary</i>"

    return msg
