from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Form, Response
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse
from agent import get_reply
from morning import generate_morning
from hourly import _check_weather, _check_sports, _check_deals
from db import get_profile, upsert_profile, get_due_reminders

app = FastAPI()


@app.post("/sms")
async def sms_webhook(
    From: str = Form(...),
    Body: str = Form(...),
):
    profile_before = get_profile(From)
    is_preference_reply = (
        profile_before.get("morning_onboarded")
        and not profile_before.get("morning_prefs_received")
    )

    reply = get_reply(phone_number=From, message=Body.strip())
    twiml = MessagingResponse()
    twiml.message(reply)

    if is_preference_reply:
        upsert_profile(From, {"morning_prefs_received": True})
        briefing = generate_morning(From)
        twiml.message(briefing)

    return Response(content=str(twiml), media_type="application/xml")


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
