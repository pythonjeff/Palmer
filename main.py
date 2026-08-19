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
from morning import generate_morning, send_morning_messages, send_missing_data_asks
from alerts import run_alert_checks
from followup import run_followups
from db import get_profile, upsert_profile, save_message, get_history, HISTORY_LIMIT
from send_reminders import send_due_reminders
from watches import run_watches
from shopping import run_price_watches
from sms_util import ensure_sms, send_sms, FALLBACK_SMS
from tapback import parse_reaction, record_reaction, interpret_reaction, learn_from_reactions

app = FastAPI()

_scheduler = BackgroundScheduler()
_scheduler.add_job(send_due_reminders, "interval", minutes=1)
_scheduler.add_job(send_morning_messages, "interval", minutes=5)
_scheduler.add_job(run_watches, "interval", minutes=30)
_scheduler.add_job(run_alert_checks, "interval", minutes=60)
_scheduler.add_job(send_missing_data_asks, "interval", minutes=60)
_scheduler.add_job(run_followups, "interval", hours=4)
# SerpAPI: 12h cadence keeps the starter plan (5000 searches/mo) comfortable
# while leaving headroom for Amazon watches (dual-source: Google Shopping +
# amazon_product engine both go through this same tick).
_scheduler.add_job(run_price_watches, "interval", hours=12)
_scheduler.start()

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
            verdict = interpret_reaction(
                reaction, last_assistant, get_profile(from_number)
            )
            record_reaction(from_number, reaction, verdict)
            learn_from_reactions(from_number)

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


@app.get("/preview")
async def preview_morning(phone: str):
    message = generate_morning(phone)
    return PlainTextResponse(message)
