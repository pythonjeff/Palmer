"""One adjacent story per briefing: what the world is talking about, filtered
down to what THIS person would plausibly care about.

Everything else in the morning briefing is strictly what the user asked to
track. This is the one deliberate exception — a friend who reads the news all
day surfaces the occasional thing you didn't ask for but are glad to hear. The
constraint that makes it work is adjacency: not the top global trend, but the
trending thing nearest what they already follow, and never something already
covered by today's topics.

Google Trends via SerpAPI returns hundreds of raw queries ("newcastle vs
liverpool", "fromm and oma's pride recalls"), most of which are noise for any
given person. Haiku does the adjacency judgement; Tavily supplies the substance,
because a bare search query is not a story.
"""
from __future__ import annotations

import threading
from datetime import date

import serpapi
from llm import client, HAIKU_MODEL, _parse_json
from datafeeds import _search_raw

# Trending is identical for every user in a geo, so one fetch serves the whole
# morning run. Keyed by (geo, local date) — same pattern as rubrics.py.
_cache: dict[tuple[str, str], list[dict]] = {}
_cache_lock = threading.Lock()

# How many trends Haiku sees. The full list is ~600; the long tail is noise and
# only inflates the prompt.
CANDIDATE_COUNT = 40
MIN_SEARCH_VOLUME = 20000


def trending_now(geo: str = "US", today: date | None = None) -> list[dict]:
    """Top trending searches for a geo, highest volume first. Never raises."""
    key = (geo, (today or date.today()).isoformat())
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit

    items: list[dict] = []
    try:
        data = serpapi.search({"engine": "google_trends_trending_now", "geo": geo})
        for t in (data or {}).get("trending_searches") or []:
            query = (t.get("query") or "").strip()
            volume = t.get("search_volume") or 0
            if not query or volume < MIN_SEARCH_VOLUME:
                continue
            cats = [c.get("name", "") for c in (t.get("categories") or []) if c.get("name")]
            items.append({"query": query, "volume": volume, "categories": cats})
        items.sort(key=lambda i: i["volume"], reverse=True)
        items = items[:CANDIDATE_COUNT]
    except Exception as e:
        print(f"trending_now failed for {geo}: {type(e).__name__}: {e}")
        items = []

    with _cache_lock:
        _cache[key] = items
    return items


_PICK_PROMPT = """Someone follows these subjects closely:
{interests}

Today their briefing already covers:
{covered}

Here is what the country is searching for right now:
{candidates}

Pick the ONE trending item they'd most likely be glad someone mentioned — something ADJACENT to what they follow, not identical to it. A person who follows baseball might care about a major trade in another sport; a person who follows SpaceX might care about a big aviation story. Someone who follows nothing related to it should not get it.

The bar is high. Ask: would a well-read friend actually bring this up unprompted? Most trending searches fail that test — they trend because something is scheduled, not because something happened.

Never pick:
- A scheduled fixture or a scoreline ("espanyol vs real madrid", "cowboys vs cardinals")
- An incident that resolved with no consequences (a flight that landed safely, a recall, a minor outage)
- Celebrity or gossip items, product launches, or anything a search spike alone made visible
- Anything today's briefing already covers. That is a repeat, not a discovery.

A real development means: something changed that has consequences beyond today — a result that shifts a race, a decision, a failure, a breakthrough, a death, a genuine first.

Be careful with names that collide. "cardinals" can mean two different teams; do not treat a match on the list as related to a subject they follow just because a word overlaps.

Then pick the best of what's left. If several clear the bar, take the one nearest their world. Do not reject a real development just because the match is loose — adjacent is the point, and a business story for someone who follows markets counts even if it isn't about their exact holdings.

Say NONE only when everything on the list is fixtures, gossip and noise. That happens, especially on weekends. But NONE is a verdict on the list, not a way to play it safe.

Return ONLY: {{"query": "<the trending query exactly as written>", "why": "<6 words on the connection to them>"}}
or {{"query": "NONE"}}"""


def adjacent_story(interests: list[str], covered: list[str],
                   geo: str = "US", today: date | None = None) -> dict | None:
    """Pick one trending item near this person's interests and attach a real
    story to it. Returns {"query", "why", "story"} or None. Never raises."""
    if not interests:
        return None
    candidates = trending_now(geo, today)
    if not candidates:
        return None

    listing = "\n".join(
        f"- {c['query']}" + (f" ({', '.join(c['categories'])})" if c["categories"] else "")
        for c in candidates
    )
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": _PICK_PROMPT.format(
                interests="\n".join(f"- {i}" for i in interests),
                covered="\n".join(f"- {c}" for c in covered) or "- (nothing yet)",
                candidates=listing,
            )}],
        )
        parsed = _parse_json(response.content[0].text)
    except Exception as e:
        print(f"adjacent_story pick failed: {type(e).__name__}: {e}")
        return None

    if not isinstance(parsed, dict):
        return None
    query = str(parsed.get("query", "") or "").strip()
    if not query or query.upper() == "NONE":
        return None
    # Only accept a query that was actually on the list — otherwise the model has
    # invented a trend, and an invented trend is exactly the filler this is meant
    # to avoid.
    if not any(query.lower() == c["query"].lower() for c in candidates):
        print(f"adjacent_story: {query!r} was not in the candidate list, dropping")
        return None

    results = _search_raw(query, days=2, max_age_hours=48, min_score=0.5)
    if not results:
        return None
    top = results[0]
    return {
        "query": query,
        "why": str(parsed.get("why", "") or "")[:60],
        "story": f"{top.get('title','')}\n{top.get('content','')}",
    }
