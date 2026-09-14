"""One paced, unprompted text about something Palmer actually knows.

Three kinds of subject, all copied from data that already exists. Nothing is
fetched for this job that the page has not already fetched, and nothing in a
subject is the model's own words:

  thread   an ongoing_thread from the profile ("interviewing at Stripe next week")
  team     a followed team's game yesterday or today — the same rows the page's
           Scores section renders (sports.team_day, free, cached per league)
  news     a headline the page already holds for one of their topics — the
           morning's news pass read back, never a second search

Haiku picks ONE by echoing its text exactly, or NONE, and the echo is matched
back against the candidate list. A paraphrase or an invention matches nothing,
and the tick sends nothing. That echo rule is the whole defence, and it is what
lets the job take on news and sports without a new way to make things up.

The gap between check-ins is measured in days, not ticks (GAP_DAYS), so the
2-hour scheduler tick only decides how soon after the gap lapses the text goes
out. It used to be 3 days, which read as a drumbeat; with teams and news in
the pool there is nearly always a candidate, so the gap is the whole rate limit.
"""
from dataclasses import dataclass
from datetime import datetime

from agent import _build_system
from llm import client, HAIKU_MODEL, SONNET_MODEL
from smstext import _sms_clean
from userprofile import _is_duplicate_subject
from db import get_all_profiles, upsert_profile, save_message, get_history, claim_daily_guard
from morning import _local_now, _local_today

# Days between check-ins for a user with no negative reactions on record;
# tapback.pacing_factor stretches it, GAP_MAX_DAYS caps the stretch.
GAP_DAYS = 10
GAP_MAX_DAYS = 28

# A headline older than this is not something to bring up out of the blue.
# The morning refreshes headlines daily for anyone onboarded, so this only
# bites when a user's morning has been failing.
NEWS_MAX_AGE_HOURS = 36


@dataclass(frozen=True)
class Subject:
    kind: str            # "thread" | "team" | "news"
    text: str            # the line the model sees and must echo
    url: str | None = None
    source: str | None = None


def _should_send_followup(profile: dict) -> bool:
    """Check all guards: onboarded, timezone known, time window, pacing gap,
    and at least one pool a subject could come from."""
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
            gap = min(round(GAP_DAYS * pacing_factor(profile)), GAP_MAX_DAYS)
            last_date = datetime.fromisoformat(last_sent).date()
            if (local_now.date() - last_date).days < gap:
                return False
        except Exception:
            pass
    # ongoing_threads ONLY on the personal side. life_context is a paragraph
    # about someone's life, not a thread with a follow-up, and letting it
    # trigger a check-in is what let the model invent one.
    return bool(profile.get("ongoing_threads") or profile.get("followed_teams")
                or profile.get("morning_topics"))


def _load_payload(profile: dict) -> dict | None:
    """The user's page as last built. A read, never a refresh — this job spends
    nothing on data."""
    token = profile.get("home_token")
    if not token:
        return None
    try:
        from home import load
        return load(token)
    except Exception:
        return None


def _candidates(profile: dict, payload: dict | None, today) -> list[Subject]:
    """Every real thing a check-in could be about, as text the model must echo.

    The last subject is left out so one thing cannot resurface every gap —
    unless it is the only thing there is, because excluding it would end
    check-ins for good."""
    out: list[Subject] = []
    for t in (profile.get("ongoing_threads") or []):
        if t:
            out.append(Subject("thread", str(t).strip()))

    teams = [t for t in (profile.get("followed_teams") or []) if t.get("abbrev")]
    if teams:
        try:
            from sports import team_day, result_line
            for team in teams:
                day = team_day(team, today)
                name = team.get("name") or team.get("abbrev")
                if day.get("last"):
                    out.append(Subject("team", f"{name}: yesterday {result_line(day['last'], team)}"))
                if day.get("today"):
                    out.append(Subject("team", f"{name}: today {result_line(day['today'], team)}"))
        except Exception as e:
            print(f"followup: team lookup failed: {type(e).__name__}: {e}")

    if payload:
        fetched = (payload.get("fetched") or {}).get("headlines") or 0
        fresh = (datetime.now().timestamp() - fetched) < NEWS_MAX_AGE_HOURS * 3600
        if fresh:
            for h in (payload.get("headlines") or []):
                title = (h.get("title") or "").strip()
                if title:
                    out.append(Subject("news", f"{title} ({h.get('topic') or 'news'})",
                                       url=h.get("url"), source=h.get("source")))

    last = (profile.get("followup_last_thread") or "").strip().lower()
    if last and len(out) > 1:
        out = [s for s in out if s.text.strip().lower() != last]
    return out


