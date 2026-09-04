from dotenv import load_dotenv
load_dotenv()

import os
import threading
from collections import defaultdict
from fastapi import FastAPI, Form, Response, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response as FileResponse
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from apscheduler.schedulers.background import BackgroundScheduler
from agent import get_reply, save_assistant_turn
from smstext import shorten_message
from morning import generate_morning, send_morning_messages, send_missing_data_asks
from evening import send_evening_messages
from db import get_profile, upsert_profile, save_message, get_history, HISTORY_LIMIT
from send_reminders import send_due_reminders
from watches import run_watches
from shopping import run_price_watches
from sms_util import ensure_sms, send_sms, FALLBACK_SMS
from tapback import parse_reaction, record_reaction, interpret_reaction, learn_from_reactions

app = FastAPI()

# Importing main starts the job loop, and send_due_reminders sends real SMS on
# a 1-minute interval. Set PALMER_NO_SCHEDULER=1 for anything that imports this
# module without wanting to be a live dyno — tests, shells, one-off scripts.
_SCHEDULER_ENABLED = os.environ.get("PALMER_NO_SCHEDULER") != "1"

_scheduler = BackgroundScheduler()

# INTERVAL vs CRON: an interval job's first run is scheduled at start + interval,
# and that clock restarts on every dyno boot — i.e. every deploy. The error is
# proportional to the period, so the short jobs below stay on "interval" (losing
# up to 30 minutes is immaterial) while everything at an hour or longer is on
# fixed clock times, so its cadence is a property of the clock rather than of
# the deploy history. run_price_watches (further down) is the case that made
# this concrete: at 12h it only ran on days production was left alone.
#
# misfire_grace_time throughout: APScheduler's default is 1 SECOND, so a tick
# delayed behind a slow job is dropped outright rather than run late.
#
# Every cron job pins timezone=Etc/UTC rather than inheriting it. A bare
# BackgroundScheduler() takes the PROCESS timezone, which is Etc/UTC on the
# dyno but whatever the developer's machine is set to locally — so an
# unpinned grid means local runs and tests silently disagree with prod, and
# a TZ config var would rotate the whole schedule with nothing looking wrong.
_scheduler.add_job(send_due_reminders, "interval", minutes=1)
# The two scheduled updates. Each user has a local target time for each, and a
# per-day guard on the profile makes extra ticks harmless. The evening one is
# a diff against the morning (see evening.py) and sends nothing on a day when
# nothing changed — the quiet is the point.
_scheduler.add_job(send_morning_messages, "interval", minutes=5)
_scheduler.add_job(send_evening_messages, "interval", minutes=5)
_scheduler.add_job(run_watches, "interval", minutes=30)

# There is deliberately no other unprompted sender on a clock here. Three used
# to be: a live score poller (every 2 min), a "friend would text this" daily
# news alert (hourly), and a check-in about something in the profile (every
# 2h). All three were removed together — what they carried now rides the two
# updates above, and anything a user actually asked to be told about goes
# through a watch. Do not add one back without a user-set trigger behind it.

# :30 so this does not share a second with the hourly reminder tick.
_scheduler.add_job(send_missing_data_asks, "cron", minute=30,
                   timezone="Etc/UTC", misfire_grace_time=1800)
# SerpAPI: 12h cadence keeps the starter plan (5000 searches/mo) comfortable
# while leaving headroom for Amazon watches (dual-source: Google Shopping +
# amazon_product engine both go through this same tick).
#
# CRON, NOT INTERVAL — and that distinction is load-bearing at this cadence.
# An interval job's first run is scheduled at start + interval, and the clock
# restarts on every dyno boot, which means every deploy. Ship twice in one
# afternoon and a 12h interval job never runs at all; this one only fired on
# days production was left alone for 12 straight hours. A quiet tick logs
# nothing, so it failed invisibly. Fixed UTC hours make the cadence a property
# of the clock instead of a property of the deploy history.
#
# TWICE DAILY, NOT EVERY 12 HOURS — the slots are 16h and 8h apart, not
# evenly split, and that is deliberate. The budget constraint above is runs
# per day, which two slots satisfy at any spacing; the hour is the part users
# feel, since these are unprompted texts. A strict 12h split puts the two
# slots at H and H+12, and across the two timezones on record there is no H
# where both land in waking hours — Chicago and LA are 2h apart, which
# squeezes the feasible window to nothing. Even spacing would have to be paid
# for with a 10pm text, so it isn't.
#
# 00:00 and 16:00 UTC are daytime in both zones, winter and summer: 16:00 is
# 11am CDT / 9am PDT (10am CST / 8am PST), 00:00 is 7pm CDT / 5pm PDT (6pm
# CST / 4pm PST). Timezone is pinned rather than inherited from the process —
# the dyno happens to be Etc/UTC today, but a TZ config var would silently
# rotate the whole schedule.
#
# misfire_grace_time: if a tick is delayed (a long earlier job holding the
# thread), run it late rather than dropping it. APScheduler's default of 1s
# would skip the slot entirely and wait for the next one.
_scheduler.add_job(
    run_price_watches, "cron", hour="0,16", timezone="Etc/UTC",
    misfire_grace_time=3600,
)

