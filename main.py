from dotenv import load_dotenv
load_dotenv()

import os
from fastapi import FastAPI, Form, Response, BackgroundTasks
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from agent import get_reply, commit_reply
from morning import generate_morning
from hourly import _check_weather, _check_sports, _check_deals
from db import get_profile, upsert_profile, get_due_reminders

app = FastAPI()


def _send_outbound(to: str, body: str):
    twilio = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    twilio.messages.create(body=body, from_=os.environ["TWILIO_PHONE_NUMBER"], to=to)


def _handle_sms(from_number: str, body: str, is_preference_reply: bool):
    reply = get_reply(phone_number=from_number, message=body)
    _send_outbound(from_number, reply)
    commit_reply(from_number, body, reply)

    if is_preference_reply:
        upsert_profile(from_number, {"morning_prefs_received": True})
        briefing = generate_morning(from_number)
        _send_outbound(from_number, briefing)


@app.post("/sms")
async def sms_webhook(
    background_tasks: BackgroundTasks,
    From: str = Form(...),
    Body: str = Form(...),
):
    profile_before = get_profile(From)
    is_preference_reply = (
        profile_before.get("morning_onboarded")
        and not profile_before.get("morning_prefs_received")
    )

    background_tasks.add_task(_handle_sms, From, Body.strip(), is_preference_reply)

    return Response(content=str(MessagingResponse()), media_type="application/xml")


@app.get("/preview")
async def preview_morning(phone: str):
    message = generate_morning(phone)
    return PlainTextResponse(message)


@app.get("/preview/hourly")
async def preview_hourly(phone: str):
    profile = get_profile(phone)
    lines = []

    reminders = get_due_reminders(phone)
    if reminders:
        for r in reminders:
            lines.append(f"[REMINDER] {r['text']}")
    else:
        lines.append("[REMINDER] None due")

    for checker in [_check_weather, _check_sports, _check_deals]:
        try:
            result = checker(profile)
            label = checker.__name__.replace("_check_", "").upper()
            lines.append(f"[{label}] {result or 'NO_ALERT'}")
        except Exception as e:
            lines.append(f"[{checker.__name__}] ERROR: {e}")

    return PlainTextResponse("\n\n".join(lines))
