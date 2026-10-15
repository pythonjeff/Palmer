# Sms

Inbound and outbound SMS. Governs `sms_util.py`, `smstext.py`, `tapback.py`, the `/sms` and `/sms-status` routes.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## Reactions (tapback.py)

iMessage and Google Messages degrade reactions to plain text over SMS (`Liked "..."`), so they arrive as ordinary inbound messages. `main._handle_sms_inner` short-circuits on them before anything else runs:

1. `parse_reaction()` — free regex; is this a reaction at all
2. `interpret_reaction()` — Haiku, in context and per person: what is the reaction *doing*
3. act — silence for everything except `answer` (a 👍 on "want me to add that?" is a yes)

Silence is both the default and the failure default, so a Haiku outage degrades to silence rather than to unwanted texts. **Returning `True` from the reaction branch is load-bearing** — `_handle_sms` fires `FALLBACK_SMS` on a falsy return.

Reactions then feed `communication_style`, `morning_prefs["avoid"]`, and a pacing factor that stretches followup gaps and lowers the watch cap. Each is behind a threshold so one stray tap can't reshape Palmer, and a dropped topic is announced once via `pending_preference_notice` rather than silently vanishing.

## SMS send pipeline

All outbound SMS goes through `sms_util.send_sms` / `ensure_sms`. It cleans text (`_sms_clean` strips markdown and non-SMS glyphs), splits on paragraph breaks over 1500 chars, and falls back through progressively shorter candidates (original → `shorten_message` → hard truncate → `FALLBACK_SMS`) so a user is never left with silence. Never call Twilio's `messages.create` directly from feature code; go through this module.

## Twilio safety

Every `/sms` and `/sms-status` request is validated with Twilio's HMAC-SHA1 `RequestValidator`. Requests failing validation return 403. All DB queries are parameterized and scoped by phone number.

## `/sms-status` retry

Twilio delivery failures with error codes `30019` or `21617` (content-size issues) trigger an automatic shorten-and-retry via the `/sms-status` webhook. Other delivery failures are logged and dropped — do not add blanket retry-on-any-failure without thinking about loops.

## A URL survives this codebase byte for byte, or is not sent

Three independent mechanical defects produced every "bad link", none of them in
the code that chooses a link:

- the markdown scrub was `[text](anything) -> text`, **deleting** the target, so
  a reply reading `[your page](https://...)` arrived as "your page";
- `encode('ascii', 'ignore')` **drops** bytes rather than failing, so a
  non-ASCII path became a shorter URL that still looks like one — a dead link
  that looks alive is worse than a visibly broken one;
- three paths truncated at a fixed offset (`shorten_message`'s slice,
  `send_sms`'s `body[:320]`, `_split_for_sms`'s hard chunker), each landing
  mid-URL on exactly the messages most likely to carry one.

`smstext.URL_RE` is the one definition. `_protect_urls`/`_restore_urls` hold
links out of every transform and put them back percent-encoded;
`truncate_preserving_urls` never cuts inside one and returns a link alone when it
cannot fit beside prose.

**`shorten_message` never shows the model a URL.** Asking it to preserve a
placeholder was tried and is the worse bet — a dropped marker loses the link
silently. Prose is shortened alone and links are re-appended last, the shape that
lets an app draw a preview.

**`send_sms` decides the status-callback question, not the caller.**
`/sms-status` answers a content-size failure by rerunning the original body
through `shorten_message`, so a message carrying a URL must opt out. `morning.py`
did this by hand and said why; chat replies, watch alerts and price alerts all
carried URLs and did not. Deciding it centrally means a new sender inherits the
rule instead of remembering it.

## History records what was sent, and unprompted means silent-on-failure

`send_sms` returns False on a Twilio failure **and** on a `leaks_deliberation`
block. `alerts.py` and `watches.py` ignored it and saved unconditionally, so
history held messages the user never received — and `_build_system` feeds history
back to the model, which then refers to them. `shopping.py` and `flightwatch.py`
never saved at all, making their alerts invisible both to the model and to their
own `_is_duplicate_subject` check, which reads assistant messages.

The asymmetry on a failed **watch** send is deliberate: the claim stays spent,
because it is a rate limit rather than a delivery record, and retrying every tick
against a body the guard blocks identically is worse than burning one cooldown.
That is the inverse of the reminder rule, for the same reason the reminder rule
gives.

**`ensure_sms` is for replies only.** Its contract — never leave them with
silence — is right for a message the user is waiting on and wrong for anything
unprompted: a failed price check used to text "something went sideways on my
end, try again" to someone who had asked for nothing. Proactive senders use
`send_sms` and accept False. `test_phantom_history.py` asserts none of them
reaches for `ensure_sms` again.

`messages.kind` records which job sent an assistant message (`morning`,
`followup`, `watch`, `price`, `flight`, `reminder`, `reply`, `city_ask`; `alert`
on rows from the retired daily-alert job). NULL means written before the column
existed; readers must tolerate it.