# Forecast accuracy audit — cron for the same reason as above: at once a day an
# interval job's phase is a function of deploy history, and a skipped day is a
# hole in the record this exists to build.
#
# 11:00 UTC is 06:00 CDT / 04:00 PDT — shortly before the 07:00 local morning
# sends, so the forecast logged is the one users are about to be told, and it is
# the same calendar date in both zones, which is what keeps target_date honest.
# Sends nothing and touches no user path; a failure is a gap in the log.
from wxaudit import run_forecast_audit
_scheduler.add_job(
    run_forecast_audit, "cron", hour="11", timezone="Etc/UTC",
    misfire_grace_time=3600,
)

# Flight watches: once a day, and cron for the same reason as the jobs above.
# Once daily is the cost control, not a preference — SerpAPI is the only paid
# input and the account is on 250 searches/month, so each active watch costs
# ~30 and db.FLIGHT_WATCH_MAX caps a user at three. 13:00 UTC is mid-morning
# US-wide, late enough that a fare alert does not arrive before the briefing.
from flightwatch import run_flight_watches
_scheduler.add_job(
    run_flight_watches, "cron", hour="13", timezone="Etc/UTC",
    misfire_grace_time=3600,
)

if _SCHEDULER_ENABLED:
    _scheduler.start()
else:
    print("PALMER_NO_SCHEDULER=1 — background jobs not started")

_in_flight: dict[str, set] = defaultdict(set)
_in_flight_lock = threading.Lock()
_phone_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

_seen_sids: list[str] = []
_seen_sids_lock = threading.Lock()
_SEEN_SIDS_MAX = 200

_seen_status_sids: list[str] = []
_seen_status_sids_lock = threading.Lock()


def _send_gif_outbound(to: str, media_url: str):
    if not send_sms(to, "", media_url=media_url, add_status_callback=False):
        print(f"GIF send failed for {to}")


def _handle_sms(from_number: str, body: str, media_url: str | None):
    reply_sent = False
    try:
        reply_sent = _handle_sms_inner(from_number, body, media_url)
    except Exception as e:
        print(f"UNHANDLED ERROR in _handle_sms for {from_number}: {e}")
    if not reply_sent:
        ensure_sms(from_number, FALLBACK_SMS)


