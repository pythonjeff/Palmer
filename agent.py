"""Palmer's conversation loop: system-prompt assembly, tool dispatch, reply generation.

The rest of what used to live here was split out into llm, netutil, smstext,
prompts, tools_def, weather, datafeeds and userprofile. Import those directly —
agent no longer re-exports them.

_build_system is the one helper siblings still take from here: it assembles the
system prompt for every user-facing message (see CLAUDE.md "One voice").
"""
import json
import threading
from datetime import datetime, timezone

from db import (
    init_db, get_history, save_message, get_profile, upsert_profile, save_reminder, cancel_reminders,
    HISTORY_LIMIT,
    save_watch, get_user_watches, cancel_watches,
    save_price_watch, get_user_price_watches, cancel_price_watches, set_price_watch_baseline,
)

# --- used directly by the orchestration below ---
from llm import client, SONNET_MODEL
from prompts import SYSTEM_PROMPT
from tools_def import TOOLS
from smstext import _sms_clean, _normalize_hhmm
from userprofile import topic_already_covered
from weather import _get_weather
from datafeeds import _search, _get_price, _get_gif, _fetch_media
from userprofile import _update_profile, _consolidate_history

init_db()













































































def _prompt_safe_profile(profile: dict) -> dict:
    """Profile with briefing directives stripped out.

    The profile is dumped as raw JSON into every system prompt, so anything
    phrased as an instruction reads as an order for the CURRENT message. A user
    who saved "Format: bullet points per subject" as a morning topic got labelled
    dumps in ordinary conversation, against SYSTEM_PROMPT's own no-headers rule.
    Delivery preferences belong to the briefing job (morning.py), not to replies.
    """
    if not profile:
        return profile
    # Volatile facts are dropped once stale and dated once they are a few days
    # old, so the model stops reading "Based in LA" and "active fire emergency"
    # as things that are true right now.
    from userprofile import fresh_profile_for_prompt
    safe = fresh_profile_for_prompt(profile)
    topics = safe.get("morning_topics")
    if topics:
        from morning import _is_directive
        kept = [t for t in topics if t and not _is_directive(t)]
        if len(kept) != len(topics):
            safe["morning_topics"] = kept
    return safe


def base_system() -> str:
    """SYSTEM_PROMPT with its placeholders filled and no profile.

    For paths that must still sound like Palmer when the profile read fails.
    SYSTEM_PROMPT is a template — passing it unformatted ships literal
    "{profile_block}" to the model — so a caller cannot simply fall back to the
    constant itself."""
    now = datetime.now(timezone.utc)
    return SYSTEM_PROMPT.format(
        date=now.strftime("%A, %B %d, %Y"),
        now_utc=now.strftime("%H:%M"),
        profile_block="You don't know much about this person yet.",
    )


