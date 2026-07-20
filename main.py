from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Form, Response
from fastapi.responses import PlainTextResponse
from twilio.twiml.messaging_response import MessagingResponse
from agent import get_reply
from morning import generate_morning

app = FastAPI()


@app.post("/sms")
async def sms_webhook(
    From: str = Form(...),
    Body: str = Form(...),
):
    reply = get_reply(phone_number=From, message=Body.strip())
    twiml = MessagingResponse()
    twiml.message(reply)
    return Response(content=str(twiml), media_type="application/xml")


@app.get("/preview")
async def preview_morning(phone: str):
    message = generate_morning(phone)
    return PlainTextResponse(message)
