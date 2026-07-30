from dotenv import load_dotenv
load_dotenv()

import os
from twilio.rest import Client as TwilioClient
from db import claim_due_reminders, save_message
from agent import _sms_clean


def send_due_reminders():
    reminders = claim_due_reminders()
    if not reminders:
        return

    twilio = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    from_number = os.environ["TWILIO_PHONE_NUMBER"]

    for r in reminders:
        try:
            body = _sms_clean(f"hey - {r['text']}")
            twilio.messages.create(body=body, from_=from_number, to=r["phone"])
            save_message(r["phone"], "assistant", body)
            print(f"Sent reminder {r['id']} to {r['phone']}: {r['text']}")
        except Exception as e:
            print(f"Failed reminder {r['id']} to {r['phone']}: {e}")


if __name__ == "__main__":
    send_due_reminders()
