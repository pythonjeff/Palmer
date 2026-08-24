"""Turning a morning topic into a tradeable symbol.

The Markets section of Palmer Home is derived from the user's morning topics,
so "add Nvidia to my site" only shows a price if the topic can be resolved to a
symbol. It used to be a bare uppercase-word regex, which meant the section
populated only when the model happened to write the ticker into the topic
itself — "Nvidia stock price (NVDA)" worked, "Nvidia stock" silently did not,
and the user got the topic listed under "Palmer is watching" with no price
anywhere. It also matched the "US" in "US stock market" and spent a yfinance
call on a delisted symbol every time the page refreshed.

Resolution runs cheapest-first and never calls a model on the read path, which
runs on every page view:

    1. crypto name        "bitcoin"              -> bitcoin
    2. explicit symbol    "$NVDA", "(NVDA)"      -> NVDA
    3. curated name map   "nvidia", "s&p 500"    -> NVDA, ^GSPC
    4. bare uppercase     "NVDA stock"           -> NVDA   (stopword-guarded)

`resolve_company_ticker` is the escape hatch for names the map doesn't carry.
It makes a network call, so it runs once when a topic is SAVED, not when it is
read — see the update_morning_briefing dispatch in agent.py.

No model is asked for a symbol anywhere in here, and that is deliberate. Two
earlier versions of this got it wrong in the same way: a hardcoded list of
private companies (which listed SpaceX as private after it had IPO'd as SPCX),
then a Haiku lookup with its answer verified against the exchange. Both encoded
a model's snapshot of who was public, and a snapshot goes stale the moment
anybody lists. Asked for SpaceX's ticker a model may answer SPCE — Virgin
Galactic — with total confidence.

Yahoo's search endpoint answers the same question authoritatively, for free,
with no key, and stays current on its own: it independently returns SPCX for
SpaceX and XYZ for Block, the two entries the local map had wrong.

Indices are the exception and stay hand-mapped, because search returns futures
contracts for them ("s&p 500" -> ES=F, "nasdaq" -> NQ=F) rather than the index.

`python tickers.py --audit` re-checks every curated symbol against live market
data. Run it when touching the maps — it is what catches staleness in the parts
that are still written by hand.
"""
from __future__ import annotations

import re

# Symbol -> how it should read on the page. Yahoo's index symbols are correct
# but unreadable; nobody wants "^GSPC" in their Markets section.
INDEX_LABELS = {
    "^GSPC": "S&P 500",
    "^DJI": "Dow",
    "^IXIC": "Nasdaq",
    "^RUT": "Russell 2000",
    "^VIX": "VIX",
}

# Names users actually type. Kept deliberately short — this is the common path,
# not an exhaustive listing; anything missing falls through to the Haiku pass at
# save time. Keys are matched on word boundaries against the lowercased topic.
# Index names are unambiguously market references — "nasdaq" needs no further
# qualification — so these resolve on their own.
INDEX_TICKERS = {
    "s&p 500": "^GSPC", "s&p500": "^GSPC", "sp500": "^GSPC", "s&p": "^GSPC",
    "dow jones": "^DJI", "the dow": "^DJI",
    "nasdaq": "^IXIC", "russell 2000": "^RUT", "vix": "^VIX",
    "stock market": "^GSPC", "the market": "^GSPC", "markets": "^GSPC",
}

# Company names, by contrast, are ordinary words in a news topic. "SpaceX news"
# and "Disney movie news" are subjects, not price requests, so these only
# resolve when the topic also asks about a price — see resolve_topic_asset.
COMPANY_TICKERS = {
    # tech
    "apple": "AAPL", "microsoft": "MSFT", "alphabet": "GOOGL", "google": "GOOGL",
    "amazon": "AMZN", "nvidia": "NVDA", "tesla": "TSLA", "meta": "META",
    "facebook": "META", "netflix": "NFLX", "intel": "INTC", "amd": "AMD",
    "qualcomm": "QCOM", "broadcom": "AVGO", "oracle": "ORCL", "salesforce": "CRM",
    "adobe": "ADBE", "palantir": "PLTR", "uber": "UBER", "lyft": "LYFT",
    "airbnb": "ABNB", "spotify": "SPOT", "snowflake": "SNOW", "micron": "MU",
    "arm holdings": "ARM", "super micro": "SMCI", "dell": "DELL", "ibm": "IBM",
    "spacex": "SPCX", "space exploration technologies": "SPCX",
    "rocket lab": "RKLB", "ast spacemobile": "ASTS",
    # finance / crypto-adjacent equities
    "coinbase": "COIN", "robinhood": "HOOD", "paypal": "PYPL",
    "block": "XYZ", "square": "XYZ",
    "jpmorgan": "JPM", "goldman sachs": "GS", "bank of america": "BAC",
    "berkshire": "BRK-B", "visa": "V", "mastercard": "MA", "sofi": "SOFI",
    "microstrategy": "MSTR", "strategy": "MSTR",
    # consumer / industrial / health
    "walmart": "WMT", "costco": "COST", "target": "TGT", "home depot": "HD",
    "nike": "NKE", "starbucks": "SBUX", "mcdonalds": "MCD", "coca cola": "KO",
    "pepsi": "PEP", "disney": "DIS", "boeing": "BA", "ford": "F",
    "general motors": "GM", "rivian": "RIVN", "lucid": "LCID",
    "exxon": "XOM", "chevron": "CVX", "pfizer": "PFE", "moderna": "MRNA",
    "eli lilly": "LLY", "novo nordisk": "NVO", "unitedhealth": "UNH",
    "gamestop": "GME",
}

