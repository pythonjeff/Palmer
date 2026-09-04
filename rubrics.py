"""Topic-genre classification and per-genre significance rubrics.

The rubrics are where Palmer's taste lives. They enumerate — in plain prose,
the way a real friend would think about it — what actually deserves a text
about a given kind of topic, and what nobody would bother sending.

watches.py (user-created news watches) calls classify_genre() once per topic
and then splices rubric_for(genre) into its Haiku scoring prompt. First
classification is cached in-process; watches.py additionally persists it to
the row so subsequent process starts don't re-pay the cost. (An unprompted
daily alert job used to share these; it was retired in favour of the evening
update, which is a diff rather than a judgment call.)
"""
from __future__ import annotations

import threading

from llm import client, HAIKU_MODEL


VALID_GENRES = (
    "sports_team",
    "market_instrument",
    "geopolitics",
    "tech_company",
    "weather_event",
    "entertainment",
    "personal_interest",
    "other",
)


GENRE_RUBRICS: dict[str, str] = {
    "sports_team": """A real friend would text about:
- A win or loss that actually matters: rivalry game, playoff race move, upset of a strong team, blowout of a good one
- Trades, signings, contract extensions, coaching hires or firings
- Key-player injuries (starter out for weeks, unexpected return from injury)
- Streaks starting to matter (5+ in a row) or streaks ending; movement in the standings
- Milestones: record broken, first in franchise history, personal record for a marquee player
- Off-field news that changes how the team is talked about (scandal, suspension, front-office shakeup)

Nobody would text about:
- Routine box scores, backup player stats, average-day results
- Pre-game predictions, betting lines, fantasy takes
- Generic "state of the franchise" think pieces, mock draft speculation
- Historical retrospectives, "on this day" nostalgia
- Rumor pieces with no real sources
""",

    "market_instrument": """A real friend would text about:
- Big single-day moves: >=5% for a stock, >=8% for crypto, >=25bp for a rate
- Earnings that miss or beat by a wide margin; guidance that resets expectations
- Fed / central bank / SEC actions that move the whole market or the specific asset
- Company-specific news that shifts the story: M&A, CEO change, product recall, major lawsuit, security breach
- Historic thresholds: all-time high or low, round-number level that traders talk about ($100k BTC, $200 AAPL, 8% mortgage)
- Broad regime shifts: inflation print, jobs number, rate cut, credit event

Nobody would text about:
- Routine days under threshold; typical open/close chatter
- Analyst notes, price-target changes, rating shuffles
- Options unusual activity, technical patterns, chart-reading commentary
- Recap-of-the-day articles that rehash what already moved
- Endless "will Fed cut?" speculation with no new data
""",

    "geopolitics": """A real friend would text about:
- War or military action starting, escalating meaningfully, or ending
- Major diplomatic breakthroughs OR breakdowns: treaty signed, accord reached, ambassador expelled, alliance shift
- Election results with real consequences; government falls; leader steps down
- Coups, regime changes, major protest movements crossing an inflection point
- Sanctions announced, sanctions lifted, historic agreements between adversaries
- Direct attacks on civilians, mass-casualty events, humanitarian catastrophes

Nobody would text about:
- Daily political commentary or opinion pieces
- Poll fluctuations mid-cycle; hypothetical scenarios
- Routine diplomatic statements or speeches
- Speculation about what "might" happen next quarter
- Explainers on already-established situations
""",

    "tech_company": """A real friend would text about:
- Product launches with real user impact: new device, big feature the user cares about, major software release
- Layoffs, C-suite changes, board shakeups at major companies
- Regulatory actions, antitrust suits, major lawsuits with teeth
- Security incidents / breaches that could affect the user
- Earnings that move the stock materially
- Acquisitions, IPOs, funding rounds that reset the industry

Nobody would text about:
- Minor version releases, small feature updates
- Rumor pieces without sourcing
- Analyst commentary, editorial opinion, "hot takes"
- Ranked lists, "best of" articles, review roundups
- Speculation about future products with no leak
""",

    "weather_event": """A real friend would text about:
- Watches or warnings for the user's own area: tornado, hurricane, flash flood, blizzard, red-flag fire
- Historic events: record heat or cold, unusual snowfall total, hurricane hitting land
- Evacuation orders, major infrastructure impact (power out for a metro, roads closed)
- Storm tracks that shift onto the user's region

Nobody would text about:
- Routine forecast changes (a few degrees warmer, chance of rain)
- Weather that doesn't affect the user's region
- Long-range speculative forecasts
""",

    "entertainment": """A real friend would text about:
- Show / film releases the user actually follows (season out, unexpected renewal or cancellation)
- Awards, major casting news, breakup/marriage/death of someone they've mentioned
- Genuinely unusual moments: viral moment, historic performance, feud that broke out
- Album drops, tour announcements for artists they follow

Nobody would text about:
- Reviews, "best of" rankings, critical takes
- Rumor pieces, casting speculation
- Routine coverage of celebrities the user hasn't shown interest in
""",

    "personal_interest": """A real friend would text about:
- Genuinely notable development in the area: breakthrough, discovery, major milestone
- Something the user is directly affected by, or that changes how the topic is discussed
- Real news that anyone following this space would want to know today

Nobody would text about:
- General field commentary or meta-discussion
- Opinion pieces, listicles, retrospectives
- Routine updates that don't change the picture
""",

    "other": """A real friend would text about a genuinely notable, time-sensitive development that
this person would care about right now. Nobody would text about routine coverage, opinion pieces,
rumor, or content that could sit unread for a week and lose nothing.
""",
}


