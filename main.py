from dotenv import load_dotenv
load_dotenv()

import os
import threading
from collections import defaultdict
from fastapi import FastAPI, Form, Response, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from apscheduler.schedulers.background import BackgroundScheduler
from agent import get_reply, save_assistant_turn, shorten_message
from morning import generate_morning, extract_morning_prefs
from db import get_profile, upsert_profile, save_message, get_history
from send_reminders import send_due_reminders
from alerts import run_alert_checks

INTRO_MESSAGE = (
    "oh — also, I'm Palmer. mornings I send a quick rundown of whatever's actually relevant to you. "
    "rest of the day I'm here.\n\n"
    "what city are you in, and what should I be tracking for you?"
)

app = FastAPI()

_scheduler = BackgroundScheduler()
_scheduler.add_job(send_due_reminders, "interval", minutes=1)
_scheduler.add_job(run_alert_checks, "interval", minutes=30)
_scheduler.start()

_in_flight: dict[str, set] = defaultdict(set)
_in_flight_lock = threading.Lock()
_phone_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_twilio = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])

_seen_sids: list[str] = []
_seen_sids_lock = threading.Lock()
_SEEN_SIDS_MAX = 200


def _send_outbound(to: str, body: str):
    from_number = os.environ["TWILIO_PHONE_NUMBER"]
    if len(body) <= 1500:
        _twilio.messages.create(body=body, from_=from_number, to=to)
        return
    # Split on paragraph breaks first, then hard-split anything still too long
    parts = [p.strip() for p in body.split("\n\n") if p.strip()]
    if len(parts) <= 1:
        parts = [body[i:i+1500] for i in range(0, len(body), 1500)]
    for part in parts:
        _twilio.messages.create(body=part, from_=from_number, to=to)


def _send_with_retry(to: str, body: str):
    """Send a message, shortening with Haiku and retrying once if it fails."""
    try:
        _send_outbound(to, body)
        return
    except Exception as e:
        print(f"Send failed for {to}: {e} — shortening and retrying")
    try:
        _send_outbound(to, shorten_message(body))
    except Exception as e2:
        print(f"Retry also failed for {to}: {e2}")
        try:
            _send_outbound(to, "something went sideways sending that — ask me again")
        except Exception:
            pass


def _send_gif_outbound(to: str, media_url: str):
    _twilio.messages.create(from_=os.environ["TWILIO_PHONE_NUMBER"], to=to, media_url=[media_url])


def _handle_sms(from_number: str, body: str, media_url: str | None):
    try:
        _handle_sms_inner(from_number, body, media_url)
    except Exception as e:
        print(f"UNHANDLED ERROR in _handle_sms for {from_number}: {e}")


def _handle_sms_inner(from_number: str, body: str, media_url: str | None):
    profile_before = get_profile(from_number)
    is_new_user = not profile_before.get("intro_sent") and not profile_before.get("morning_onboarded")
    is_preference_reply = (
        profile_before.get("intro_sent") and not profile_before.get("morning_prefs_received")
    )

    token = object()
    with _in_flight_lock:
        _in_flight[from_number].add(token)

    try:
        with _phone_locks[from_number]:  # serialize per phone so history never interleaves
            # Fetch history BEFORE saving the current message so it isn't double-included
            history = get_history(from_number, limit=20)
            # Save user message NOW — if the process dies mid-reply, this exchange
            # is still in DB so the next message has full context
            save_message(from_number, "user", body or "[photo]")

            try:
                reply, gif_url = get_reply(
                    phone_number=from_number, message=body, media_url=media_url, history=history
                )
            except Exception as e:
                print(f"get_reply failed for {from_number}: {e}")
                try:
                    _send_outbound(from_number, "something went sideways on my end, try again")
                except Exception as e2:
                    print(f"fallback send also failed for {from_number}: {e2}")
                return

            if not reply:
                print(f"get_reply returned empty string for {from_number}")
                try:
                    _send_outbound(from_number, "something went sideways on my end, try again")
                except Exception as e2:
                    print(f"fallback send also failed for {from_number}: {e2}")
                return

            with _in_flight_lock:
                add_quote = len(_in_flight[from_number]) > 1

            if add_quote:
                snippet = body if len(body) <= 50 else body[:50].rstrip() + "…"
                reply = f"> {snippet}\n{reply}"

            _send_with_retry(from_number, reply)
            if gif_url:
                _send_gif_outbound(from_number, gif_url)
            save_assistant_turn(from_number, body, reply)
            print(f"Replied to {from_number}: {reply[:100]}")
    finally:
        with _in_flight_lock:
            _in_flight[from_number].discard(token)

    if is_new_user:
        upsert_profile(from_number, {"intro_sent": True})
        _send_outbound(from_number, INTRO_MESSAGE)
        save_message(from_number, "assistant", INTRO_MESSAGE)
    elif is_preference_reply:
        extract_morning_prefs(from_number, body)
        upsert_profile(from_number, {"morning_onboarded": True, "morning_prefs_received": True})


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


@app.get("/preview")
async def preview_morning(phone: str):
    message = generate_morning(phone)
    return PlainTextResponse(message)


@app.get("/preview/hourly")
async def preview_hourly(phone: str):
    from hourly import _check_weather, _check_sports, _check_deals
    profile = get_profile(phone)
    lines = []
    for checker in [_check_weather, _check_sports, _check_deals]:
        try:
            result = checker(profile)
            label = checker.__name__.replace("_check_", "").upper()
            lines.append(f"[{label}] {result or 'NO_ALERT'}")
        except Exception as e:
            lines.append(f"[{checker.__name__}] ERROR: {e}")
    return PlainTextResponse("\n\n".join(lines))
