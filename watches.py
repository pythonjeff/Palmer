import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta, date as _date

from agent import _build_system, base_system
from llm import client, HAIKU_MODEL, SONNET_MODEL
from smstext import _sms_clean, URL_RE
from datafeeds import _search_raw
from sources import source_tier, canonical_domain, corroborated
from userprofile import _is_duplicate_subject, _user_already_covered
from db import (
    get_active_watches, update_watch_alerted, get_messages_after,
    claim_watch_alert, set_watch_genre, update_watch_story, get_profile,
)
from rubrics import classify_genre, rubric_for

DAILY_ALERT_MAX = 4

def _daily_ok(watch: dict, cap: int = DAILY_ALERT_MAX, today: str | None = None) -> bool:
    """True if this watch is under the daily alert cap, on the READER's day.

    It keyed on the dyno's UTC date, which rolls at 17:00 Pacific — mid-evening
    — so the allowance reset in the middle of a user's evening and could be
    spent again before their own day was over. alerts.py had the same defect
    and was fixed; this is the same fix, and the write side (db's
    update_watch_alerted) takes the same date so the two agree.

    `cap` is lowered for users whose reactions say Palmer is texting too much —
    see tapback.pacing_factor. Defaults keep existing callers unaffected."""
    today = today or _date.today().isoformat()
    if watch.get("daily_alert_date") != today:
        return True  # new day, count resets
    return watch.get("daily_alert_count", 0) < cap


def _user_engaged(watch: dict) -> bool:
    """True if the user replied after the last alert — lowers the threshold next check."""
    last_alerted = watch.get("last_alerted")
    if not last_alerted:
        return False
    msgs = get_messages_after(watch["phone"], last_alerted)
    return any(m["role"] == "user" for m in msgs)


def _check_watch_hit(results: str, description: str, recent_summaries: list[str],
                     engaged: bool, genre: str, story_state: str | None = None) -> bool:
    """Ask Haiku if results contain something a friend would text about, using
    the genre-specific rubric so the bar matches the topic AND the current
    story state so we don't re-alert on the same arc.

    Two gates in one prompt:
      1. Does this clear the friend-would-text bar for the genre?
      2. Does it ADVANCE the current story state (or is it a rehash)?

    `engaged` = user replied to the last alert; softens the rubric footer so
    finer developments qualify while the user is following the story closely.
    `story_state` is the last-known 1-2 sentence state ('where we are in the
    story'); None on first fire, filled in thereafter by _update_story_state."""
    dedup_block = ""
    if recent_summaries:
        lines = "\n".join(f'- "{s}"' for s in recent_summaries)
        dedup_block = f"\nAlready sent titles — reply NO if results cover the same events:\n{lines}\n"

    story_block = ""
    if story_state:
        story_block = (
            f"\nCurrent story state (already told to the user — reply NO if the candidate "
            f"just rehashes this, YES only if it MEANINGFULLY advances it):\n"
            f"\"{story_state}\"\n"
        )

    footer = (
        "The user is following this closely right now — the bar is lower. Meaningful "
        "in-story updates qualify, not just top-of-cycle news. Still say NO to pure "
        "recap, opinion, or content that adds no new facts."
        if engaged else
        "Bar is high: only fire on the kind of thing a friend would actually text about, "
        "not routine coverage."
    )

    prompt = (
        f'Watch topic: "{description}"\n'
        f"Genre: {genre}\n\n"
        f"Rubric — the bar a real friend would text at for this kind of topic:\n"
        f"{rubric_for(genre)}\n"
        f"{footer}\n"
        f"{story_block}"
        f"{dedup_block}\n"
        f"Candidate news:\n{results[:2000]}\n\n"
        "Does this candidate clear the bar AND advance the story? Reply YES or NO."
    )
    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip().upper().startswith("YES")


def _update_story_state(watch_id: int, previous_state: str | None,
                        new_alert_title: str, new_alert_content: str) -> None:
    """Fold a newly-fired alert into the watch's rolling story state via Haiku,
    then persist. Silent on any failure — the alert already went out, we just
    lose the semantic-dedup benefit for the next tick."""
    context = f"Previous state: {previous_state or '(none — first alert on this watch)'}\n"
    context += f"New alert title: {new_alert_title}\n"
    if new_alert_content:
        context += f"New alert content: {new_alert_content[:400]}\n"
    prompt = (
        "Write ONE plain sentence (max two) capturing where the story is NOW, "
        "after this new alert. This will be shown to a future scorer as 'the "
        "user already knows this — reply NO to rehash, YES only if advancing.' "
        "Keep facts only, no framing.\n\n"
        f"{context}\n"
        "Reply with just the sentence."
    )
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = response.content[0].text.strip()
        if summary:
            update_watch_story(watch_id, summary[:400])
    except Exception as e:
        print(f"_update_story_state failed for watch {watch_id}: {e}")