# Uppercase tokens that look like tickers and are not. "US stock market"
# resolving to "US" was a real, silent, every-page-view failure.
NOT_TICKERS = frozenset({
    "US", "USA", "UK", "EU", "UN", "AI", "ML", "IT", "TV", "PC", "EV",
    "ETF", "IPO", "CEO", "CFO", "COO", "GDP", "CPI", "PPI", "SEC", "FED",
    "FOMC", "NYSE", "IRS", "DOJ", "FBI", "CIA", "NASA", "WHO", "NATO",
    "USD", "EUR", "GBP", "JPY", "ATH", "YOY", "YTD", "EPS", "P/E",
    "Q1", "Q2", "Q3", "Q4", "ESG", "API", "APR", "IRA", "HSA",
    "WSJ", "NYT", "CNBC", "BBC", "CNN", "AP", "NFL", "NBA", "MLB", "NHL",
    "THE", "AND", "FOR", "NEW", "NOW", "TOP", "ALL", "MY", "A", "I", "S", "P",
})

_PRICE_WORDS = ("price", "stock", "shares", "ticker", "crypto", "market",
                "quote", "trading", "index")

_EXPLICIT_SYMBOL = re.compile(r"(?:\$([A-Za-z][A-Za-z.\-]{0,5})\b|\(\s*([A-Z][A-Z.\-]{0,5})\s*\))")
_BARE_TICKER = re.compile(r"\b([A-Z][A-Z.\-]{0,4})\b")


def _label_for(symbol: str) -> str:
    return INDEX_LABELS.get(symbol, symbol)