def _build_system(phone: str, include_recent: bool = False, is_new_user: bool = False) -> str:
    profile = get_profile(phone)
    profile_block = ("What you know about them:\n" + json.dumps(_prompt_safe_profile(profile), indent=2)
                     if profile else "You don't know much about this person yet. Learn as you go.")
    from tapback import reaction_block
    profile_block += reaction_block(profile)
    if (profile or {}).get("morning_topics"):
        # The profile is dumped as raw JSON above, so anything phrased as an
        # instruction in morning_topics reads as an order for THIS reply. One
        # user had "Format: bullet points per subject" in there and it turned
        # ordinary replies into labelled dumps.
        profile_block += (
            "\n\nmorning_topics is the subject list for their SCHEDULED briefing — "
            "reference data, not instructions for this message. Any formatting or "
            "delivery preference stored in there applies to the briefing job only. "
            "Never let it change how you write a reply."
        )
    if (profile or {}).get("city"):
        # `city` is the only location any tool uses. Everything else in the
        # profile that names a place — life_context, life_summary, an old
        # thread — is background and may be months out of date. One profile
        # read `city: "Culver City"` three lines above `life_context: "Based in
        # LA"`, both true when written, and the model reconciled them by
        # putting an LA temperature under the Culver City name. Say which wins.
        profile_block += (
            f"\n\nTheir location is {profile['city']}, full stop. Other fields may mention "
            "a broader region, an old address or a trip — that is background, not where "
            "they are. Never let it override the city above, and never pair a number with "
            "a place it did not come from. A dated value shown as "
            "{\"value\": ..., \"as_of\": ...} was true then, not necessarily now."
        )
    style = (profile.get("communication_style") or "").strip() if profile else ""
    if style:
        profile_block += (
            f"\n\nCALIBRATION READ (see the CALIBRATION section): {style}\n"
            "That is your register for this person — mirror it. Anything in there that they "
            "asked for directly outranks whatever you inferred from how they text. Adjusting "
            "register never means dropping your spine."
        )
    now = datetime.now(timezone.utc)
    system = SYSTEM_PROMPT.format(
        date=now.strftime("%A, %B %d, %Y"),
        now_utc=now.strftime("%H:%M"),
        profile_block=profile_block,
    )
    if is_new_user:
        system += (
            "\n\nNEW USER CONTEXT\n"
            "This is the VERY FIRST message this person has sent you. You've never talked before. "
            "Follow the NEW USERS rules above — three cases (bare greeting, random question, or "
            "'what can you do'). Pick the case that matches what they actually said and reply "
            "accordingly. Do not mention that you were just told this is their first message."
        )
    elif (profile or {}).get("intro_sent") and not (profile or {}).get("onboarding_ask_sent"):
        missing = [f for f in ("name", "city") if not (profile or {}).get(f)]
        if missing:
            ask = "their name and what city they're in" if len(missing) == 2 else f"their {missing[0]}"
            system += (
                "\n\nONBOARDING ASK\n"
                f"You still don't know {ask}. Somewhere natural in this reply — answer whatever "
                f"they actually said first if it needs answering — work in a question for {ask}, "
                "so you can personalize things and get their morning briefing dialed in. One line, "
                "not a form, not your opener. Don't mention their page or send any link here — "
                "that comes later, on request or with their first morning update."
            )
    if include_recent:
        recent = get_history(phone, limit=8)
        if recent:
            lines = "\n".join(
                f"{m['role']}: {m['content'][:250]}" for m in recent
            )
            system += f"\n\nRecent texts (for continuity — don't recite back):\n{lines}"
    suggestion = profile.get("pending_morning_suggestion")
    if suggestion:
        system += (
            f"\n\nYou've noticed this person keeps coming back to {suggestion} in conversation, "
            f"but it's not in their morning update. At a natural moment in this exchange — not as your opener — "
            f"mention it: something like 'you keep bringing up [X] — want me to add that to your morning?' "
            f"Use update_morning_briefing if they say yes. Don't force it if the moment isn't right."
        )
    notice = profile.get("pending_preference_notice")
    if notice:
        system += (
            f"\n\nThey've thumbs-downed {notice} enough times that you've stopped putting it "
            f"in their morning briefing. Mention it ONCE, at a natural moment in this exchange — "
            f"not as your opener, not as an announcement. Something like 'pulled the {notice} "
            f"stuff out of your mornings, you kept giving it the thumbs down.' Then let it go. "
            f"If they say they want it back, call update_morning_briefing. Never let a topic "
            f"disappear without them knowing why."
        )

    watches = get_user_watches(phone)
    if watches:
        watch_lines = "\n".join(
            f"- [{w['id']}] {w['description']} — checked every 30 min, alerts at most every {w['cooldown_hours']}h"
            for w in watches
        )
        system += (
            f"\n\nActive watches (background news checks you're running for them):\n{watch_lines}"
            f"\n\nIf they ask what you're tracking, list the descriptions naturally in your voice — not as a bulleted list. "
            f"Mention the alert frequency only if they ask how often."
        )
    price_watches = get_user_price_watches(phone)
    if price_watches:
        pw_lines = []
        for w in price_watches:
            bits = [f"[{w['id']}] {w['product_name']}"]
            if w.get("target_price") is not None:
                bits.append(f"target ${float(w['target_price']):.2f}")
            if w.get("baseline_price") is not None:
                bits.append(f"baseline ${float(w['baseline_price']):.2f}")
            if w.get("last_seen_price") is not None:
                bits.append(f"last seen ${float(w['last_seen_price']):.2f}")
            pw_lines.append("- " + " — ".join(bits))
        system += (
            "\n\nActive price watches (products you're checking every 12 hours for them):\n"
            + "\n".join(pw_lines)
            + "\n\nIf they ask what you're tracking, roll these in with any news watches above — natural prose, not a list."
        )
    return system


