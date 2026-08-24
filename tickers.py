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

That Haiku call is the dangerous path: asked for SpaceX's ticker a model may
answer SPCE, which is Virgin Galactic — a different company, priced wrong, on
someone's personal page. So the model does not get the last word. It must
return a symbol AND the official company name, and the name is checked against
what the market data actually says that symbol is. A guess that names the right
company but the wrong symbol fails the check and yields nothing.

This replaced a hardcoded list of private companies, which was the wrong shape
for the problem: it encoded one model's snapshot of who was public, and went
out of date the moment anybody IPO'd. It listed SpaceX as private, which stopped
being true. `python tickers.py --audit` re-checks every curated symbol against
live market data and is how that class of staleness gets caught — it is worth
running when touching COMPANY_TICKERS.
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


_RESOLVE_PROMPT = """What publicly traded company or index does this request refer to?

Request: {topic}

Answer on ONE line, exactly: SYMBOL | Official company name
Or the single word NONE.

Answer NONE if:
- the company is private or not publicly traded on its own
- it is a subsidiary priced under a different parent, unless the user clearly means the parent
- the request is about a sector, theme, or news topic rather than one tradeable thing
- you are not confident of the exact symbol

Never guess. A wrong ticker puts a different company's price on this person's page.
The name you give will be checked against the exchange listing for that symbol, so
give the official registered name, not a nickname."""

# Corporate boilerplate that differs between a model's answer and the exchange's
# registered name without meaning the two disagree.
_NAME_NOISE = frozenset({
    "inc", "incorporated", "corp", "corporation", "company", "co", "companies",
    "plc", "ltd", "limited", "holdings", "holding", "group", "the", "sa", "nv",
    "ag", "ab", "as", "asa", "class", "a", "b", "c", "common", "stock", "shares",
    "index", "composite", "average", "technologies", "technology",
})


def _name_tokens(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9&]+", (name or "").lower())
    return {w for w in words if w not in _NAME_NOISE}


def _names_agree(claimed: str, listed: str) -> bool:
    """Whether two renderings of a company name refer to the same company.

    Deliberately tolerant about suffixes ("Inc.", "Holdings") and strict about
    the distinctive words: "Space Exploration" vs "Virgin Galactic" share
    nothing and must not pass."""
    a, b = _name_tokens(claimed), _name_tokens(listed)
    if not a or not b:
        return False
    if a <= b or b <= a:
        return True
    return len(a & b) / min(len(a), len(b)) >= 0.6


def _verify_symbol(symbol: str, claimed_name: str) -> bool:
    """Confirm the exchange agrees this symbol is the company the model named.

    Fails closed. A network blip costs one unresolved topic; a wrong ticker
    costs a real price for the wrong company, silently, on someone's page."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        info = t.info or {}
        listed = info.get("longName") or info.get("shortName") or ""
        if not listed or not t.fast_info.last_price:
            return False
        return _names_agree(claimed_name, listed)
    except Exception as e:
        print(f"_verify_symbol({symbol!r}) failed: {type(e).__name__}: {e}")
        return False


def resolve_company_ticker(topic: str) -> str | None:
    """Ticker for a topic the map doesn't carry, or None.

    Costs a Haiku call plus a listing lookup, so callers must use it at SAVE
    time only. Returns None rather than raising; an unresolved topic simply
    gets no price row."""
    try:
        from llm import client, HAIKU_MODEL
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=40,
            messages=[{"role": "user", "content": _RESOLVE_PROMPT.format(topic=topic)}],
        )
        answer = (response.content[0].text or "").strip()
    except Exception as e:
        print(f"resolve_company_ticker failed for {topic!r}: {type(e).__name__}: {e}")
        return None

    if not answer or answer.upper().startswith("NONE"):
        return None
    symbol, _, claimed = answer.partition("|")
    symbol = symbol.strip().upper().strip(".$")
    claimed = claimed.strip()
    if (not claimed or symbol in NOT_TICKERS
            or not re.fullmatch(r"\^?[A-Z][A-Z.\-]{0,5}", symbol)):
        return None
    if not _verify_symbol(symbol, claimed):
        print(f"resolve_company_ticker: {symbol!r} did not verify as {claimed!r}")
        return None
    return symbol


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