def resolve_topic_asset(topic: str) -> tuple[str, str] | None:
    """(symbol, display_label) for a topic that is asking for a price, else None.

    Free and deterministic — safe to call on every page view."""
    from datafeeds import _CRYPTO_IDS
    if not topic:
        return None
    low = topic.lower()

    for name in _CRYPTO_IDS:
        if re.search(rf"\b{re.escape(name)}\b", low):
            return name, name.title()

    m = _EXPLICIT_SYMBOL.search(topic)
    if m:
        sym = (m.group(1) or m.group(2) or "").upper()
        if sym and sym not in NOT_TICKERS:
            return sym, _label_for(sym)

    # Longest key first so "dow jones" beats "the dow" and "s&p 500" beats "s&p".
    for name in sorted(INDEX_TICKERS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", low):
            sym = INDEX_TICKERS[name]
            return sym, _label_for(sym)

    # Everything below needs the topic to actually be asking about a price.
    # Without this gate "SpaceX news" resolves to SPCX and a news topic
    # silently grows a stock ticker in the Markets section.
    if not any(w in low for w in _PRICE_WORDS):
        return None

    for name in sorted(COMPANY_TICKERS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", low):
            sym = COMPANY_TICKERS[name]
            return sym, _label_for(sym)

    for cand in _BARE_TICKER.findall(topic):
        if cand not in NOT_TICKERS and len(cand) >= 2:
            return cand, _label_for(cand)
    return None


def resolve_asset_name(name: str) -> str | None:
    """Symbol for something the caller already knows is a price request.

    Same ladder as resolve_topic_asset without the price-word gate, because the
    context supplies the intent — get_price was called, so "spacex" means the
    stock. Without this the tool passed company names straight to yfinance,
    which 404s on "SPACEX" and let Palmer conclude the company was private."""
    got = resolve_topic_asset(name)
    if got:
        return got[0]
    low = (name or "").lower().strip()
    for key in sorted(COMPANY_TICKERS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", low):
            return COMPANY_TICKERS[key]
    for cand in _BARE_TICKER.findall(name or ""):
        if cand not in NOT_TICKERS and len(cand) >= 2:
            return cand
    return None


# Yahoo's own symbol search. Keyless, ~0.2s, and — unlike anything written from
# model knowledge — self-updating: it independently returns SPCX for SpaceX and
# XYZ for Block, the two entries this map had wrong.
_SEARCH_URL = "https://query2.finance.yahoo.com/v1/finance/search"

# Real listings, not derivatives. The filter is load-bearing rather than
# defensive: unfiltered, "openai" comes back as a tokenized crypto and a
# thematic ETF that merely share the name, and "anthropic" as a crypto
# derivative. Restricting to equities on a US exchange is what makes a private
# company resolve to nothing instead of to somebody else's price.
_US_EXCHANGES = frozenset({"NMS", "NYQ", "NAS", "NGM", "ASE", "PCX", "BTS", "NCM"})


def _search_query(topic: str) -> str:
    """The topic reduced to the thing being asked about.

    Search matches names, not sentences: "spacex" returns SPCX and "spacex
    stock" returns nothing at all."""
    words = [w for w in re.split(r"[^A-Za-z0-9&.\-]+", topic or "")
             if w and w.lower() not in _PRICE_WORDS and w.lower() not in
             {"the", "a", "an", "and", "my", "of", "for", "updates", "update",
              "news", "daily", "today", "latest"}]
    return " ".join(words).strip()


def search_symbol(name: str) -> str | None:
    """Ticker for a company name via Yahoo's search, or None.

    Network-bound, so callers must use it at SAVE time — never on the read
    path, which runs on every page view. Returns None on any failure: an
    unresolved topic costs one missing price row, which is cheap and visible,
    where a wrong symbol is silent."""
    import urllib.parse
    from netutil import _http_get_json
    query = _search_query(name)
    if not query:
        return None
    url = f"{_SEARCH_URL}?" + urllib.parse.urlencode({"q": query, "quotesCount": 10})
    data = _http_get_json(url, timeout=10)
    if not data:
        return None
    for quote in (data.get("quotes") or []):
        if quote.get("quoteType") != "EQUITY":
            continue
        if quote.get("exchange") not in _US_EXCHANGES:
            continue
        symbol = (quote.get("symbol") or "").upper()
        if symbol and symbol not in NOT_TICKERS and "." not in symbol:
            return symbol
    return None


def resolve_company_ticker(topic: str) -> str | None:
    """Ticker for a topic the local maps don't carry, or None.

    This used to ask Haiku for a symbol and then check the answer against the
    exchange, because a model asked for SpaceX's ticker may answer SPCE —
    Virgin Galactic. Yahoo's search does the same job without the model, the
    verification step, or the chance of a confident wrong answer, and it stays
    current on its own.

    Indices deliberately do NOT come through here: search returns futures
    contracts for them ("s&p 500" -> ES=F, "nasdaq" -> NQ=F), so those stay
    hand-mapped in INDEX_TICKERS where they are correct."""
    return search_symbol(topic)


def looks_like_price_topic(topic: str) -> bool:
    """Whether a topic is asking for a price at all. Gates the save-time Haiku
    call so ordinary news topics never pay for one."""
    return any(w in (topic or "").lower() for w in _PRICE_WORDS)


def audit_map() -> list[tuple[str, str, str]]:
    """Check every curated symbol against live market data.

    The map is written from model knowledge, which is a snapshot with a date on
    it. Two entries were already wrong when this was added: SpaceX was listed
    as private after it had IPO'd as SPCX, and Block was still SQ after it
    became XYZ. Run this after touching COMPANY_TICKERS.

    Returns the rows that failed. Network-bound, so it is a command, not a test."""
    import yfinance as yf
    problems, checked = [], {}
    everything = {**INDEX_TICKERS, **COMPANY_TICKERS}
    for name, symbol in sorted(everything.items(), key=lambda kv: kv[1]):
        if symbol in checked:
            continue
        try:
            t = yf.Ticker(symbol)
            info = t.info or {}
            listed = info.get("longName") or info.get("shortName") or ""
            price = t.fast_info.last_price
            ok = bool(listed and price)
        except Exception as e:
            listed, ok = f"{type(e).__name__}: {e}", False
        checked[symbol] = ok
        status = "ok  " if ok else "FAIL"
        print(f"{status} {symbol:8} {name:32} {listed[:44]}")
        if not ok:
            problems.append((name, symbol, listed))
    print(f"\n{len(checked)} symbols checked, {len(problems)} failed")
    return problems


if __name__ == "__main__":
    import sys
    if "--audit" in sys.argv:
        sys.exit(1 if audit_map() else 0)
    print("usage: python tickers.py --audit")