def _handle_sms_inner(from_number: str, body: str, media_url: str | None) -> bool:
    # Reactions are almost always conversation-closers. Interpret first, because
    # the one exception matters: if Palmer asked a question and they answered it
    # with a thumbs up, that IS a reply and swallowing it strands the thread.
    if media_url is None:
        reaction = parse_reaction(body)
        if reaction:
            last_assistant = ""
            for m in reversed(get_history(from_number, limit=4)):
                if m["role"] == "assistant":
                    last_assistant = m["content"]
                    break
            profile = get_profile(from_number)
            verdict = interpret_reaction(reaction, last_assistant, profile)
            profile = record_reaction(from_number, reaction, verdict, profile)
            learn_from_reactions(from_number, profile)

            if not verdict.get("needs_reply"):
                # Returning True is load-bearing: _handle_sms fires FALLBACK_SMS
                # on a falsy return, which would defeat the whole point.
                print(f"Reaction from {from_number} ({reaction['kind']}/"
                      f"{verdict['function']}/{verdict['sentiment']}) — no reply sent")
                return True

            # It answered something Palmer asked. Hand it to the normal path as an
            # explicit turn so he responds to the yes/no, not to the emoji.
            label = reaction.get("emoji") or reaction.get("kind")
            quoted = reaction.get("quoted") or last_assistant[:120]
            body = f'[they reacted {label} to: "{quoted}" — this is their answer]'
            print(f"Reaction from {from_number} answers a pending question — replying")

    profile_before = get_profile(from_number)
    is_new_user = not profile_before.get("intro_sent")

    token = object()
    with _in_flight_lock:
        _in_flight[from_number].add(token)

    reply_sent = False
    try:
        with _phone_locks[from_number]:  # serialize per phone so history never interleaves
            history = get_history(from_number, limit=HISTORY_LIMIT)
            save_message(from_number, "user", body or "[photo]")

            reply = None
            gif_url = None
            try:
                reply, gif_url = get_reply(
                    phone_number=from_number, message=body, media_url=media_url,
                    history=history, is_new_user=is_new_user,
                )
            except Exception as e:
                print(f"get_reply failed for {from_number}: {e}")

            if not reply or not reply.strip():
                if reply is not None:
                    print(f"get_reply returned empty string for {from_number}")
                reply_sent = ensure_sms(from_number, FALLBACK_SMS)
            else:
                with _in_flight_lock:
                    add_quote = len(_in_flight[from_number]) > 1

                if add_quote:
                    snippet = body if len(body) <= 50 else body[:50].rstrip() + "…"
                    reply = f"> {snippet}\n{reply}"

                reply_sent = ensure_sms(from_number, reply)
                if reply_sent:
                    if gif_url:
                        _send_gif_outbound(from_number, gif_url)
                    try:
                        save_assistant_turn(from_number, body, reply)
                    except Exception as e:
                        print(f"save_assistant_turn failed for {from_number}: {e}")
                    print(f"Replied to {from_number}: {reply[:100]}")
    finally:
        with _in_flight_lock:
            _in_flight[from_number].discard(token)

    if is_new_user and reply_sent:
        # Mark only after a successful reply so a failed first send retries the intro flow.
        upsert_profile(from_number, {"intro_sent": True})

    return reply_sent


@app.post("/sms")
async def sms_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(default=""),
    NumMedia: int = Form(default=0),
    MediaUrl0: str = Form(default=None),
    MessageSid: str = Form(default=""),
):
    validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
    form_data = await request.form()
    url = str(request.url)
    if request.headers.get("x-forwarded-proto") == "https":
        url = url.replace("http://", "https://", 1)
    if not validator.validate(url, dict(form_data), request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(status_code=403)

    if MessageSid:
        with _seen_sids_lock:
            if MessageSid in _seen_sids:
                print(f"Duplicate MessageSid {MessageSid} — dropping Twilio retry")
                return Response(content=str(MessagingResponse()), media_type="application/xml")
            _seen_sids.append(MessageSid)
            if len(_seen_sids) > _SEEN_SIDS_MAX:
                del _seen_sids[0]

    body = Body.strip()
    media_url = MediaUrl0 if NumMedia > 0 else None

    background_tasks.add_task(_handle_sms, From, body, media_url)

    return Response(content=str(MessagingResponse()), media_type="application/xml")


_RETRIABLE_ERRORS = {"30019", "21617"}  # content size errors fixable by shortening


def _retry_shortened_send(to: str, message_sid: str):
    print(f"Delivery failed for {to} (SID={message_sid}) — shortening and retrying")
    try:
        from twilio.rest import Client as TwilioClient
        original = TwilioClient(
            os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
        ).messages(message_sid).fetch()
        ensure_sms(to, shorten_message(original.body), add_status_callback=False)
        print(f"Status-callback retry sent to {to}")
    except Exception as e:
        print(f"Status-callback retry failed for {to}: {e}")


@app.post("/sms-status")
async def sms_status_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    MessageSid: str = Form(...),
    MessageStatus: str = Form(...),
    ErrorCode: str = Form(default=""),
    To: str = Form(...),
):
    validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
    form_data = await request.form()
    url = str(request.url)
    if request.headers.get("x-forwarded-proto") == "https":
        url = url.replace("http://", "https://", 1)
    if not validator.validate(url, dict(form_data), request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(status_code=403)

    if MessageStatus in ("failed", "undelivered") and ErrorCode in _RETRIABLE_ERRORS:
        with _seen_status_sids_lock:
            if MessageSid in _seen_status_sids:
                print(f"Duplicate status retry for MessageSid {MessageSid} — dropping")
                return Response(status_code=204)
            _seen_status_sids.append(MessageSid)
            if len(_seen_status_sids) > _SEEN_SIDS_MAX:
                del _seen_status_sids[0]
        background_tasks.add_task(_retry_shortened_send, To, MessageSid)

    return Response(status_code=204)


@app.get("/c/{token}.png")
async def artifact_png(token: str):
    """The briefing as a flat card, for MMS and og:image.

    Public by necessity — Twilio fetches MMS media and the recipient's phone
    fetches the og:image, neither of which can carry auth. The token is the
    protection; see artifacts.py."""
    from artifacts import load, render_png
    payload = load(token)
    if payload is None:
        raise HTTPException(status_code=404)
    return FileResponse(
        content=render_png(token, payload),
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-Robots-Tag": "noindex, nofollow",
            "Referrer-Policy": "no-referrer",
        },
    )