def _profile_and_consolidate(phone_number: str, user_msg: str, reply: str, shown_suggestion: str | None,
                             shown_notice: str | None = None):
    """Background: extract profile updates, clear any shown suggestion, consolidate history."""
    _update_profile(phone_number, user_msg, reply)
    # One shot, same as the suggestion below: Palmer has now had his chance to
    # mention the dropped topic. Clear it so he doesn't bring it up every turn.
    if shown_notice:
        upsert_profile(phone_number, {"pending_preference_notice": None})
    # One shot: clear the suggestion Palmer just had a chance to raise. Also reset
    # the topic count so we don't immediately re-trigger. If user said yes the
    # morning_topics already updated via update_morning_briefing; if no, they had a chance.
    if shown_suggestion:
        post_profile = get_profile(phone_number)
        cleaned_topics = [
            t for t in (post_profile.get("conversation_topics") or [])
            if shown_suggestion.lower() not in t and t not in shown_suggestion.lower()
        ]
        upsert_profile(phone_number, {
            "pending_morning_suggestion": None,
            "conversation_topics": cleaned_topics,
        })
    _consolidate_history(phone_number)


def save_assistant_turn(phone_number: str, user_msg: str, reply: str):
    """Persist the assistant reply and kick off profile updates in the background."""
    save_message(phone_number, "assistant", reply)
    # Capture suggestion before the background thread runs (it reads the pre-update profile)
    pre_profile = get_profile(phone_number)
    shown_suggestion = pre_profile.get("pending_morning_suggestion")
    shown_notice = pre_profile.get("pending_preference_notice")
    threading.Thread(
        target=_profile_and_consolidate,
        args=(phone_number, user_msg, reply, shown_suggestion, shown_notice),
        daemon=True,
    ).start()


def _resolve_asset(asset: str) -> str:
    """Turn whatever the model passed to get_price into something yfinance
    understands.

    It passes company names — "SpaceX", "Nvidia" — and yfinance 404s on those.
    Worse than the failed lookup is what the model concluded from it: that the
    company must be private. Resolution goes through tickers.py so the tool and
    the page's Markets section agree on what a name means, with the verified
    Haiku pass as the fallback for names the map doesn't carry."""
    from tickers import resolve_asset_name, resolve_company_ticker
    if not asset:
        return asset
    return resolve_asset_name(asset) or resolve_company_ticker(asset) or asset


def _normalize_price_topic(topic: str) -> str:
    """Append the ticker to a price topic that doesn't already resolve to one.

    The Markets section on Palmer Home is derived from these topic strings, so
    "add Nvidia to my site" has to end up as something tickers.py can resolve.
    It used to work only when the drafting model spontaneously wrote the symbol
    into the topic — which it often did, and sometimes didn't, and the failure
    was silent: the topic showed under "Watching" with no price.

    This is the one place the resolution can cost a model call, because topics
    are added rarely and read on every page view. Returns the topic unchanged
    when there is nothing tradeable in it — SpaceX is private, "AI news" is a
    subject, and neither should grow a fake ticker."""
    from tickers import resolve_topic_asset, resolve_company_ticker, looks_like_price_topic
    if not topic or not looks_like_price_topic(topic):
        return topic
    if resolve_topic_asset(topic):
        return topic
    symbol = resolve_company_ticker(topic)
    return f"{topic} ({symbol})" if symbol else topic


# Weather topics name a place, and that place is the one every weather pull uses.
_WEATHER_TOPIC_WORDS = ("weather", "forecast", "temperature")


def _city_from_weather_topic(topic: str) -> str | None:
    """The city a weather topic names, when it names one.

    `profile["city"]` is not just where the user lives — it is the only input to
    every weather pull Palmer makes (`home._fetch_weather`, the morning line,
    the text briefing). A user who says "make my morning weather Culver City"
    is setting that location, but they say it to update_morning_briefing, which
    only ever wrote the topic string. The city field kept its old, broader value
    and the page went on fetching the old city's forecast — which is how a user
    in Culver City got three consecutive mornings of Los Angeles temperatures
    with Culver City's name on them.

    EXTRACT_PROMPT cannot cover this: its LOCATION PRECISION rule deliberately
    only writes city from a statement of residence or an explicit correction,
    and "I want weather for X" is neither. So the write has to happen here.

    Costs a model call, on the same terms as _normalize_price_topic: topics are
    added rarely and read on every page view, so this runs on save and never on
    the read path."""
    if not topic or not any(w in topic.lower() for w in _WEATHER_TOPIC_WORDS):
        return None
    # Local import: morning imports _build_system from here at module level.
    from morning import _infer_city_from_topics
    return _infer_city_from_topics([topic])


