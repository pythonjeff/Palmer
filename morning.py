import json
import os
from agent import client, _search, _build_system
from db import get_profile, upsert_profile, get_all_phones


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
    topics = profile.get("morning_topics")
    if topics:
        topic_list = ", ".join(topics)
        prompt = f"""Convert these morning briefing topics into search queries:

Topics: {topic_list}
City (if relevant): {profile.get("city") or profile.get("location") or "unknown"}

Return a JSON array of search queries, one per topic. Example: ["Bitcoin price today", "St. Louis weather today"]. Just the JSON array."""
    else:
        prompt = f"""Based on this user profile, what should I search for their morning briefing?

Profile: {json.dumps(profile, indent=2)}

Return a JSON array of 1-3 search queries based on what they said they want each morning. Example: ["St. Louis weather today", "Cardinals score last night"]. If unclear, return []. Just the JSON array."""

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
    queries = _get_search_queries(profile)
    results = "\n\n".join(f"{q}:\n{_search(q)}" for q in queries) if queries else ""
    prompt = f"Write a morning text for this person. Here's what you found:\n\n{results}\n\nWeave it in naturally, Palmer's voice. One or two sentences per topic max — the whole thing should fit in a single text. No bullet points. Just the message."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


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
            twilio.messages.create(body=message, from_=from_number, to=phone)
            print(f"Sent to {phone}: {message}")
        except Exception as e:
            print(f"Failed for {phone}: {e}")