@app.get("/c/{token}")
async def artifact_page(token: str):
    """The interactive briefing. An MMS card is a bitmap with no tap targets,
    so this is where headlines and tickers become links."""
    from artifacts import load, image_url, page_url
    from page import render
    payload = load(token)
    if payload is None:
        raise HTTPException(status_code=404)
    return FileResponse(
        content=render(payload, token=token,
                       image_url=image_url(token), page_url=page_url(token)),
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=300",
            "X-Robots-Tag": "noindex, nofollow",
            "Referrer-Policy": "no-referrer",
        },
    )


@app.api_route("/h/{token}.png", methods=["GET", "HEAD"])
async def home_png(token: str):
    """The user's home as a flat card — og:image and MMS.

    Answers HEAD as well as GET: link-preview scrapers and proxies commonly
    probe with HEAD before fetching an image, and a 405 there can cost the
    preview outright."""
    from home import load, refresh_stale
    from artifacts import render_png
    payload = load(token)
    if payload is None:
        raise HTTPException(status_code=404)
    payload = refresh_stale(token, payload)
    from artifacts import _card_fingerprint
    stamp = _card_fingerprint(payload)
    return FileResponse(
        content=render_png(token, payload),
        media_type="image/png",
        # An ETag so a cache that DOES revalidate gets a straight answer, and a
        # short max-age so one that does not still comes back soon. The real
        # busting is the ?v= fingerprint on the og:image URL; these two are the
        # belt to that pair of braces.
        headers={"Cache-Control": "public, max-age=300",
                 "ETag": f'"{stamp}"',
                 "X-Robots-Tag": "noindex, nofollow",
                 "Referrer-Policy": "no-referrer"},
    )


@app.api_route("/h/{token}", methods=["GET", "HEAD"])
async def home_page(token: str):
    """The user's live page. No login — the token is the whole protection, so
    this shows only briefing-grade content (see home.py)."""
    from home import load, refresh_stale
    from page import render
    payload = load(token)
    if payload is None:
        raise HTTPException(status_code=404)
    payload = refresh_stale(token, payload)
    base = os.environ.get("APP_URL", "").rstrip("/")
    # The og:image URL carries the card's content fingerprint. Link-preview
    # scrapers — iMessage most stubbornly — cache og:images by URL and have no
    # reason to refetch a URL they have already seen, so a fixed
    # /h/{token}.png meant every morning's message showed whatever card was
    # scraped the very first time. The server was rendering today's card
    # faithfully; nobody was ever asking for it. A fingerprint in the URL makes
    # each day's link a different image to a cache, and an unchanged day stays
    # cheap because the fingerprint is unchanged too.
    from artifacts import _card_fingerprint
    stamp = _card_fingerprint(payload)
    return FileResponse(
        content=render(payload, token=token,
                       image_url=f"{base}/h/{token}.png?v={stamp}",
                       page_url=f"{base}/h/{token}"),
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store",
                 "X-Robots-Tag": "noindex, nofollow",
                 "Referrer-Policy": "no-referrer"},
    )


@app.get("/preview")
async def preview_morning(phone: str, full: bool = False, evening: bool = False):
    """What this user's morning would actually look like.

    Defaults to the real thing — weather, commute, their team, and 1-2 opening
    highlights plus their page link. Pass full=1 for the long-form text
    briefing, which is now only the fallback for when the page can't be built.
    Pass evening=1 for tonight's update — the diff against this morning, or
    "(nothing changed)" when there is nothing to send."""
    if evening:
        from evening import _compose_evening
        message, _ = _compose_evening(phone)
        return PlainTextResponse(message or "(nothing changed since this morning — no evening update would be sent)")
    if full:
        return PlainTextResponse(generate_morning(phone))
    from morning import _compose_morning
    message, _ = _compose_morning(phone)
    return PlainTextResponse(message)
