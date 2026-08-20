import os

from twilio.rest import Client as TwilioClient

from smstext import _sms_clean, shorten_message

_twilio = TwilioClient(os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
_APP_URL = os.environ.get("APP_URL", "").rstrip("/")
_STATUS_CALLBACK_URL = f"{_APP_URL}/sms-status" if _APP_URL else None


FALLBACK_SMS = "something went sideways on my end, try again"

_SMS_CHUNK_LIMIT = 900  # GSM-7 safe across all US carriers (~6 segments) per message part


def _split_for_sms(text: str, max_chars: int = _SMS_CHUNK_LIMIT) -> list[str]:
    """Split cleaned text into multiple SMS-sized parts instead of truncating it.
    Prefers splitting on paragraph breaks; falls back to hard chunks if the text has
    no natural break points (e.g. one long unbroken paragraph)."""
    if len(text) <= max_chars:
        return [text]
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(parts) > 1:
        return parts
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def send_sms(to: str, body: str, *, add_status_callback: bool = True, media_url: str | None = None) -> bool:
    """Send SMS/MMS with cleaning, chunking, and fallbacks. Long text is sent as multiple
    SMS parts rather than truncated. Returns True if Twilio accepted the send."""
    if body:
        body = _sms_clean(body)
        if not body.strip():
            body = FALLBACK_SMS
    elif not media_url:
        body = FALLBACK_SMS

    from_number = os.environ["TWILIO_PHONE_NUMBER"]
    kwargs = {"from_": from_number, "to": to}
    if media_url:
        kwargs["media_url"] = [media_url]
    if add_status_callback and _STATUS_CALLBACK_URL:
        kwargs["status_callback"] = _STATUS_CALLBACK_URL

    def _create(text: str) -> None:
        if not text and media_url:
            _twilio.messages.create(**kwargs)
            return
        for part in _split_for_sms(text):
            _twilio.messages.create(body=part, **kwargs)

    def _candidates():
        if body:
            yield body
            if len(body) > 320:
                yield shorten_message(body)
                yield body[:320]
        yield FALLBACK_SMS

    seen: set[str] = set()
    for attempt in _candidates():
        attempt = _sms_clean((attempt or FALLBACK_SMS).strip())
        if not attempt or attempt in seen:
            continue
        seen.add(attempt)
        try:
            _create(attempt)
            return True
        except Exception as e:
            print(f"Send failed for {to}: {e}")

    if media_url:
        try:
            _create("")
            return True
        except Exception as e:
            print(f"MMS send failed for {to}: {e}")

    return False


def ensure_sms(to: str, body: str, **kwargs) -> bool:
    """Send SMS, exhausting all fallbacks. Last resort is the plain fallback string."""
    if send_sms(to, body, **kwargs):
        return True
    return send_sms(to, FALLBACK_SMS, add_status_callback=False)
