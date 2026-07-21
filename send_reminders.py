from dotenv import load_dotenv
load_dotenv()

import os
from twilio.rest import Client as TwilioClient
from db import get_all_due_reminders, mark_reminder_sent


def send_due_reminders():
    reminders = get_all_due_reminders()
    if not reminders:
        return

    twilio = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
    from_number = os.environ["TWILIO_PHONE_NUMBER"]

    for r in reminders:
        try:
            twilio.messages.create(
                body=f"hey — {r['text']}",
                from_=from_number,
                to=r["phone"],
            )
            mark_reminder_sent(r["id"])
            print(f"Sent reminder {r['id']} to {r['phone']}: {r['text']}")
        except Exception as e:
            print(f"Failed reminder {r['id']} to {r['phone']}: {e}")


if __name__ == "__main__":
    send_due_reminders()
