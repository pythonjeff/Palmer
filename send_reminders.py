from dotenv import load_dotenv
load_dotenv()

from db import claim_due_reminders, save_message, get_profile
from agent import _sms_clean, client, _build_system, SONNET_MODEL


def _personalize_reminder(phone: str, text: str, profile: dict) -> str:
    """Write the reminder as the SAME Palmer the user talks to.

    This used to carry its own one-line persona ("You're Palmer, a sharp, casual
    texting friend") plus a raw profile dump, which meant reminders never saw the
    calibrated register, the reaction history, or any of SYSTEM_PROMPT — a user
    who asked for less sarcasm still got the breezy default here. _build_system
    supplies profile and recent history, so both are dropped from the prompt.
    """
    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=100,
            system=_build_system(phone, include_recent=True),
            messages=[{"role": "user", "content": f"""Write the reminder text for this person.

Reminder: {text}

Rules:
- Sound like a friend, not an app. "hey, didn't you have that interview today?" not "Reminder: interview"
- If the context is stressful or significant, dial in — don't be breezy about a medical appointment
- Under 120 characters. Plain text only, no emoji.
- Just the message, nothing else."""}],
        )
        return _sms_clean(response.content[0].text.strip()) or _sms_clean(f"hey - {text}")
    except Exception as e:
        print(f"_personalize_reminder failed for {phone}: {e}")
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
