from dotenv import load_dotenv
load_dotenv()

import json
import os
from db import claim_due_reminders, save_message, get_profile, get_history
from agent import _sms_clean, client, HAIKU_MODEL


def _personalize_reminder(phone: str, text: str, profile: dict) -> str:
    """Use Haiku to write the reminder in Palmer's voice with profile + recent context."""
    recent = get_history(phone, limit=6)
    recent_block = ""
    if recent:
        recent_block = "\nRecent texts:\n" + "\n".join(
            f"{m['role']}: {m['content'][:200]}" for m in recent
        )
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": f"""You're Palmer, a sharp, casual texting friend. Write a reminder text for this person.

Reminder: {text}
What you know about them: {json.dumps(profile) if profile else "nothing yet"}{recent_block}

Rules:
- Sound like a friend, not an app. "hey, didn't you have that interview today?" not "Reminder: interview"
- If the context is stressful or significant, dial in — don't be breezy about a medical appointment
- Under 120 characters. Plain text only, no emoji.
- Just the message, nothing else."""}],
        )
        return _sms_clean(response.content[0].text.strip())
    except Exception:
        return _sms_clean(f"hey - {text}")


def send_due_reminders():
    from sms_util import send_sms
    reminders = claim_due_reminders()
    if not reminders:
        return

    for r in reminders:
        try:
            profile = get_profile(r["phone"])
            body = _personalize_reminder(r["phone"], r["text"], profile)
            send_sms(r["phone"], body)
            save_message(r["phone"], "assistant", body)
            print(f"Sent reminder {r['id']} to {r['phone']}: {body}")
        except Exception as e:
            print(f"Failed reminder {r['id']} to {r['phone']}: {e}")


if __name__ == "__main__":
    send_due_reminders()
