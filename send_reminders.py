from dotenv import load_dotenv
load_dotenv()

from db import claim_due_reminders, save_message, get_profile
from agent import _build_system
from llm import client, SONNET_MODEL
from smstext import _sms_clean


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
- Sound like a person, not an app. "didn't you have that interview today?" not "Reminder: interview". Plain and direct — this is the nudge they asked for, not a conversation opener.
- If the context is stressful or significant, dial in — don't be breezy about a medical appointment
- Under 120 characters. Plain text only, no emoji.
- Just the message, nothing else."""}],
        )
        return _sms_clean(response.content[0].text.strip()) or _sms_clean(f"hey - {text}")
    except Exception as e:
        print(f"_personalize_reminder failed for {phone}: {e}")
        return _sms_clean(f"hey - {text}")


def send_due_reminders():
    """Deliver claimed reminders, then re-arm the recurring ones.

    In-batch dedup rather than the _is_duplicate_subject gate the other
    proactive senders use, and that difference is deliberate. A reminder is
    something the user explicitly asked to receive at a named time, so
    suppressing it because Palmer happened to mention the topic six hours ago
    would defeat the request — a missed reminder is worse than the duplicate it
    would prevent. The failure actually observed was four near-identical rows
    firing in the same minute, which is a within-tick problem, so the guard is
    scoped to the tick.
    """
    from sms_util import send_sms
    from db import rearm_reminder, _similar_reminder_text, _parse_due
    from timeutil import next_occurrence

    reminders = claim_due_reminders()
    if not reminders:
        return

    already: dict[str, list[str]] = {}
    for r in reminders:
        try:
            phone = r["phone"]
            if any(_similar_reminder_text(prev, r["text"]) for prev in already.get(phone, ())):
                # A redundant row is left claimed and NOT re-armed, so a
                # recurring duplicate retires itself instead of colliding with
                # its twin every period.
                print(f"Reminder {r['id']}: duplicate of another due this tick, dropping")
                continue

            profile = get_profile(phone)
            body = _personalize_reminder(phone, r["text"], profile)
            sent = send_sms(phone, body)
            if sent:
                save_message(phone, "assistant", body, kind="reminder")
            already.setdefault(phone, []).append(r["text"])
            print(f"Sent reminder {r['id']} to {phone}: {body}")

            # Re-arm regardless of whether the send succeeded. The claim already
            # consumed this occurrence, so bailing here on a Twilio hiccup would
            # silently end a standing reminder — the opposite of the daily-guard
            # jobs, where releasing the claim is right because the claim IS the
            # delivery record and a later tick can retry the same occurrence.
            if r.get("recurrence"):
                base = _parse_due(r.get("due_at"))
                nxt = next_occurrence(base, r["recurrence"], profile.get("timezone")) if base else None
                if nxt and rearm_reminder(r["id"], nxt.isoformat()):
                    print(f"Reminder {r['id']} re-armed ({r['recurrence']}) for {nxt.isoformat()}")
        except Exception as e:
            print(f"Failed reminder {r['id']} to {r['phone']}: {e}")


if __name__ == "__main__":
    send_due_reminders()
