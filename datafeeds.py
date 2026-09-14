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

import sources
from smstext import _parse_published
from timeutil import local_today

# US equities trade on New York's calendar, not the server's and not the
# reader's. timeutil imports nothing from Palmer, so this adds no cycle.
_MARKET_TZ = "America/New_York"


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
                min_score: float = 0.5, trusted_only: bool = False) -> list[dict]:
    """Return Tavily result dicts filtered for recency and source quality,
    best-source-first.

    Source quality is applied here rather than in each caller because this is
    the one place every news surface goes through — watch alerts, the morning
    briefing, Palmer Home, and the conversation search all end up here, and
    they were previously each free to do their own ranking or none at all.

    `max_results` is 10, not 5. Tavily bills per search, not per result, and
    the recency window throws most of a page away — a 5-result pull that loses
    three to the 12-hour cutoff leaves the tier sort nothing to choose between,
    which is how a lone content farm ends up as the best available source.

    The relevance floor is per-source rather than flat; see sources.meets_score.
    `trusted_only` drops tier 3 entirely; see sources.rank."""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_tavily.search, query, topic="news", days=days, max_results=10)
            response = future.result(timeout=15)
        results = response.get("results", [])
        now = datetime.now(timezone.utc)
        kept = []
        for r in results:
            pub = _parse_published(r.get("published_date"))
            if pub and now - pub <= timedelta(hours=max_age_hours):
                if sources.meets_score(r.get("url", ""), r.get("score"), min_score):
                    kept.append(r)
        return sources.rank(kept, trusted_only=trusted_only)
    except Exception:
        return []

def _search(query: str, days: int = 7, require_date: bool = False,
            max_age_hours: float | None = None) -> str:
    """The conversation-facing search. Returns prose for the drafting model.

    Junk sources are dropped and the rest ordered best-source-first, same as
    every other news path. Each story is labelled with its domain — the model
    was previously handed a flat list with no provenance at all, so it could
    not tell a wire report from a content farm and had no way to attribute
    anything it repeated."""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_tavily.search, query, topic="news", days=days, max_results=10)
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
        results = sources.rank(results)
        if not results:
            # A dead-end string here is how "I can't find news on that" becomes
            # "just Google it". The search RAN and came back empty, which is
            # information — and it is the common case, not the rare one: the
            # 12-hour recency gate plus the source floor mean a good fraction
            # of topics return nothing on a given day. Say what to do with it.
            return (
                f"The news search ran for {query!r} and came back with nothing that "
                f"cleared the recency and source bar. That is an empty result, not a "
                f"broken tool — you DO have news search. If the query was narrow or "
                f"oddly worded, try one more angle; otherwise tell them plainly you "
                f"couldn't find anything current on it and do NOT send them to another "
                f"site to look."
            )
        return "\n\n".join(
            f"[{sources.canonical_domain(r.get('url', '')) or 'unknown source'}] {r['title']}\n"
            f"Published: {r.get('published_date', 'unknown')}\n{r['content']}"
            for r in results[:5]
        )
    except concurrent.futures.TimeoutError:
        return (
            f"The news search for {query!r} timed out this once. You DO have news "
            f"search — say plainly you couldn't pull it right now and offer to try "
            f"again, or answer the rest of what they asked. Do not name another site."
        )
    except Exception as e:
        # The exception text is deliberately reduced to its type. The underlying
        # error carries the full request URL, and that URL carries the API key.
        print(f"_search failed for {query!r}: {type(e).__name__}: {e}")
        return (
            f"The news search for {query!r} errored this once ({type(e).__name__}). "
            f"You DO have news search — say plainly you couldn't pull it right now "
            f"and offer to try again. Do not invent a result and do not name another site."
        )

def price_snapshot(asset: str, label: str | None = None) -> dict | None:
    """Structured price data for the visual dashboard, including a short series
    for the sparkline. None on any failure.

    `label` overrides how the row reads on the page. Yahoo's index symbols are
    correct but unreadable — nobody wants "^GSPC" in their Markets section — so
    tickers.resolve_topic_asset hands us "S&P 500" alongside the symbol.

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
                "label": label or asset.title(), "price": data["usd"],
                "pct_24h": data.get("usd_24h_change") or 0.0,
                "pct_7d": data.get("usd_7d_change") or 0.0,
                "series": series, "is_crypto": True,
                # The actual CoinGecko coin id, e.g. "avalanche-2" for a topic
                # that matched on "avax" or "avalanche" — page._price_link needs
                # this to build a working coingecko.com URL. The display label
                # ("Avalanche", "Btc") is not that id for most of _CRYPTO_IDS,
                # so a link built from the label 404s.
                "symbol": coin_id,
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
            "label": label or asset.upper(), "price": float(current),
            "pct_24h": ((current - prev) / prev * 100) if prev else 0.0,
            "pct_7d": ((current - first) / first * 100) if first else 0.0,
            "series": closes, "is_crypto": False,
            # The real Yahoo ticker, e.g. "^GSPC" for a topic that resolved to
            # the S&P 500 — the label reads "S&P 500" for humans, but a Yahoo
            # quote URL built from that string 404s. See the crypto branch.
            "symbol": asset.upper(),
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
                return (
                    f"No price came back for {asset!r}. You DO have crypto prices — "
                    f"this one name didn't resolve. Ask them to confirm which coin they "
                    f"mean, or try the ticker instead of the name. Do not guess a number "
                    f"and do not send them to another site."
                )
            price = data["usd"]
            c24 = data.get("usd_24h_change") or 0
            c7d = data.get("usd_7d_change") or 0
            price_str = f"${price:,.2f}" if price < 1000 else f"${price:,.0f}"
            return f"{asset.title()}: {price_str} ({_fmt_pct(c24)} past 24h, {_fmt_pct(c7d)} past 7 days)"
        except Exception as e:
            print(f"crypto price lookup failed for {asset!r}: {type(e).__name__}: {e}")
            return (
                f"The crypto price lookup for {asset!r} errored this once "
                f"({type(e).__name__}). You DO have crypto prices — say plainly you "
                f"couldn't pull it right now and offer to try again. Do not guess a "
                f"number and do not name another site."
            )

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
            # "Check the ticker symbol" reads as an instruction to a developer,
            # and the model relayed it to the user as one. Say who should do what.
            return (
                f"No live price came back for {asset!r}. You DO have stock prices — "
                f"that symbol didn't resolve. If they gave a company name, try the "
                f"ticker; if you already used a ticker, ask them which company they "
                f"mean. Never conclude from this that the company is private, delisted "
                f"or hasn't listed — a failed lookup is not evidence of that."
            )

        # Determine what trading day this data is actually from, on the
        # EXCHANGE's calendar. This used to be _date.today() — the dyno's UTC
        # day — so from 19:00 ET the UTC date had already rolled and that
        # afternoon's close was labelled "yesterday". Deliberately not the
        # reader's zone either: a session closes when New York says it does,
        # whoever is asking.
        today = local_today(_MARKET_TZ)
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
        return (
            f"The stock lookup for {asset!r} timed out this once. You DO have stock "
            f"prices — say plainly you couldn't pull it right now and offer to try "
            f"again. Do not guess a number and do not name another site."
        )
    except Exception as e:
        print(f"stock lookup failed for {asset!r}: {type(e).__name__}: {e}")
        return (
            f"The stock lookup for {asset!r} errored this once ({type(e).__name__}). "
            f"You DO have stock prices — say plainly you couldn't pull it right now "
            f"and offer to try again. Do not guess a number, do not name another site, "
            f"and never conclude the company is private or delisted from a failed lookup."
        )

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