def _pick_subject(profile: dict, history: list[dict], candidates: list[Subject]) -> Subject | None:
    """The one candidate most worth a text today, or None.

    Returns a Subject COPIED FROM THE LIST, never the model's own words. The
    old version returned whatever Haiku emitted and handed it straight to the
    drafter, so a confabulated thread was written up as though it were real.
    userprofile.topic_already_covered solved the same problem by echo-matching,
    for the same reason: an echo can be checked against the list, a paraphrase
    cannot. Fails closed. No match means no text."""
    if not candidates:
        return None
    listed = "\n".join(f"- [{s.kind}] {s.text}" for s in candidates[:12])
    recent_str = "\n".join(
        f"{m['role']}: {m['content'][:150]}" for m in history[-6:]
    ) if history else ""
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=80,
            messages=[{"role": "user", "content": f"""Palmer texts this person a short unprompted note every week or two. Deciding what today's is about, if anything.

Candidates (kind in brackets):
{listed}

Recent conversation:
{recent_str}

Pick the ONE most worth a text today: a game today or a result from yesterday, a story on a subject they follow that actually moved, or a personal thread where progress would be expected by now. Prefer the timely thing over the personal one unless the personal one is clearly due.

Reply with that candidate's text copied EXACTLY as it appears after the bracket, and nothing else. Reply NONE if nothing warrants interrupting them, or if the only things you could say would be guesses."""}],
        )
        result = response.content[0].text.strip().strip('"').rstrip(".")
        if not result or result.upper().startswith("NONE"):
            return None
        wanted = result.strip().lower()
        for s in candidates:
            if s.text.strip().lower() == wanted:
                return s
        print(f"followup: Haiku named a subject not on the list ({result[:60]!r}), skipping")
        return None
    except Exception:
        return None


def _draft_followup(phone: str, subject: Subject) -> str:
    """One line in Palmer's voice about the subject, and nothing the data does
    not say. A news subject carries its link last and alone, appended in code
    rather than asked of the model, so it survives the draft byte for byte."""
    system = _build_system(phone, include_recent=True)
    common = (
        "One sentence, no opener, no ceremony. Palmer's voice. Use ONLY what the "
        "subject line and the recent messages above actually say. Do not invent a "
        "detail, a name, a date, a player, a storyline or an outcome, and do not "
        "assume anything has happened since. Check recent messages so you're not "
        "reusing the shape of your last unprompted text."
    )
    if subject.kind == "team":
        ask = (f"Send a brief text about their team, from this and nothing else: "
               f"{subject.text}. A take or a plain heads-up — not a recap. ")
    elif subject.kind == "news":
        ask = (f"Send a brief text about this story on a subject they follow: "
               f"{subject.text}" + (f", via {subject.source}" if subject.source else "")
               + ". A take or a plain heads-up — do not summarize beyond the headline, "
               "and do not include or mention a link; one is added after. ")
    else:
        ask = (f"Send a brief, casual check-in about this open thread: {subject.text}. "
               "Not a generic 'hey how did that go'. If the thread doesn't give you "
               "enough to be specific, ask one short question about it and nothing else. ")
    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=120,
            system=system,
            messages=[{"role": "user", "content": ask + common}],
        )
        text = _sms_clean(response.content[0].text.strip())
    except Exception:
        return ""
    if text and subject.kind == "news" and subject.url:
        text = f"{text}\n{subject.url}"
    return text


def run_followups():
    """Send a paced check-in if one is due. Called every 2 hours by APScheduler.

    Every bail path after the claim RESTORES the previous followup_sent_date
    rather than nulling it. That is not tidiness: claim_daily_guard overwrites
    the field with today, so writing None on a bail erased the record of the
    last real send — and _should_send_followup measures the pacing gap against
    exactly that field. The gap is the thing standing between "a check-in" and
    "a drumbeat"."""
    from sms_util import send_sms

    for phone, profile in get_all_profiles():
        try:
            if not _should_send_followup(profile):
                continue

            prior = profile.get("followup_sent_date")

            def _release():
                """Put the guard back the way it was, not to None."""
                upsert_profile(phone, {"followup_sent_date": prior})

            today = _local_today(profile["timezone"])
            if not claim_daily_guard(phone, "followup_sent_date", today.isoformat()):
                continue

            candidates = _candidates(profile, _load_payload(profile), today)
            if not candidates:
                _release()
                continue

            history = get_history(phone, limit=10)
            subject = _pick_subject(profile, history, candidates)
            if not subject:
                _release()
                continue

            message = _draft_followup(phone, subject)
            if not message:
                _release()
                continue

            if _is_duplicate_subject(phone, message):
                _release()
                print(f"Follow-up skipped for {phone}: subject already covered by a recent message")
                continue

            if send_sms(phone, message):
                save_message(phone, "assistant", message, kind="followup")
                # Remember what was just sent so the next pick moves on.
                upsert_profile(phone, {"followup_last_thread": subject.text})
                print(f"Follow-up sent to {phone} [{subject.kind}]: {message[:80]}")
            else:
                _release()
        except Exception as e:
            print(f"Follow-up check failed for {phone}: {e}")
