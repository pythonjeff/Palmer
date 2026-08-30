from datetime import datetime

from agent import _build_system
from llm import client, HAIKU_MODEL, SONNET_MODEL
from smstext import _sms_clean
from userprofile import _is_duplicate_subject
from db import get_all_profiles, upsert_profile, save_message, get_history, claim_daily_guard
from morning import _local_now, _local_today


def _should_send_followup(profile: dict) -> bool:
    """Check all guards: onboarded, timezone known, time window, 3-day gap, threads exist."""
    if not profile.get("morning_onboarded"):
        return False
    tz = profile.get("timezone")
    if not tz:
        return False
    try:
        local_now = _local_now(tz)
    except Exception:
        return False
    if not (13 <= local_now.hour < 19):  # 1pm–7pm local only
        return False
    last_sent = profile.get("followup_sent_date")
    if last_sent:
        try:
            from tapback import pacing_factor
            gap = min(round(3 * pacing_factor(profile)), 14)
            last_date = datetime.fromisoformat(last_sent).date()
            if (local_now.date() - last_date).days < gap:
                return False
        except Exception:
            pass
    # ongoing_threads ONLY. life_context is a paragraph about someone's life,
    # not a thread with a follow-up, and letting it trigger a check-in is what
    # let the model invent one: _pick_thread was handed prose and asked to find
    # something "worth a check-in today" in it.
    return bool(profile.get("ongoing_threads"))


def _pick_thread(profile: dict, history: list[dict]) -> str | None:
    """The one stored thread most worth a check-in today, or None.

    Returns a string COPIED FROM THE PROFILE, never the model's own words. The
    old version returned whatever Haiku emitted (anything under 80 chars) and
    handed it straight to the drafter, so a confabulated thread was written up
    as though it were real — a specific-sounding question about something that
    never happened. userprofile.topic_already_covered already solved this exact
    problem by echo-matching, for the same reason: an echo can be checked
    against the list, a paraphrase cannot.

    Fails closed. No match means no text."""
    threads = [t for t in (profile.get("ongoing_threads") or []) if t]
    # Don't check in twice running on the same thread. Nothing recorded this
    # before, so one thread could resurface every few days indefinitely.
    last = (profile.get("followup_last_thread") or "").strip().lower()
    if last and len(threads) > 1:
        threads = [t for t in threads if t.strip().lower() != last]
    life_ctx = (profile.get("life_context") or "").strip()
    if not threads:
        return None
    threads_str = "\n".join(f"- {t}" for t in threads[:5])
    life_str = f"Life context: {life_ctx}" if life_ctx else ""
    recent_str = "\n".join(
        f"{m['role']}: {m['content'][:150]}" for m in history[-6:]
    ) if history else ""

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=50,
            messages=[{"role": "user", "content": f"""An AI friend is deciding whether to send a proactive check-in text today.

Open threads:
{threads_str}
{life_str}

Recent conversation:
{recent_str}

Is there ONE thread above that is genuinely worth a brief check-in today? Something time-sensitive, emotionally loaded, or where progress would be expected?

Reply with that thread copied EXACTLY as it appears in the list above, and nothing else. Reply NONE if nothing warrants interrupting them, or if the only things you could say would be guesses."""}],
        )
        result = response.content[0].text.strip().strip('"').rstrip(".")
        if not result or result.upper().startswith("NONE"):
            return None
        # Match the echo back against the stored list and return the STORED
        # string. Anything invented dies here instead of becoming a text about
        # something that never happened.
        for t in threads:
            if t.strip().lower() == result.strip().lower():
                return t
        print(f"followup: Haiku named a thread not on the list ({result[:60]!r}), skipping")
        return None
    except Exception:
        return None


def _draft_followup(phone: str, thread: str) -> str:
    """Draft a one-line check-in in Palmer's voice."""
    system = _build_system(phone, include_recent=True)
    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=100,
            system=system,
            messages=[{"role": "user", "content": (
                f"Send a brief, casual check-in about this open thread: {thread}. "
                "One sentence, no opener, no ceremony. Palmer's voice, and not a "
                "generic 'hey how did that go'.\n\n"
                "Use ONLY what the thread text and the recent messages above actually "
                "say. Do not invent a detail, a name, a date or an outcome, and do not "
                "assume anything has happened since. If the thread doesn't give you "
                "enough to be specific, ask one short question about it and nothing "
                "else. Check recent messages so you're not reusing the shape of your "
                "last check-in."
            )}],
        )
        return _sms_clean(response.content[0].text.strip())
    except Exception:
        return ""


def run_followups():
    """Send a proactive check-in if warranted. Called every 2 hours by APScheduler.

    Every bail path after the claim RESTORES the previous followup_sent_date
    rather than nulling it. That is not tidiness: claim_daily_guard overwrites
    the field with today, so writing None on a bail erased the record of the
    last real send — and _should_send_followup measures the 3-to-14-day pacing
    gap against exactly that field. A user whose thread never qualified would
    have their gap silently voided, leaving only the one-per-local-day guard.
    The gap is the thing standing between "a check-in" and "a drumbeat"."""
    from sms_util import send_sms

    for phone, profile in get_all_profiles():
        try:
            if not _should_send_followup(profile):
                continue

            prior = profile.get("followup_sent_date")

            def _release():
                """Put the guard back the way it was, not to None."""
                upsert_profile(phone, {"followup_sent_date": prior})

            today_str = _local_today(profile["timezone"]).isoformat()
            if not claim_daily_guard(phone, "followup_sent_date", today_str):
                continue

            history = get_history(phone, limit=10)
            thread = _pick_thread(profile, history)
            if not thread:
                _release()
                continue

            message = _draft_followup(phone, thread)
            if not message:
                _release()
                continue

            if _is_duplicate_subject(phone, message):
                _release()
                print(f"Follow-up skipped for {phone}: subject already covered by a recent message")
                continue

            if send_sms(phone, message):
                save_message(phone, "assistant", message, kind="followup")
                # Remember what was just asked about so the next pick moves on.
                upsert_profile(phone, {"followup_last_thread": thread})
                print(f"Follow-up sent to {phone}: {message[:80]}")
            else:
                _release()
        except Exception as e:
            print(f"Follow-up check failed for {phone}: {e}")
