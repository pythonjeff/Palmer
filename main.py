from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Form, Response
from twilio.twiml.messaging_response import MessagingResponse
from agent import get_reply

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
