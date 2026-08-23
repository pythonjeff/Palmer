"""External data feeds: Tavily news search, crypto/stock prices, GIFs, media.

Weather deliberately lives in weather.py — it is far bigger than the rest
and has its own two-provider fallback.
"""
import os
import base64
import random
import concurrent.futures
from datetime import datetime, timezone, timedelta, date as _date

import requests as _requests
from tavily import TavilyClient

from smstext import _parse_published


_CRYPTO_IDS = {
    "bitcoin": "bitcoin", "btc": "bitcoin",
    "ethereum": "ethereum", "eth": "ethereum",
    "dogecoin": "dogecoin", "doge": "dogecoin",
    "solana": "solana", "sol": "solana",
    "cardano": "cardano", "ada": "cardano",
    "xrp": "ripple", "ripple": "ripple",
    "litecoin": "litecoin", "ltc": "litecoin",
    "avalanche": "avalanche-2", "avax": "avalanche-2",
    "polygon": "matic-network", "matic": "matic-network",
    "shiba inu": "shiba-inu", "shib": "shiba-inu",
    "bnb": "binancecoin", "binance coin": "binancecoin",
    "chainlink": "chainlink", "link": "chainlink",
    "polkadot": "polkadot", "dot": "polkadot",
    "uniswap": "uniswap", "uni": "uniswap",
    "stellar": "stellar", "xlm": "stellar",
    "monero": "monero", "xmr": "monero",
}

_tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])

def _search_raw(query: str, days: int = 1, max_age_hours: float = 12,
                min_score: float = 0.5) -> list[dict]:
    """Return Tavily result dicts filtered by recency and quality, sorted best-first."""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_tavily.search, query, topic="news", days=days, max_results=5)
            response = future.result(timeout=15)
        results = response.get("results", [])
        now = datetime.now(timezone.utc)
        kept = []
        for r in results:
            pub = _parse_published(r.get("published_date"))
            if pub and now - pub <= timedelta(hours=max_age_hours):
                if (r.get("score") or 0) >= min_score:
                    kept.append(r)
        kept.sort(key=lambda r: r.get("score") or 0, reverse=True)
        return kept
    except Exception:
        return []

def _search(query: str, days: int = 7, require_date: bool = False,
            max_age_hours: float | None = None) -> str:
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_tavily.search, query, topic="news", days=days, max_results=5)
            response = future.result(timeout=15)
        results = response.get("results", [])
        if require_date or max_age_hours is not None:
            now = datetime.now(timezone.utc)
            kept = []
            for r in results:
                pub = _parse_published(r.get("published_date"))
                if pub is None:
                    continue  # undated results can't be trusted as fresh
                if max_age_hours is not None and now - pub > timedelta(hours=max_age_hours):
                    continue
                kept.append(r)
            results = kept
        if not results:
            return "No results found."
        return "\n\n".join(
            f"{r['title']}\nPublished: {r.get('published_date', 'unknown')}\n{r['content']}"
            for r in results
        )
    except concurrent.futures.TimeoutError:
        return "Search timed out."
    except Exception as e:
        return f"Search failed: {e}"

def price_snapshot(asset: str) -> dict | None:
    """Structured price data for the visual dashboard, including a short series
    for the sparkline. None on any failure.

    _get_price computes price and deltas and formats them away. This is
    additive — _get_price is untouched, so the text briefing can't regress."""
    asset_lower = asset.lower().strip()
    coin_id = _CRYPTO_IDS.get(asset_lower)
    try:
        if coin_id:
            resp = _requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd",
                        "include_24hr_change": "true", "include_7d_change": "true"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get(coin_id) or {}
            if not data:
                return None
            series = []
            try:  # sparkline is a bonus — never let it cost us the price
                chart = _requests.get(
                    f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
                    params={"vs_currency": "usd", "days": "7", "interval": "daily"},
                    timeout=10,
                )
                chart.raise_for_status()
                series = [p[1] for p in (chart.json().get("prices") or [])]
            except Exception:
                pass
            return {
                "label": asset.title(), "price": data["usd"],
                "pct_24h": data.get("usd_24h_change") or 0.0,
                "pct_7d": data.get("usd_7d_change") or 0.0,
                "series": series, "is_crypto": True,
            }

        import yfinance as yf
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            def _fetch():
                t = yf.Ticker(asset.upper())
                return t.fast_info, t.history(period="7d")
            fi, hist = ex.submit(_fetch).result(timeout=15)
        current = fi.last_price
        if not current:
            return None
        closes = [float(c) for c in hist["Close"].tolist()] if not hist.empty else []
        prev = closes[-2] if len(closes) >= 2 else current
        first = closes[0] if closes else current
        return {
            "label": asset.upper(), "price": float(current),
            "pct_24h": ((current - prev) / prev * 100) if prev else 0.0,
            "pct_7d": ((current - first) / first * 100) if first else 0.0,
            "series": closes, "is_crypto": False,
        }
    except Exception as e:
        print(f"price_snapshot failed for {asset!r}: {type(e).__name__}: {e}")
        return None


