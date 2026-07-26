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
from agent import get_reply, commit_reply, shorten_message
from morning import generate_morning, extract_morning_prefs
from db import get_profile, upsert_profile, save_message
from send_reminders import send_due_reminders
from alerts import run_alert_checks

INTRO_MESSAGE = (
    "Hey! I'm Palmer. I'll text you every morning with whatever you actually care about — "
    "weather, sports scores, news, crypto prices, local stuff, anything — and I'm here during the day for "
    "questions, reminders, or whatever.\n\n"
    "Two things to get started: what city are you in, and what do you want in your morning update? "
    "Just tell me in plain english."
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


def _handle_sms(from_number: str, body: str, media_url: str | None, is_new_user: bool, is_preference_reply: bool):
    token = object()
    with _in_flight_lock:
        _in_flight[from_number].add(token)

    try:
        with _phone_locks[from_number]:  # serialize per phone so history never interleaves
            try:
                reply, gif_url = get_reply(phone_number=from_number, message=body, media_url=media_url)
            except Exception as e:
                print(f"get_reply failed for {from_number}: {e}")
                _send_outbound(from_number, "something went sideways on my end, try again")
                return

            with _in_flight_lock:
                add_quote = len(_in_flight[from_number]) > 1

            if add_quote:
                snippet = body if len(body) <= 50 else body[:50].rstrip() + "…"
                reply = f"> {snippet}\n{reply}"

            _send_with_retry(from_number, reply)
            if gif_url:
                _send_gif_outbound(from_number, gif_url)
            commit_reply(from_number, body or "[photo]", reply)
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
):
    validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])
    form_data = await request.form()
    url = str(request.url)
    if request.headers.get("x-forwarded-proto") == "https":
        url = url.replace("http://", "https://", 1)
    if not validator.validate(url, dict(form_data), request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(status_code=403)

    body = Body.strip()
    media_url = MediaUrl0 if NumMedia > 0 else None

    profile_before = get_profile(From)
    # New user: never received intro AND never went through old onboarding flow
    is_new_user = not profile_before.get("intro_sent") and not profile_before.get("morning_onboarded")
    # Preference reply: got the intro but hasn't set their morning topics yet
    is_preference_reply = (
        profile_before.get("intro_sent")
        and not profile_before.get("morning_prefs_received")
    )

    background_tasks.add_task(_handle_sms, From, body, media_url, is_new_user, is_preference_reply)

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
