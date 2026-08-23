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
It costs a Haiku call, so it runs once when a topic is SAVED, not when it is
read — see the update_morning_briefing dispatch in agent.py.

PRIVATE_COMPANIES exists because that Haiku call is the dangerous path. Asked
for SpaceX's ticker, a model will happily answer SPCE, which is Virgin
Galactic — a different company, priced wrong, on someone's personal page. The
private names are checked before the model is ever asked.
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
COMPANY_TICKERS = {
    # indices and the generic "how's the market" ask
    "s&p 500": "^GSPC", "s&p500": "^GSPC", "sp500": "^GSPC", "s&p": "^GSPC",
    "dow jones": "^DJI", "the dow": "^DJI",
    "nasdaq": "^IXIC", "russell 2000": "^RUT", "vix": "^VIX",
    "stock market": "^GSPC", "the market": "^GSPC", "markets": "^GSPC",
    # tech
    "apple": "AAPL", "microsoft": "MSFT", "alphabet": "GOOGL", "google": "GOOGL",
    "amazon": "AMZN", "nvidia": "NVDA", "tesla": "TSLA", "meta": "META",
    "facebook": "META", "netflix": "NFLX", "intel": "INTC", "amd": "AMD",
    "qualcomm": "QCOM", "broadcom": "AVGO", "oracle": "ORCL", "salesforce": "CRM",
    "adobe": "ADBE", "palantir": "PLTR", "uber": "UBER", "lyft": "LYFT",
    "airbnb": "ABNB", "spotify": "SPOT", "snowflake": "SNOW", "micron": "MU",
    "arm holdings": "ARM", "super micro": "SMCI", "dell": "DELL", "ibm": "IBM",
    # finance / crypto-adjacent equities
    "coinbase": "COIN", "robinhood": "HOOD", "block": "SQ", "paypal": "PYPL",
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

# Well-known companies with no public listing. Checked BEFORE the model is
# asked, because the wrong answer here is a real price for a different company.
PRIVATE_COMPANIES = frozenset({
    "spacex", "openai", "anthropic", "stripe", "starlink", "x", "twitter",
    "bytedance", "tiktok", "databricks", "canva", "epic games", "valve",
    "spacex stock", "neuralink", "the boring company", "xai", "perplexity",
    "instacart-private", "fidelity", "vanguard", "bloomberg", "deloitte",
    "mars", "cargill", "koch", "publix", "in-n-out", "chick-fil-a", "ikea",
    "lego", "rolex", "patagonia", "spanx", "sheetz", "wawa",
})

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


def is_private_company(name: str) -> bool:
    low = name.lower().strip()
    return any(re.search(rf"\b{re.escape(p)}\b", low) for p in PRIVATE_COMPANIES)


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
    for name in sorted(COMPANY_TICKERS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name)}\b", low):
            sym = COMPANY_TICKERS[name]
            return sym, _label_for(sym)

    if any(w in low for w in _PRICE_WORDS):
        for cand in _BARE_TICKER.findall(topic):
            if cand not in NOT_TICKERS and len(cand) >= 2:
                return cand, _label_for(cand)
    return None


_RESOLVE_PROMPT = """What is the stock ticker symbol for the company or index in this request?

Request: {topic}

Answer with ONLY the ticker symbol, or the word NONE.

Answer NONE if:
- the company is private or not publicly traded on its own
- it is a subsidiary priced under a different parent (answer the parent's ticker ONLY if the user clearly means the parent)
- the request is about a sector, theme, or news topic rather than one tradeable thing
- you are not confident of the exact symbol

Never guess. A wrong ticker puts a different company's price on this person's page, which is worse than showing nothing."""


def resolve_company_ticker(topic: str) -> str | None:
    """Ticker for a topic the map doesn't carry, or None.

    Costs a Haiku call — callers must use it at SAVE time only. Returns None
    rather than raising; an unresolved topic simply gets no price."""
    if is_private_company(topic):
        return None
    try:
        from llm import client, HAIKU_MODEL
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=12,
            messages=[{"role": "user", "content": _RESOLVE_PROMPT.format(topic=topic)}],
        )
        answer = (response.content[0].text or "").strip().upper().strip(".$")
    except Exception as e:
        print(f"resolve_company_ticker failed for {topic!r}: {type(e).__name__}: {e}")
        return None
    if (not answer or answer == "NONE" or answer in NOT_TICKERS
            or not re.fullmatch(r"\^?[A-Z][A-Z.\-]{0,5}", answer)):
        return None
    return answer


def looks_like_price_topic(topic: str) -> bool:
    """Whether a topic is asking for a price at all. Gates the save-time Haiku
    call so ordinary news topics never pay for one."""
    return any(w in (topic or "").lower() for w in _PRICE_WORDS)
