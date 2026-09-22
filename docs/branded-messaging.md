# Branded messaging: what Palmer has, and what it would take to get the rest

Written after looking at how Reebok's texts arrive on an iPhone — brand name and
logo on a thread nobody saved, tappable action chips, and a short link on a
branded host. This is what each of those actually is, which ones are code, and
which ones are paperwork.

---

## What Reebok is doing

The tell in the screenshot is the label **`Text Message • RCS`**. Not iMessage,
not Apple Messages for Business, not a carrier sender ID.

**1. The name and logo are an RCS Business Messaging agent.** An RBM agent is a
first-class identity in the RCS network rather than a phone number with a label
attached. The display name, logo, accent colour and verified mark live on the
agent record, and the handset renders them from there — which is why there is
nothing for the recipient to save. The "Report Spam" affordance appears for the
same reason: an RBM thread is a business thread by construction, so the client
offers a business-specific control.

**2. `View Now` / `Explore Now` are RBM suggested actions.** A structured
payload (an `openUrl` action on a rich card), not something iOS parsed out of
the message text. You cannot get chips by writing better SMS.

**3. `reebok.attn.tv/aYHagQ_44GaH` is Attentive's link shortener.** Every
Attentive customer gets `{brand}.attn.tv` — a subdomain of the *vendor's*
domain, not one Reebok owns. Short host, ~10-character path.

Three things it is *not*, which are easy to chase by mistake:

- **Google Verified SMS** — discontinued October 2022, folded into RBM.
- **Apple Messages for Business** — a separate Apple channel, blue-bubble,
  inbound-initiated from Maps/Safari/Siri. Not a broadcast channel and not this.
- **Branded Calling** — voice, via STIR/SHAKEN rich call data. Different product.

---

## Where Palmer stands

| | Status |
|---|---|
| Link preview names the brand | **done** — `og:site_name`, `og:image:alt`, favicon, apple-touch-icon |
| Saveable contact card | **done** — `/palmer.vcf`, offered on the page |
| One owner for every public URL | **done** — `links.py` |
| Short branded host | **ready** — set `LINK_DOMAIN`, see below |
| Verified brand name + logo in the thread | needs RCS enrollment |
| Tappable action chips | needs RCS enrollment |

The contact card is the part that needed nobody's approval. It does not give
Palmer a verified identity — it gives the *user* a one-tap way to put a name and
a face on the thread, which is most of the felt difference and works on both
platforms today.

---

## The short domain

`LINK_DOMAIN` fronts every reader-facing URL. `APP_URL` stays what it is:
Twilio's status callbacks post to it and it must remain the real app host.

```
APP_URL=https://palmer-ai-9f3c.herokuapp.com   # Twilio callbacks. Unchanged.
LINK_DOMAIN=palmr.at                           # what a person reads in a text.
```

Point it at the app with a CNAME, add the domain to the dyno so it terminates
TLS, and that is the whole change — no code edit, because `links.py` owns the
shape.

**It must be an alias that serves the page, never a redirecting shortener.**
This is the one constraint worth being rigid about. A link preview is drawn by a
scraper fetching the URL and reading the og tags out of the HTML; a redirect in
front of that makes the preview depend on the scraper chasing it and attributing
the result to the short URL. Some do. iMessage's is the least forgiving thing in
this pipeline already — it is why the og:image carries a `?v=` fingerprint at
all — and a redirect is the standard way a working preview silently stops
working.

Concretely: **do not enable Twilio Link Shortening on the Messaging Service that
carries Palmer's links.** It rewrites URLs in the message body into its own
redirecting host (`yourdomain/` + ten characters), which is exactly that shape.
It is a good product for click tracking on campaign sends; it is the wrong shape
for a link whose preview is most of its value.

**The token stays 22 characters.** A branded shortlink's path is ~10, and that
gap is not worth closing. Reebok's link is scoped to one order and expires;
Palmer's `/h/{token}` is a permanent unauthenticated key to a page naming
someone's city, their commute and the hour they leave the house. The 128 bits
are the authentication (see `artifacts.py`). The host is the part a reader
notices, and that is the half `LINK_DOMAIN` makes changeable.

---

## RCS, if Palmer wants the verified thread