def rubric_for(genre: str | None) -> str:
    """Return the rubric text for a genre, falling back to 'other' for unknown values."""
    if genre and genre in GENRE_RUBRICS:
        return GENRE_RUBRICS[genre]
    return GENRE_RUBRICS["other"]


_classify_cache: dict[str, str] = {}
_cache_lock = threading.Lock()


def _cache_key(topic: str) -> str:
    return (topic or "").strip().lower()


def classify_genre(topic: str) -> str:
    """Classify a topic string into one of VALID_GENRES.

    Result is memoized in-process on the trimmed-lowercased topic so repeats
    are free within a dyno's lifetime. Callers that want persistence across
    restarts (watches.py) also stash the value in the DB row.

    Returns 'other' on any parse or API failure — never raises."""
    key = _cache_key(topic)
    if not key:
        return "other"
    with _cache_lock:
        hit = _classify_cache.get(key)
    if hit is not None:
        return hit

    prompt = (
        "Classify this topic into exactly ONE of the following categories. "
        "Reply with only the category name — no punctuation, no explanation.\n\n"
        "Categories:\n"
        "- sports_team: a specific team, league, or athlete (Cardinals, Eagles, NFL, LeBron)\n"
        "- market_instrument: a stock ticker, crypto, commodity, currency, or rate (AAPL, Bitcoin, oil, mortgage rates)\n"
        "- geopolitics: government, war, elections, international relations, protest movements\n"
        "- tech_company: a specific tech company, product, or platform (OpenAI, Apple, TikTok)\n"
        "- weather_event: weather in a specific location (St. Louis weather, hurricane season)\n"
        "- entertainment: TV, film, music, celebrity, gaming\n"
        "- personal_interest: hobbies, science, food, travel, or other subjects that don't fit above\n"
        "- other: use only if truly nothing else fits\n\n"
        f"Topic: {topic}\n\n"
        "Category:"
    )
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=15,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip().lower()
        # Model sometimes returns "sports_team." or "Category: sports_team" — normalize.
        for genre in VALID_GENRES:
            if genre in raw:
                with _cache_lock:
                    _classify_cache[key] = genre
                return genre
    except Exception as e:
        print(f"classify_genre failed for {topic!r}: {e}")
    with _cache_lock:
        _classify_cache[key] = "other"
    return "other"


def _reset_cache_for_tests() -> None:
    """Clear the in-process memoization. Tests use this to isolate calls."""
    with _cache_lock:
        _classify_cache.clear()
