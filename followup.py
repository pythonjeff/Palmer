from datetime import datetime

from agent import client, _build_system, _sms_clean, _is_duplicate_subject, HAIKU_MODEL, SONNET_MODEL
from db import get_all_phones, get_profile, upsert_profile, save_message, get_history, claim_daily_guard
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
    threads = profile.get("ongoing_threads") or []
    life_ctx = (profile.get("life_context") or "").strip()
    return bool(threads or life_ctx)


def _pick_thread(profile: dict, history: list[dict]) -> str | None:
    """Use Haiku to choose the one open thread most worth a check-in today, or NONE."""
    threads = profile.get("ongoing_threads") or []
    life_ctx = (profile.get("life_context") or "").strip()
    threads_str = "\n".join(f"- {t}" for t in threads[:5]) if threads else ""
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

Is there ONE thread above that is genuinely worth a brief check-in today? Something time-sensitive, emotionally loaded, or where progress would be expected? Reply with just the thread topic (max 8 words), or NONE if nothing warrants interrupting them."""}],
        )
        result = response.content[0].text.strip()
        if not result or result.upper() == "NONE" or len(result) > 80:
            return None
        return result
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
                "One sentence, no opener, no ceremony. Make it specific and alive — "
                "an observation, a pointed question only if you actually need the answer, or a statement that just shows you remembered. "
                "Not a generic 'hey how did that go' — something that sounds like a real person had a real thought. "
                "Check recent messages to make sure you're not using the same shape as the last check-in. Palmer's voice."
            )}],
        )
        return _sms_clean(response.content[0].text.strip())
    except Exception:
        return ""


def run_followups():
    """Send a proactive check-in if warranted. Called every 4 hours by APScheduler."""
    from sms_util import send_sms

    for phone in get_all_phones():
        try:
            profile = get_profile(phone)
            if not _should_send_followup(profile):
                continue

            today_str = _local_today(profile["timezone"]).isoformat()
            if not claim_daily_guard(phone, "followup_sent_date", today_str):
                continue

            history = get_history(phone, limit=10)
            thread = _pick_thread(profile, history)
            if not thread:
                upsert_profile(phone, {"followup_sent_date": None})
                continue

            message = _draft_followup(phone, thread)
            if not message:
                upsert_profile(phone, {"followup_sent_date": None})
                continue

            if _is_duplicate_subject(phone, message):
                upsert_profile(phone, {"followup_sent_date": None})
                print(f"Follow-up skipped for {phone}: subject already covered by a recent message")
                continue

            if send_sms(phone, message):
                save_message(phone, "assistant", message)
                print(f"Follow-up sent to {phone}: {message[:80]}")
            else:
                upsert_profile(phone, {"followup_sent_date": None})
        except Exception as e:
            print(f"Follow-up check failed for {phone}: {e}")
