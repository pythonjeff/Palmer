import json
import os
from agent import client, _search, _build_system
from db import get_profile, upsert_profile, get_all_phones


def _get_search_queries(profile: dict) -> list[str]:
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        messages=[{"role": "user", "content": f"""Based on this user profile, what should I search for their morning briefing?

Profile: {json.dumps(profile, indent=2)}

Return a JSON array of 1-3 search queries based on what they said they want each morning. Example: ["St. Louis weather today", "Cardinals score last night"]. If unclear, return []. Just the JSON array."""}],
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

    if not profile.get("morning_onboarded"):
        prompt = "Write a morning text to this person — first one you've ever sent them unprompted. Good morning, Palmer's voice. Then ask what they'd like to know each morning — weather, traffic, sports scores, news, whatever they care about. Keep it natural, not like a form. 2-3 sentences. Just the message."
    else:
        queries = _get_search_queries(profile)
        results = "\n\n".join(f"{q}:\n{_search(q)}" for q in queries) if queries else ""
        prompt = f"Write a morning text for this person. Here's what you found:\n\n{results}\n\nWeave it in naturally, short and useful, Palmer's voice. Just the message."

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def send_morning_messages():
    from twilio.rest import Client
    twilio = Client(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    from_number = os.environ["TWILIO_PHONE_NUMBER"]

    for phone in get_all_phones():
        try:
            message = generate_morning(phone)
            twilio.messages.create(body=message, from_=from_number, to=phone)
            if not get_profile(phone).get("morning_onboarded"):
                upsert_profile(phone, {"morning_onboarded": True})
            print(f"Sent to {phone}: {message}")
        except Exception as e:
            print(f"Failed for {phone}: {e}")