def _apply_opening_kinds(profile: dict, tool_input: dict, updates: dict) -> str:
    """Fold opening_add / opening_remove into morning_prefs["opening_kinds"].

    Set arithmetic on the stored list rather than asking the model to restate
    the whole set — "I want movies too" is additive, and a model that has to
    re-derive the full set from a profile dump will eventually drop one of the
    kinds the user never mentioned.

    Absent means all three, so the list is only written once a user actually
    trims or restores something. Returns a short note for the tool result so
    Palmer can confirm in its own words."""
    from opening import ALL_KINDS, KIND_WORDS
    add = [KIND_WORDS.get(str(w).lower()) for w in (tool_input.get("opening_add") or [])]
    drop = [KIND_WORDS.get(str(w).lower()) for w in (tool_input.get("opening_remove") or [])]
    add = [k for k in add if k]
    drop = [k for k in drop if k]
    if not add and not drop:
        return ""

    prefs = dict(profile.get("morning_prefs") or {})
    current = prefs.get("opening_kinds")
    current = list(current) if isinstance(current, list) else list(ALL_KINDS)
    kinds = [k for k in ALL_KINDS if (k in current or k in add) and k not in drop]

    prefs["opening_kinds"] = kinds
    # Removing every kind is how a user switches the section off in their own
    # words ("none of that stuff"), so honour it as the off switch rather than
    # storing an empty list the reader has to interpret separately.
    prefs["opening"] = bool(kinds)
    updates["morning_prefs"] = prefs

    words = {"local": "new places", "event": "concerts and events", "screen": "movies and shows"}
    if not kinds:
        return " Opening section is off now."
    return " Opening now covers: " + ", ".join(words[k] for k in kinds) + "."


def _redraft_without_redirect(system: str, messages: list, draft: str) -> str | None:
    """One retry for a reply that hands the user off to a competing product.

    No tools on the retry: the draft is already past the tool loop, and the only
    thing wrong with it is what it says. Returns None on any failure, which
    leaves the caller holding the original."""
    from guards import REDIRECT_CORRECTION
    try:
        resp = client.messages.create(
            model=SONNET_MODEL, max_tokens=600, system=system,
            messages=messages + [
                {"role": "assistant", "content": draft},
                {"role": "user", "content": REDIRECT_CORRECTION.format(draft=draft)},
            ],
        )
        retry = next((b.text for b in resp.content if hasattr(b, "text")), None)
        return _sms_clean(retry) if retry else None
    except Exception as e:
        print(f"redirect redraft failed: {type(e).__name__}: {e}")
        return None


def _finalize(text: str, system: str, messages: list, gif_url):
    """Clean the draft, and enforce in code the one rule the prompt could not.

    SYSTEM_PROMPT has always forbidden sending users to competing products, and
    Palmer did it anyway in production — once while quoting the rule back
    ("I'd point you to Google Flights but I know that's not helpful coming from
    me"). Same remedy as morning._NAMES_THE_LINK: check the draft, redraft once."""
    from guards import redirects_elsewhere
    reply = _sms_clean(text)
    if not redirects_elsewhere(reply):
        return reply, gif_url
    print(f"reply handed off to a competitor, redrafting once: {reply[:90]!r}")
    retry = _redraft_without_redirect(system, messages, reply)
    if retry and not redirects_elsewhere(retry):
        return retry, gif_url
    # Twice is rare enough to be worth seeing in the logs rather than papering
    # over with a canned line that would cost Palmer's voice on every occurrence.
    print("REDIRECT GUARD: redraft still handed off; shipping the original")
    return reply, gif_url


