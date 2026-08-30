import os

from twilio.rest import Client as TwilioClient

from smstext import URL_RE, _sms_clean, shorten_message, truncate_preserving_urls

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
    # Hard chunking: break on whitespace and never mid-URL. The old
    # fixed-width slice split a link across two SMS parts, so neither half was
    # tappable and the message read as corrupted.
    chunks, rest = [], text
    while len(rest) > max_chars:
        head = truncate_preserving_urls(rest, max_chars)
        if not head or head == rest:
            head = rest[:max_chars]
        chunks.append(head.strip())
        rest = rest[len(head):].lstrip()
    if rest.strip():
        chunks.append(rest.strip())
    return chunks or [text]


def send_sms(to: str, body: str, *, add_status_callback: bool = True, media_url: str | None = None) -> bool:
    """Send SMS/MMS with cleaning, chunking, and fallbacks. Long text is sent as multiple
    SMS parts rather than truncated. Returns True if Twilio accepted the send."""
    if body:
        body = _sms_clean(body)
        if not body.strip():
            body = FALLBACK_SMS
    elif not media_url:
        body = FALLBACK_SMS

    # Last line of defence against Palmer narrating its own decisions to the
    # reader. A user received "Both of these fall into the crime/dark content
    # category they explicitly asked to avoid. Skipping." — the drafter
    # announcing a suppression, in the third person, to the person it was about.
    #
    # Refusing the send is the RIGHT outcome, not a compromise: every one of
    # these was a drafter saying it had decided not to send something. Doing
    # what it said, silently, is what it was trying to do. Checked here because
    # this is the one function every outbound message passes through, and the
    # guard it replaces lived in morning.py where four other senders never saw it.
    from guards import leaks_deliberation
    if body and leaks_deliberation(body):
        print(f"BLOCKED internal deliberation to {to}: {body[:110]!r}")
        return False

    # A message carrying a URL must not opt into the delivery-status callback.
    # /sms-status answers a content-size failure by running the original body
    # back through shorten_message and resending it, and until now that was a
    # Haiku rewrite plus a hard slice — i.e. the retry could mangle the very
    # link the message existed to deliver. morning.py already passed False for
    # this reason; chat replies, watch alerts and price alerts all carried URLs
    # and did not. Deciding it here means every sender inherits the rule
    # instead of each one remembering it.
    if add_status_callback and body and URL_RE.search(body):
        add_status_callback = False

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
                # Was body[:320], which cut mid-URL on exactly the messages most
                # likely to carry one.
                yield truncate_preserving_urls(body, 320)
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
    """Send SMS, exhausting all fallbacks. Last resort is the plain fallback string.

    ONLY for a message the user is actively waiting on — i.e. a reply to a text
    they just sent. The contract here is "never leave them with silence", and
    that is the wrong trade for anything unprompted: a price watch used to route
    through this, so a failed check texted "something went sideways on my end,
    try again" to someone who had asked for nothing at all. An unprompted
    message with nothing to say should say nothing. Proactive senders use
    send_sms and accept False."""
    if send_sms(to, body, **kwargs):
        return True
    return send_sms(to, FALLBACK_SMS, add_status_callback=False)