def _watch_genre(watch: dict) -> str:
    """Return the watch's genre, classifying + persisting on first use.
    Never raises — falls back to 'other' via classify_genre's own guarantee."""
    genre = watch.get("genre")
    if genre:
        return genre
    genre = classify_genre(watch.get("description") or "")
    try:
        set_watch_genre(watch["id"], genre)
    except Exception as e:
        print(f"set_watch_genre failed for watch {watch.get('id')}: {e}")
    watch["genre"] = genre
    return genre


def _url_reachable(url: str, timeout: int = 4) -> bool:
    """HEAD check that the URL resolves. Treats 405 (HEAD not allowed) as reachable."""
    try:
        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400
    except urllib.error.HTTPError as e:
        return e.code == 405  # HEAD blocked but page exists
    except Exception:
        return False


def _best_result(results: list[dict]) -> dict | None:
    """Return the highest-trust reachable result. Ranks by (tier, -score) so a
    tier-1 newsroom beats a higher-scoring blog."""
    ranked = sorted(
        results,
        key=lambda r: (source_tier(r.get("url", "")), -(r.get("score") or 0)),
    )
    for r in ranked:
        url = r.get("url", "")
        if url and _url_reachable(url):
            return r
    return None


def _format_alert(result: dict) -> str:
    """The raw facts of an alert: headline, then link. Not what gets sent.

    Used for the dedup gates and for the drafting fallback. Deduping on the
    facts rather than on the drafted line is deliberate — the subject is the
    news, not Palmer's phrasing, and a drafted line is a lossy paraphrase of
    it. It also keeps a Sonnet call from running behind every candidate the
    gates then throw away, which matters at a 30-minute cadence across every
    watch on the system."""
    title = (result.get("title") or "").strip()
    url = (result.get("url") or "").strip()
    if title and url:
        return _sms_clean(f"{title}\n{url}")
    return _sms_clean(title or url)


# The line has to leave room for a URL after it.
ALERT_MAX_CHARS = 200


def _draft_alert(phone: str, watch: dict, top: dict, fallback: str) -> str:
    """The alert in Palmer's voice, with the link last and alone.

    This was the one user-facing message in the system with no Palmer in it —
    a bare `title\nurl`, no system prompt, no calibration, against the rule
    that anything the user reads is drafted through _build_system. Its sibling
    alerts.py has always done this; the two paths simply diverged.

    Never raises. Every failure falls back to the raw headline, which is what
    production sent before this existed — the same discipline as
    morning._compose_morning, where a failed draft still delivers something."""
    url = (top.get("url") or "").strip()
    try:
        system = _build_system(phone, include_recent=True)
    except Exception as e:
        # Not "no system prompt": that would drop the voice, the calibration
        # AND every NEVER rule from a message still going out. Same call
        # price_alert.py makes.
        print(f"_draft_alert: _build_system failed for {phone}: {e}")
        system = base_system()
    try:
        domain = canonical_domain(url) or "unknown source"
        resp = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=120,
            system=system,
            messages=[{"role": "user", "content": (
                f"They asked you to watch this: {watch.get('description') or 'a story'}.\n"
                f"It just moved. Here is the story, and it is ALL you have:\n\n"
                f"[{domain}] {top.get('title') or ''}\n{(top.get('content') or '')[:400]}\n\n"
                "Write ONE short text telling them, like you saw it and thought of them. "
                "Lead with what happened. No opener, no ceremony.\n"
                "You can see the headline and the snippet above and NOTHING else — do not "
                "add background, consequence or history you were not given, and do not "
                "invent a detail.\n"
                "Don't say 'alert', 'update' or 'watch' — you're a friend, not an app.\n"
                "Do NOT write a URL; the link is added after your line.\n"
                f"No emoji, no markdown, plain text, under {ALERT_MAX_CHARS} characters."
            )}],
        )
        line = _sms_clean((resp.content[0].text or "").strip())
    except Exception as e:
        print(f"_draft_alert: drafting failed for watch {watch.get('id')}: {e}")
        return fallback
    # A model-invented URL cannot ride along beside the real one: a message
    # carrying two links draws no preview, and the wrong one may not resolve.
    line = URL_RE.sub("", line).strip()
    if not line:
        return fallback
    return _sms_clean(f"{line}\n{url}") if url else _sms_clean(line)