def get_reply(phone_number: str, message: str, media_url: str = None, history: list[dict] | None = None, is_new_user: bool = False) -> tuple[str, str | None]:
    """Generate a reply. Returns (text, gif_url) — gif_url is None if no GIF was queued."""
    messages = history if history is not None else get_history(phone_number, limit=HISTORY_LIMIT)
    system = _build_system(phone_number, is_new_user=is_new_user)

    # Build user content — include image if MMS photo was attached
    if media_url:
        media = _fetch_media(media_url)
        if media:
            data, content_type = media
            user_content = [{"type": "image", "source": {"type": "base64", "media_type": content_type, "data": data}}]
            if message:
                user_content.append({"type": "text", "text": message})
        else:
            user_content = message or "(sent a photo)"
    else:
        user_content = message
    messages.append({"role": "user", "content": user_content})

    gif_url = None
    # Pull the user's tz once — weather 'tomorrow'/weekday resolution needs
    # user-local today, not server UTC. Missing tz falls through as None and
    # the weather helpers degrade to server UTC (same as before this change).
    user_tz = get_profile(phone_number).get("timezone")

    for _ in range(6):  # cap tool call iterations
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=600,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        # Extract any text block present in this response
        text = next((b.text for b in response.content if hasattr(b, "text")), None)

        if response.stop_reason in ("end_turn", "max_tokens"):
            if text:
                return _finalize(text, system, messages, gif_url)
            # end_turn with no text — unlikely but guard anyway
            raise RuntimeError(f"stop_reason={response.stop_reason} but no text block in response")

        tool_results = []
        for b in response.content:
            if b.type != "tool_use":
                continue
            if b.name == "web_search":
                result = _search(b.input["query"])
            elif b.name == "get_weather":
                result = _get_weather(b.input["location"], b.input.get("when", "today"), tz=user_tz)
            elif b.name == "get_price":
                result = _get_price(_resolve_asset(b.input["asset"]))
            elif b.name == "send_gif":
                gif_url = _get_gif(b.input["query"])
                result = f"GIF queued: {gif_url}" if gif_url else "No GIF found for that query."
            elif b.name == "set_reminder":
                recurrence = b.input.get("recurrence")
                save_reminder(phone_number, b.input["text"], b.input["due_at"], recurrence)
                if recurrence:
                    result = (f"Recurring reminder saved ({recurrence}), first send "
                              f"{b.input['due_at']}. It repeats at the same local time "
                              f"until they cancel it.")
                else:
                    result = f"Reminder saved for {b.input['due_at']}."
            elif b.name == "update_morning_briefing":
                profile = get_profile(phone_number)
                topics = list(profile.get("morning_topics") or [])
                new_city = None
                overlaps = []
                for item in (b.input.get("add") or []):
                    item = _normalize_price_topic(item)
                    if new_city is None:
                        new_city = _city_from_weather_topic(item)
                    if not any(item.lower() in t.lower() or t.lower() in item.lower() for t in topics):
                        # Substring containment cannot see that "Kirkwood, MO
                        # news" and "St. Louis area news" are the same beat. Add
                        # the topic regardless and let Palmer raise it — a
                        # semantic check has false positives ("NFL headlines"
                        # reads as a duplicate of "Philadelphia Eagles news" and
                        # is not), and silently dropping what someone asked for
                        # is a worse failure than one extra question.
                        dup = topic_already_covered(item, topics)
                        if dup:
                            overlaps.append((item, dup))
                        topics.append(item)
                for item in (b.input.get("remove") or []):
                    topics = [t for t in topics if item.lower() not in t.lower()]
                # Turning the morning on with nothing in the list used to mean
                # weather and nothing else — an empty News card and no Markets
                # on day one. Seed a baseline instead of waiting to be asked.
                if b.input.get("enabled") is True and not topics:
                    from morning import default_topics
                    topics = default_topics(profile.get("city"))
                updates: dict = {"morning_topics": topics, "morning_onboarded": True}
                # Asking for a city's weather sets the city every weather pull
                # uses. Timezone is deliberately left alone — it is only derived
                # when absent, so this moves the forecast without moving the
                # hour their morning arrives.
                if new_city and new_city != profile.get("city"):
                    updates["city"] = new_city
                    print(f"city set from weather topic for {phone_number}: "
                          f"{profile.get('city')!r} -> {new_city!r}")
                if "enabled" in b.input:
                    updates["morning_enabled"] = b.input["enabled"]
                opening_note = _apply_opening_kinds(profile, b.input, updates)
                if "episode_alerts" in b.input:
                    prefs = dict(updates.get("morning_prefs")
                                 or profile.get("morning_prefs") or {})
                    prefs["episode_alerts"] = bool(b.input["episode_alerts"])
                    updates["morning_prefs"] = prefs
                    opening_note += (" New episodes will now be mentioned in their morning text."
                                     if prefs["episode_alerts"]
                                     else " New episodes will stay on their page only.")
                upsert_profile(phone_number, updates)
                # The page caches prices for 5 minutes. Without expiring that
                # stamp, a ticker the user just added does not appear until the
                # cooldown lapses, which reads as "it didn't work".
                try:
                    from home import invalidate
                    # weather too when the city moved: the page caches it for 10
                    # minutes, and a stale stamp would serve the old city's
                    # forecast right after the user corrected it.
                    sections = ["prices"]
                    if updates.get("city"):
                        sections += ["weather", "opening"]
                    elif "morning_prefs" in updates:
                        # The kinds changed, so the cached rows are the wrong
                        # shape — without this the section keeps showing the
                        # concerts they just asked to stop seeing for up to a
                        # day, which reads as Palmer ignoring them.
                        sections.append("opening")
                    invalidate(phone_number, tuple(sections))
                except Exception as e:
                    print(f"home.invalidate after briefing update failed: {e}")
                topic_str = ", ".join(topics) if topics else "none"
                enabled = updates.get("morning_enabled")
                if enabled is False:
                    result = f"Morning briefing paused. Topics saved: {topic_str}. Say 'resume my morning' to turn it back on."
                elif enabled is True:
                    result = f"Morning briefing resumed. Topics: {topic_str}."
                else:
                    result = f"Morning briefing updated. Topics: {topic_str}."
                result += opening_note
                for added, dup in overlaps:
                    result += (f" Note: {added!r} may cover the same ground as {dup!r}, "
                               f"which they already track. Both are on the list now — "
                               f"mention the overlap in one line and ask if they want to "
                               f"drop one. Do not remove anything yourself.")
            elif b.name == "set_morning_time":
                normalized = _normalize_hhmm(b.input.get("time", ""))
                if normalized:
                    upsert_profile(phone_number, {"morning_time": normalized})
                    result = f"Morning briefing time set to {normalized} local."
                else:
                    result = f"Invalid time {b.input.get('time')!r} — must be 24-hour HH:MM, e.g. 07:00."
            elif b.name == "cancel_reminders":
                count = cancel_reminders(phone_number, b.input.get("text_match"))
                result = f"Cancelled {count} reminder(s)."
            elif b.name == "add_watch":
                watch_id = save_watch(phone_number, b.input["description"], b.input["queries"], b.input.get("cooldown_hours", 4))
                result = f"Watch set (id={watch_id}). I'll check every 30 minutes and only text if something major breaks."
            elif b.name == "cancel_watch":
                count = cancel_watches(phone_number, b.input.get("text_match"))
                result = f"Cancelled {count} watch(es)."
            elif b.name == "search_shopping":
                from shopping import search_shopping
                result = search_shopping(
                    b.input["query"],
                    b.input.get("max_price"),
                    b.input.get("min_price"),
                    include_link=b.input.get("include_link", False),
                )
            elif b.name == "browse_shop":
                from shopping import browse_shop
                result = browse_shop(b.input["query"])
            elif b.name == "search_flights":
                from flights import search_flights
                result = search_flights(
                    b.input["origin"],
                    b.input["destination"],
                    b.input["outbound_date"],
                    b.input.get("return_date"),
                )
            elif b.name == "follow_team":
                from sports import find_teams, FOLLOW_MAX as TEAM_MAX
                profile = get_profile(phone_number)
                current = list(profile.get("followed_teams") or [])
                asked = (b.input.get("name") or "").strip()
                matches = find_teams(asked)
                if not matches:
                    result = (f"No team matches {asked!r}. Ask them to confirm the team — "
                              f"do not guess one, and do not send them elsewhere to look it up.")
                elif len(matches) > 1:
                    # "Cardinals" is two teams in two sports. Guessing signs them
                    # up for alerts about the wrong one, in the wrong season.
                    listed = ", ".join(f"{m['name']} ({m['league'].upper()})" for m in matches)
                    result = (f"{asked!r} matches more than one team: {listed}. Ask which they "
                              f"mean in one short line, then call follow_team again with the "
                              f"fuller name. Do NOT pick one yourself.")
                elif any(t.get("abbrev") == matches[0]["abbrev"]
                         and t.get("league") == matches[0]["league"] for t in current):
                    result = f"They already follow {matches[0]['name']}."
                elif len(current) >= TEAM_MAX:
                    result = (f"They already follow {TEAM_MAX} teams, which is the limit. "
                              f"Tell them and offer to drop one.")
                else:
                    current.append(matches[0])
                    upsert_profile(phone_number, {"followed_teams": current})
                    result = (f"Now following {matches[0]['name']}. They get a text when the lead "
                              f"changes, when someone scores in the last five minutes, and at the "
                              f"final — a few a game, not every play. Say that plainly.")
            elif b.name == "unfollow_team":
                profile = get_profile(phone_number)
                current = list(profile.get("followed_teams") or [])
                # Accept `name` as well as `text_match`. Observed live: asked to
                # "stop the eagles score texts" the model passed name=Eagles,
                # carrying the key over from follow_team, and a dispatch reading
                # only text_match would have silently unfollowed nothing while
                # telling them it had.
                match = (b.input.get("text_match") or b.input.get("name") or "").strip().lower()
                kept = [t for t in current
                        if match and match not in (t.get("name") or "").lower()]
                dropped = len(current) - len(kept)
                if dropped:
                    upsert_profile(phone_number, {"followed_teams": kept})
                result = (f"Stopped score alerts for {dropped} team(s)." if dropped
                          else "No followed team matched that.")
            elif b.name == "get_score":
                from sports import find_teams, team_game, describe
                matches = find_teams(b.input.get("team", ""))
                if not matches:
                    result = (f"No team matches {b.input.get('team')!r}. Ask them to confirm it.")
                elif len(matches) > 1:
                    listed = ", ".join(f"{m['name']} ({m['league'].upper()})" for m in matches)
                    result = f"That matches {listed} — ask which they mean."
                else:
                    game = team_game(matches[0])
                    result = (f"{matches[0]['name']}: {describe(game)}" if game
                              else f"{matches[0]['name']} have no game today.")
            elif b.name == "follow_show":
                from shows import resolve_show, FOLLOW_MAX
                profile = get_profile(phone_number)
                current = list(profile.get("shows") or [])
                asked = (b.input.get("name") or "").strip()
                # Resolve on the WRITE path, once — never on read, which runs on
                # every page view. Same terms as _normalize_price_topic.
                found = resolve_show(asked)
                if not found:
                    result = (f"No series matches {asked!r}. Ask them to confirm the title — "
                              f"do not guess one, and do not send them elsewhere to look it up.")
                elif any(sh.get("id") == found["id"] for sh in current):
                    result = f"They already follow {found['name']}."
                elif len(current) >= FOLLOW_MAX:
                    result = (f"They already follow {FOLLOW_MAX} shows, which is the limit. "
                              f"Tell them and offer to drop one.")
                else:
                    current.append({"id": found["id"], "name": found["name"]})
                    upsert_profile(phone_number, {"shows": current})
                    try:
                        from home import invalidate
                        invalidate(phone_number, ("opening",))
                    except Exception as e:
                        print(f"home.invalidate after follow_show failed: {e}")
                    result = (f"Now following {found['name']}. It shows up on their page in the "
                              f"week an episode lands and stays quiet between seasons. It is NOT "
                              f"in their morning text unless they ask for that separately.")
            elif b.name == "unfollow_show":
                profile = get_profile(phone_number)
                current = list(profile.get("shows") or [])
                # `name` too — the follow tool uses that key and the model
                # carries it over. See the note in unfollow_team.
                match = (b.input.get("text_match") or b.input.get("name") or "").strip().lower()
                kept = [sh for sh in current
                        if match and match not in (sh.get("name") or "").lower()]
                dropped = len(current) - len(kept)
                if dropped:
                    upsert_profile(phone_number, {"shows": kept})
                    try:
                        from home import invalidate
                        invalidate(phone_number, ("opening",))
                    except Exception as e:
                        print(f"home.invalidate after unfollow_show failed: {e}")
                result = (f"Unfollowed {dropped} show(s)." if dropped
                          else "No followed show matched that.")
            elif b.name == "add_flight_watch":
                from db import save_flight_watch, FLIGHT_WATCH_MAX
                ok = save_flight_watch(
                    phone_number, b.input["origin"], b.input["destination"],
                    b.input["outbound_date"], b.input.get("return_date"),
                    b.input.get("target_price"))
                route = f"{b.input['origin'].upper()} → {b.input['destination'].upper()}"
                if ok:
                    tgt = b.input.get("target_price")
                    result = (f"Now watching {route} {b.input['outbound_date']}"
                              + (f" targeting ${tgt:,.0f}." if tgt else ".")
                              + " Checked daily; they'll hear on a target hit or a move over $40.")
                else:
                    result = (f"Not added — they either already watch {route} on that date, or "
                              f"they are at the limit of {FLIGHT_WATCH_MAX} flight watches. "
                              f"Tell them which, and offer to cancel one.")
            elif b.name == "cancel_flight_watch":
                from db import cancel_flight_watches
                n = cancel_flight_watches(phone_number, b.input.get("text_match"))
                result = (f"Cancelled {n} flight watch(es)." if n
                          else "No matching flight watch to cancel.")
            elif b.name == "search_hotels":
                from hotels import search_hotels
                result = search_hotels(
                    b.input["location"],
                    b.input["check_in_date"],
                    b.input["check_out_date"],
                    b.input.get("max_price"),
                    b.input.get("min_rating"),
                )
            elif b.name == "add_price_watch":
                import shopping
                product_name = b.input["product_name"]
                watch_id = save_price_watch(
                    phone_number,
                    product_name,
                    b.input.get("target_price"),
                    b.input.get("currency", "USD"),
                )
                target = b.input.get("target_price")
                target_str = f" at or under ${float(target):.2f}" if target is not None else " for meaningful drops"
                # Seed the baseline now, same as the Amazon path — otherwise a watch
                # whose first scheduler match ever fails silently never gets a
                # reference price and can never alert (see run_price_watches).
                current = shopping.check_price(product_name)
                if current:
                    set_price_watch_baseline(watch_id, current["price"], current["url"], current["merchant"])
                    result = (
                        f"Price watch set (id={watch_id}) for {product_name}. "
                        f"Currently ${current['price']:.2f} at {current['merchant'] or 'a store I found'}. "
                        f"I'll check every 12 hours and text if it hits{target_str}."
                    )
                else:
                    result = (
                        f"Price watch set (id={watch_id}) for {product_name}, but I couldn't pin down a "
                        f"confident match on Google Shopping just now — tell the user that if it's a common "
                        f"item, a more specific name (brand + size/flavor) helps. I'll keep trying every "
                        f"12 hours and text if it hits{target_str}."
                    )
            elif b.name == "add_amazon_watch":
                import amazon
                query = b.input["product_query"]
                target = b.input.get("target_price")
                resolved = amazon.resolve_asin(query)
                if not resolved:
                    result = f"Couldn't find {query!r} on Amazon — ask the user for a more specific product name (brand + variant/size)."
                else:
                    watch_id = save_price_watch(
                        phone_number,
                        resolved["title"],
                        target_price=target,
                        source="amazon",
                        asin=resolved["asin"],
                    )
                    # Seed the baseline from the resolve step so we don't waste
                    # the first tick recording a baseline that's already stale.
                    set_price_watch_baseline(watch_id, resolved["price"], resolved["url"], "Amazon")
                    target_str = f" at or under ${float(target):.2f}" if target is not None else " for a meaningful drop"
                    result = (
                        f"Amazon watch set (id={watch_id}) for {resolved['title']}. "
                        f"Currently ${resolved['price']:.2f}. I'll check every 12 hours and text if it hits{target_str}. "
                        f"Link: {resolved['url']}"
                    )
            elif b.name == "cancel_price_watch":
                count = cancel_price_watches(phone_number, b.input.get("text_match"))
                result = f"Cancelled {count} price watch(es)."
            elif b.name == "list_watches":
                news = get_user_watches(phone_number)
                prices = get_user_price_watches(phone_number)
                if not news and not prices:
                    result = "No active watches for this user."
                else:
                    lines = []
                    for w in news:
                        lines.append(
                            f"news [{w['id']}] {w['description']} "
                            f"(checked every 30 min, at most every {w['cooldown_hours']}h)"
                        )
                    for w in prices:
                        bits = [f"price [{w['id']}] {w['product_name']}"]
                        if w.get("target_price") is not None:
                            bits.append(f"target ${float(w['target_price']):.2f}")
                        if w.get("baseline_price") is not None:
                            bits.append(f"baseline ${float(w['baseline_price']):.2f}")
                        if w.get("last_seen_price") is not None:
                            bits.append(f"last seen ${float(w['last_seen_price']):.2f}")
                        lines.append(" — ".join(bits))
                    result = "\n".join(lines)
            elif b.name == "get_my_page":
                from home import ensure_fresh
                url = ensure_fresh(phone_number)
                if url.startswith("http"):
                    result = (
                        f"{url}\n\n"
                        "That is the user's live page and it is already up to date. "
                        "Put this URL at the very end of your reply, exactly as "
                        "written, with no text or punctuation after it."
                    )
                else:
                    result = ("No page URL is available for this user right now "
                              "(APP_URL is not configured). Answer their question "
                              "directly instead and do not mention a page.")
            elif b.name == "get_travel_time":
                from traffic import get_travel_time
                result = get_travel_time(b.input["origin"], b.input["destination"])
            elif b.name == "get_city_traffic":
                from traffic import get_city_traffic
                line = get_city_traffic(b.input["city"])
                result = line if line else f"No live traffic data available for {b.input['city']!r} right now."
            else:
                result = "Unknown tool."
            tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": result})

        if not tool_results:
            # stop_reason was tool_use but no tool blocks found — something is off; return text if any
            if text:
                return _finalize(text, system, messages, gif_url)
            raise RuntimeError("stop_reason=tool_use but no tool_use blocks and no text")

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError("tool loop exceeded max iterations without end_turn")
