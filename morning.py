import json
import os
import concurrent.futures
from datetime import date as date_type
from agent import client, _tavily, _build_system, _sms_clean
from db import get_profile, upsert_profile, get_all_phones, save_message


def _search_morning(query: str) -> str:
    """Search for recent news, surfacing publish dates so stale results are visible."""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(
                _tavily.search, query,
                topic="news", days=2, max_results=5,
            )
            response = future.result(timeout=15)
        results = response.get("results", [])
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


def extract_morning_prefs(phone: str, pref_text: str):
    """Extract morning topics from a user's preference reply and save to profile."""
    try:
        profile = get_profile(phone)
        city = profile.get("city") or profile.get("location") or ""
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": f"""Someone just replied to "What do you want in your morning update?"

Their reply: "{pref_text}"
Their city (if known): "{city}"

Extract what morning topics to track. Make them specific and searchable — include city name where relevant. Examples: "Chicago weather", "Bitcoin price", "Cardinals game score", "national news headlines".

Return a JSON array of strings. Just the array, nothing else."""}],
        )
        text = response.content[0].text.strip()
        start, end = text.find("["), text.rfind("]") + 1
        if start != -1 and end > start:
            topics = json.loads(text[start:end])
            if topics:
                upsert_profile(phone, {"morning_topics": topics})
    except Exception:
        pass


def _get_search_queries(profile: dict) -> list[str]:
    today = date_type.today().strftime("%B %d, %Y")
    topics = profile.get("morning_topics")
    if topics:
        topic_list = ", ".join(topics)
        prompt = f"""Today is {today}. Convert these morning briefing topics into search queries that will find fresh, current results from today or last night.

Topics: {topic_list}
City (if relevant): {profile.get("city") or profile.get("location") or "unknown"}

Make queries time-specific — include "today", "this morning", "last night", or "{today}" where it helps get current results.
Return a JSON array of search queries, one per topic. Example: ["Bitcoin price {today}", "St. Louis weather today", "Cardinals score last night"]. Just the JSON array."""
    else:
        prompt = f"""Today is {today}. Based on this user profile, what should I search for their morning briefing?

Profile: {json.dumps(profile, indent=2)}

Make queries time-specific — use "today", "this morning", or "{today}" to get current results.
Return a JSON array of 1-3 search queries. Example: ["St. Louis weather today", "Cardinals score last night"]. If unclear, return []. Just the JSON array."""

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    start, end = text.find("["), text.rfind("]") + 1
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end])
        except Exception:
            pass
    return []


def generate_morning(phone: str) -> str:
    profile = get_profile(phone)
    system = _build_system(phone)
    today = date_type.today().strftime("%B %d, %Y")
    queries = _get_search_queries(profile)
    results = "\n\n".join(f"{q}:\n{_search_morning(q)}" for q in queries) if queries else ""
    prompt = f"""Today is {today}. Write a morning text for this person based on what you found below.

Search results:
{results}

Rules:
- Only include information that's clearly from today or last night. Check the Published dates.
- If results for a topic look stale, outdated, or generic — skip that topic entirely. Do not make something up or paraphrase old news.
- One or two sentences per topic max. The whole thing should fit in a single text.
- Palmer's voice — no bullet points, no headers, no "good morning". Just the message.
- Plain ASCII text only. No emoji, no special characters, no dashes longer than a hyphen."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return _sms_clean(response.content[0].text.strip())


def _split_message(text: str, max_chars: int = 900) -> list[str]:
    """Split at paragraph breaks if the message exceeds max_chars."""
    if len(text) <= max_chars:
        return [text]
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    return parts if len(parts) > 1 else [text]


def send_morning_messages():
    from twilio.rest import Client
    twilio = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    from_number = os.environ["TWILIO_PHONE_NUMBER"]

    for phone in get_all_phones():
        profile = get_profile(phone)
        if not profile.get("morning_onboarded"):
            continue  # not onboarded yet — intro flow handles this
        try:
            message = generate_morning(phone)
            parts = _split_message(message)
            for part in parts:
                twilio.messages.create(body=part, from_=from_number, to=phone)
            save_message(phone, "assistant", message)
            print(f"Sent to {phone} ({len(parts)} part(s)): {message}")
        except Exception as e:
            print(f"Failed for {phone}: {e}")
