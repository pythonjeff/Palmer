from dotenv import load_dotenv
load_dotenv()

import os
import threading
from collections import defaultdict
from fastapi import FastAPI, Form, Response, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from apscheduler.schedulers.background import BackgroundScheduler
from agent import get_reply, save_assistant_turn, shorten_message, _sms_clean
from morning import generate_morning, extract_morning_prefs, send_morning_messages, send_missing_data_asks, _format_morning_time
from alerts import run_alert_checks
from followup import run_followups
from db import get_profile, upsert_profile, save_message, get_history, HISTORY_LIMIT
from send_reminders import send_due_reminders
from watches import run_watches
from sms_util import ensure_sms, send_sms, FALLBACK_SMS

INTRO_MESSAGE = (
    "oh — also, I'm Palmer. mornings I send a quick rundown of whatever's actually relevant to you. "
    "rest of the day I'm here.\n\n"
    "what city are you in, and what should I be tracking for you?"
)

app = FastAPI()

_scheduler = BackgroundScheduler()
_scheduler.add_job(send_due_reminders, "interval", minutes=1)
_scheduler.add_job(send_morning_messages, "interval", minutes=5)
_scheduler.add_job(run_watches, "interval", minutes=30)
_scheduler.add_job(run_alert_checks, "interval", minutes=60)
_scheduler.add_job(send_missing_data_asks, "interval", minutes=60)
_scheduler.add_job(run_followups, "interval", hours=4)
_scheduler.start()

_in_flight: dict[str, set] = defaultdict(set)
_in_flight_lock = threading.Lock()
_phone_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

_seen_sids: list[str] = []
_seen_sids_lock = threading.Lock()
_SEEN_SIDS_MAX = 200


_APP_URL = os.environ.get("APP_URL", "").rstrip("/")
_STATUS_CALLBACK_URL = f"{_APP_URL}/sms-status" if _APP_URL else None



def _send_gif_outbound(to: str, media_url: str):
    from twilio.rest import Client as TwilioClient
    try:
        TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]).messages.create(
            from_=os.environ["TWILIO_PHONE_NUMBER"], to=to, media_url=[media_url]
        )
    except Exception as e:
        print(f"GIF send failed for {to}: {e}")


def _handle_sms(from_number: str, body: str, media_url: str | None):
    reply_sent = False
    try:
        reply_sent = _handle_sms_inner(from_number, body, media_url)
    except Exception as e:
        print(f"UNHANDLED ERROR in _handle_sms for {from_number}: {e}")
    if not reply_sent:
        ensure_sms(from_number, FALLBACK_SMS)


def _handle_sms_inner(from_number: str, body: str, media_url: str | None) -> bool:
    profile_before = get_profile(from_number)
    is_new_user = not profile_before.get("intro_sent") and not profile_before.get("morning_onboarded")
    is_preference_reply = (
        profile_before.get("intro_sent") and not profile_before.get("morning_prefs_received")
    )

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
                    phone_number=from_number, message=body, media_url=media_url, history=history
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

    if is_new_user:
        upsert_profile(from_number, {"intro_sent": True})
        send_sms(from_number, INTRO_MESSAGE)
        save_message(from_number, "assistant", INTRO_MESSAGE)
    elif is_preference_reply:
        topics = extract_morning_prefs(from_number, body)
        upsert_profile(from_number, {"morning_onboarded": True, "morning_prefs_received": True})
        updated_profile = get_profile(from_number)
        time_str = _format_morning_time(updated_profile.get("morning_time"))
        if topics:
            topic_str = ", ".join(topics)
            confirmation = _sms_clean(
                f"got it — weather plus {topic_str}, every morning at {time_str}. "
                "text me if you want a different time."
            )
        else:
            confirmation = _sms_clean(
                f"got it — I'll send your local weather every morning at {time_str}. "
                "text me anytime to add topics or change the time."
            )
        send_sms(from_number, confirmation)
        save_message(from_number, "assistant", confirmation)

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


@app.post("/sms-status")
async def sms_status_webhook(
    request: Request,
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
        print(f"Delivery failed for {To} (SID={MessageSid}, error={ErrorCode}) — shortening and retrying")
        try:
            from twilio.rest import Client as TwilioClient
            original = TwilioClient(
                os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
            ).messages(MessageSid).fetch()
            ensure_sms(To, shorten_message(original.body), add_status_callback=False)
            print(f"Status-callback retry sent to {To}")
        except Exception as e:
            print(f"Status-callback retry failed for {To}: {e}")

    return Response(status_code=204)


@app.get("/preview")
async def preview_morning(phone: str):
    message = generate_morning(phone)
    return PlainTextResponse(message)
