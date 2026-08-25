"""One drafter for price-watch alerts, shared by both price sources.

shopping.py (Google Shopping) and amazon.py (a specific ASIN) previously each
carried their own near-identical copy: same signature, character-identical
target/drop percentage math, same prompt skeleton, same fallback. A pricing fix
in one silently missed the other.

Lives in its own module rather than in either source module — shopping.py
already imports amazon.py, and putting the shared code in one of them would
make that coupling bidirectional.
"""
from __future__ import annotations

from agent import _build_system
from llm import client, SONNET_MODEL
from smstext import _sms_clean

MAX_CHARS = 180


def _context(product_name: str, current: dict, watch: dict, reason: str,
             source_label: str | None) -> str:
    """The facts the drafting model gets. Identical for both sources apart from
    how the price line names where the price came from."""
    if source_label:
        price_line = f"{source_label} price: ${current['price']:.2f}"
    else:
        price_line = f"Now: ${current['price']:.2f} at {current.get('merchant') or 'unknown seller'}"

    lines = [f"Product: {product_name}", price_line]

    target = watch.get("target_price")
    baseline = watch.get("baseline_price")
    if reason == "target" and target is not None:
        lines.append(f"They wanted it at or under ${float(target):.2f} - done.")
    elif reason in ("drop", "rise") and baseline:
        baseline = float(baseline)
        delta = current["price"] - baseline
        pct = abs(delta / baseline) * 100
        way = "Up" if delta > 0 else "Down"
        # Dollars lead: the materiality rule is a flat $2, so the dollar move is
        # the fact that earned this text. The percentage is context, not trigger.
        lines.append(f"{way} ${abs(delta):.2f} (about {pct:.0f}%) from ${baseline:.2f}.")
    return "\n".join(lines)


def _fallback(product_name: str, current: dict, source_label: str | None) -> str:
    """Direction-neutral on purpose: it states where the price IS, which is true
    whether the move was up or down, so one fallback covers every reason."""
    if source_label:
        return f"{product_name} is ${current['price']:.2f} on {source_label}."
    return (f"{product_name} is at ${current['price']:.2f} at "
            f"{current.get('merchant') or 'unknown seller'}.")


def draft_price_alert(product_name: str, current: dict, watch: dict, reason: str,
                      link: str | None = None, source_label: str | None = None) -> str:
    """Palmer-voice one-liner announcing a price watch hit.

    Runs through _build_system so the alert is in the SAME Palmer the user talks
    to — calibrated register, reaction history, profile — rather than a one-line
    approximation of his voice. Sonnet, because this is user-facing drafting.

    `link` is appended after the line when the URL is a clean permalink worth
    sending (Amazon dp/ASIN); Google Shopping URLs are not, so it stays None.
    Never raises — falls back to a plain factual line.
    """
    ctx = _context(product_name, current, watch, reason, source_label)
    url_rule = ("Do NOT include a URL in your line — it gets appended separately. "
                if link else "No URL. ")
    # A rise is not a hit. Telling someone their watch "hit" when the price went
    # UP reads as good news and is actively misleading, so it gets its own lead.
    if reason == "rise":
        lead = ("Tell them something they're tracking went UP in price. "
                "This is not good news and not a win — don't congratulate them "
                "or dress it up. State it plainly and briefly. ")
    else:
        lead = "Tell them their price watch just hit. "
    prompt = (
        f"{lead}One short line, no opener, no ceremony. "
        "Don't say 'alert' or 'notification' — you're a friend, not an app. "
        f"{url_rule}No emoji, no markdown, no bullets. Under {MAX_CHARS} characters.\n\n{ctx}"
    )

    # A missing phone or a failed profile read shouldn't cost the user their
    # alert — degrade to drafting without the personalised system prompt.
    kwargs = {}
    phone = watch.get("phone")
    if phone:
        try:
            kwargs["system"] = _build_system(phone)
        except Exception as e:
            print(f"draft_price_alert: _build_system failed for {phone}: {e}")

    try:
        response = client.messages.create(
            model=SONNET_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        line = _sms_clean(response.content[0].text.strip())
    except Exception as e:
        print(f"draft_price_alert failed: {type(e).__name__}: {e}")
        line = _sms_clean(_fallback(product_name, current, source_label))

    if not line:
        line = _sms_clean(_fallback(product_name, current, source_label))
    return f"{line} {link}" if link else line