def run_watches():
    """Check all active watches and send alerts if triggered. Called every 30 minutes by APScheduler."""
    from sms_util import send_sms
    from db import save_message

    watches = get_active_watches()
    now = datetime.now(timezone.utc)

    # One profile read per USER, not per watch. get_profile opens a fresh DB
    # connection per call and this loop covers every watch for every user, so
    # doing it inline cost N connections a tick for N watches.
    from tapback import pacing_factor
    from timeutil import local_today
    caps = {}
    days = {}
    for phone in {w["phone"] for w in watches}:
        try:
            profile = get_profile(phone)
            caps[phone] = max(1, round(DAILY_ALERT_MAX / pacing_factor(profile)))
            # The reader's day rides along on the read that was already happening.
            days[phone] = local_today(profile.get("timezone")).isoformat()
        except Exception:
            caps[phone] = DAILY_ALERT_MAX

    for watch in watches:
        try:
            # Cooldown: respect per-watch minimum gap between alerts
            if watch["last_alerted"]:
                last = datetime.fromisoformat(watch["last_alerted"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if now - last < timedelta(hours=watch["cooldown_hours"]):
                    continue

            # Daily cap per watch. Normally DAILY_ALERT_MAX; lower for users whose
            # reactions say Palmer is texting too much (see tapback.pacing_factor).
            today = days.get(watch["phone"])
            if not _daily_ok(watch, caps.get(watch["phone"], DAILY_ALERT_MAX), today):
                continue

            # Collect raw results across all queries, deduped by URL
            all_raw: list[dict] = []
            seen_urls: set[str] = set()
            for query in watch["queries"]:
                for r in _search_raw(query, days=1, max_age_hours=12):
                    url = r.get("url", "")
                    if url not in seen_urls:
                        seen_urls.add(url)
                        all_raw.append(r)

            if not all_raw:
                continue

            if not corroborated(all_raw):
                domains = {canonical_domain(r.get("url", "")) for r in all_raw}
                domains.discard("")
                print(f"Watch {watch['id']}: no corroboration ({len(domains)} domain(s), no tier-1), skipping")
                continue

            # Build combined text for hit-check (Haiku reads title + snippet, not full content)
            combined = "\n\n".join(
                f"{r['title']}\nPublished: {r.get('published_date', 'unknown')}\n{r.get('content', '')[:300]}"
                for r in all_raw
            )

            engaged = _user_engaged(watch)
            genre = _watch_genre(watch)
            if not _check_watch_hit(combined, watch["description"], watch["recent_summaries"],
                                    engaged, genre, story_state=watch.get("story_state")):
                continue

            # Pick best reachable result — falls through to next if top URL is dead
            top = _best_result(all_raw)
            if not top:
                print(f"Watch {watch['id']}: all URLs unreachable, skipping alert")
                continue

            alert = _format_alert(top)
            if not alert:
                continue

            if _is_duplicate_subject(watch["phone"], alert):
                print(f"Watch {watch['id']}: subject already covered by a recent message, skipping")
                continue

            # User-mention dedup: the user brought this story up themselves.
            # Suppress even if all our own gates would fire — they already know.
            if _user_already_covered(watch["phone"], alert):
                print(f"Watch {watch['id']}: user already mentioned this story themselves, skipping")
                continue

            if not claim_watch_alert(watch["id"], watch["cooldown_hours"]):
                print(f"Watch {watch['id']}: already claimed by another process, skipping")
                continue

            # Drafted LAST — after both dedup gates and after the claim — so a
            # Sonnet call is only spent on an alert that is actually going out.
            body = _draft_alert(watch["phone"], watch, top, fallback=alert)

            # A failed send must not become history. The watch claim above stays
            # spent either way: it is a rate limit, not a delivery record, so
            # burning one cooldown is far safer than retrying every tick against
            # a body the guard will block identically each time. (The inverse of
            # the reminder rule, where the claim IS the delivery record.)
            #
            # No add_status_callback here on purpose: send_sms sees the URL and
            # turns the callback off itself, so a new sender inherits the rule
            # instead of having to remember it.
            if not send_sms(watch["phone"], body):
                print(f"Watch {watch['id']}: send failed, not recording it as sent")
                continue
            # Save what was SENT. History is fed back to the model, and
            # _is_duplicate_subject reads it — both have to see the real message.
            save_message(watch["phone"], "assistant", body, kind="watch")

            # Use title for dedup context (shorter than full alert with URL)
            title = (top.get("title") or alert)[:120]
            recent = (watch["recent_summaries"] + [title])[-3:]
            alert_url = top.get("url") or None
            alert_domain = canonical_domain(alert_url) if alert_url else None
            update_watch_alerted(watch["id"], title, recent, url=alert_url,
                                 domain=alert_domain, today=today)
            # Fold the alert into the rolling story state so the next tick's
            # scorer sees 'the user already knows this — reply YES only if
            # advancing.' Failure is silent — the alert already went out.
            _update_story_state(
                watch["id"], watch.get("story_state"),
                new_alert_title=title,
                new_alert_content=top.get("content") or "",
            )
            print(f"Watch {watch['id']} triggered for {watch['phone']} (engaged={engaged}): {alert[:80]}")

        except Exception as e:
            print(f"Watch {watch['id']} check failed: {e}")