Twilio's RCS is GA and is the route. Registration is two submissions — Google
(all countries) and US carrier launch (vetted via Aegis) — which Twilio's
Console now collects in one guided flow.

**The gate is the paperwork, not the integration.** Expect to need:

- legal business name matching EIN documentation, EIN/FTIN, legal address
- a brand contact in E.164
- **a corporate email on the brand's own domain** — Gmail and Yahoo are rejected
- a real, publicly reachable online presence that a reviewer will search for and
  compare against the submission
- CTIA-grade opt-in evidence: channel-specific consent, nothing pre-checked,
  "Msg & data rates may apply / Reply HELP for help / Reply STOP to cancel"
  visible on one screen, message frequency as a number or "varies" (never
  "up to X"), and at least three sample messages for a recurring program

Brand assets, which `brand.mark_png()` already produces at the right size:

- logo **224×224**, ≤50KB (the default `mark_png()` size, for this reason)
- banner **1440×448**, ≤200KB — *not yet drawn*
- accent colour at **4.5:1 contrast against white** — `brand.INK` (#161510)
  clears this comfortably; the paper tone does not and is a background, not an
  accent
- a ≤100-character description — `brand.TAGLINE`
- live, public Privacy Policy and Terms URLs — **Palmer has neither today**, and
  this is the item most likely to actually block a submission

Rejections are usually boring and preventable: brand-name inconsistency across
documents, a dead URL, vague use-case text, opt-in and frequency that do not
match the samples.

**Rough costs** (verify against Twilio's live pricing before budgeting — these
are second-hand):

- RCS sender onboarding ≈ **$700 one-time**, plus US carrier onboarding fees
- basic RCS ≈ **$0.0083** per message (SMS parity); rich media ≈ **$0.022** send
- carrier surcharges ≈ $0.0035–$0.005 per message
- A2P 10DLC brand: **$4** (sole proprietor / low-volume) or **$44** (standard),
  **$15** campaign vetting, **$1.50–$10**/month campaign renewal

A2P 10DLC registration is worth doing regardless of RCS — it is what gets US
traffic out of unregistered-sender filtering, and it is cheap.

### How it would land in this codebase

Deliberately not built yet — there is nothing to test against until an agent is
approved, and speculative scaffolding is how this repo has been bitten before
(`PRIVATE_COMPANIES`, twice). But the seam already exists, and it is worth
knowing it is narrow:

- Every outbound message already goes through **`sms_util.send_sms`**. That is
  the one function that would grow a `ContentSid`.
- Twilio's **Content API** lets one template hold several content types —
  author a `twilio/card` (title, subtitle, media, actions) *and* a
  `twilio/text`, and Twilio delivers the richest type the device supports with
  the text as fallback. Channel fallback across senders works too: put the RCS
  sender and an SMS number in one Messaging Service sender pool, or pass
  `FallbackFrom`.
- The chips in the Reebok screenshot are `twilio/card` `actions[]` with URL
  actions, or `twilio/call-to-action`. ≤4 buttons render as a rich card.
- RCS content needs **no per-template approval** — that endpoint is
  WhatsApp-only.

The discipline that makes this a small change is already in place: one send
path, and the "single URL, last and alone" rule that `morning.py`'s
`carries_link` enforces.

Note that on RCS the og tags stop mattering — there is no scraper, the card is
a payload you author. The og work above is for the SMS/iMessage path, which is
where Palmer lives today and where it will keep living for every recipient
without RCS.

---

## The one known gap in the current preview

The card is **1200×630**. That is a standard og:image ratio and it renders, but
Apple's guidance favours a square (**1200×1200**) because it survives the
different crops iOS applies across versions and contexts, and the iOS 16+
full-width bubble preview wants ≥2400×1256.

Not changed here: `cards.py`'s entire layout is tuned to 1200×630 — the opening
band sits between the weather chips at ~y354 and the news rule at `H-90`, and
`CARD_OPENING_ROWS` is 3 because that is what fits above it. Re-cutting the
canvas is a design change, not a metadata change. Worth doing deliberately if
previews ever look wrong in practice; the current card is 48KB, comfortably
under the ~1MB weight where preview latency starts to matter (each recipient's
device fetches the image itself, with no server-side proxy).