def _get_price(asset: str) -> str:
    asset_lower = asset.lower().strip()

    def _fmt_pct(p: float) -> str:
        sign = "+" if p >= 0 else ""
        return f"{sign}{p:.1f}%"

    # Crypto path — CoinGecko 24h change is a true rolling window, not market-day-dependent
    coin_id = _CRYPTO_IDS.get(asset_lower)
    if coin_id:
        try:
            resp = _requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": coin_id,
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_7d_change": "true",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get(coin_id, {})
            if not data:
                return f"No price data found for {asset}."
            price = data["usd"]
            c24 = data.get("usd_24h_change") or 0
            c7d = data.get("usd_7d_change") or 0
            price_str = f"${price:,.2f}" if price < 1000 else f"${price:,.0f}"
            return f"{asset.title()}: {price_str} ({_fmt_pct(c24)} past 24h, {_fmt_pct(c7d)} past 7 days)"
        except Exception as e:
            return f"Crypto price lookup failed: {e}"

    # Stock path via yfinance
    try:
        import yfinance as yf
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            def _fetch():
                t = yf.Ticker(asset.upper())
                return t.fast_info, t.history(period="5d")
            fi, hist = ex.submit(_fetch).result(timeout=15)

        current = fi.last_price
        if current is None or current == 0:
            return f"Couldn't find price data for '{asset}'. Check the ticker symbol."

        # Determine what trading day this data is actually from
        today = _date.today()
        last_trade_date = hist.index[-1].date() if not hist.empty else None

        if last_trade_date == today:
            day_label = "today"
            market_note = ""
        elif last_trade_date == today - timedelta(days=1):
            day_label = "yesterday"
            market_note = ""
        else:
            day_label = last_trade_date.strftime("%A") if last_trade_date else "last session"
            market_note = " — market closed"

        prev = fi.regular_market_previous_close or current
        c24 = (current - prev) / prev * 100

        c7d_str = ""
        if len(hist) >= 4:
            week_ago = float(hist["Close"].iloc[0])
            c7d = (current - week_ago) / week_ago * 100
            c7d_str = f", {_fmt_pct(c7d)} past 5 sessions"

        return f"{asset.upper()}: ${current:.2f} ({_fmt_pct(c24)} on {day_label}{c7d_str}{market_note})"

    except concurrent.futures.TimeoutError:
        return f"Stock lookup timed out for '{asset}'."
    except Exception as e:
        return f"Stock lookup failed for '{asset}': {e}"

_SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

def _fetch_media(url: str) -> tuple[str, str] | None:
    """Fetch media from a Twilio URL. Returns (base64_data, content_type) or None."""
    try:
        resp = _requests.get(
            url,
            auth=(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]),
            timeout=10,
        )
        content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
        if content_type not in _SUPPORTED_IMAGE_TYPES:
            return None
        return base64.standard_b64encode(resp.content).decode(), content_type
    except Exception:
        return None

def _get_gif(query: str) -> str | None:
    """Search Giphy for a GIF matching the query. Returns a URL or None."""
    api_key = os.environ.get("GIPHY_API_KEY")
    if not api_key:
        return None
    try:
        resp = _requests.get(
            "https://api.giphy.com/v1/gifs/search",
            params={"api_key": api_key, "q": query, "limit": 10, "rating": "pg-13"},
            timeout=8,
        )
        data = resp.json().get("data", [])
        if not data:
            return None
        pick = random.choice(data[:3])  # top 3 are most relevant; add variety without going too far down
        # downsized keeps files under ~2MB — better for MMS delivery
        images = pick.get("images", {})
        return (images.get("downsized") or images.get("original") or {}).get("url")
    except Exception:
        return None
