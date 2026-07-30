from datetime import datetime, timezone, timedelta
from agent import client, _search, _sms_clean, HAIKU_MODEL, SONNET_MODEL
from db import get_active_watches, update_watch_alerted


def _check_watch_hit(results: str, description: str) -> bool:
    """Ask Haiku whether search results actually match the watch description."""
    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=10,
        messages=[{"role": "user", "content": f"""Watch description: "{description}"

Search results:
{results[:2000]}

Do these results contain a clear, new event that matches the watch description?
Reply with exactly YES or NO."""}],
    )
    return response.content[0].text.strip().upper().startswith("YES")


def _draft_watch_alert(results: str, description: str) -> str:
    """Draft a brief SMS alert for a triggered watch."""
    response = client.messages.create(
        model=SONNET_MODEL,
        max_tokens=200,
        messages=[{"role": "user", "content": f"""You're Palmer, a concise AI texting assistant.

The user asked you to watch for: "{description}"

Here's what just came up in the news:
{results[:2000]}

Write a 1-2 sentence SMS telling them what happened. Be specific — name the event, not just "something happened." Plain ASCII only, no emoji, no markdown."""}],
    )
    return _sms_clean(response.content[0].text.strip())


def run_watches():
    """Check all active watches and send alerts if triggered. Called every 30 minutes by APScheduler."""
    from sms_util import send_sms
    from db import save_message

    watches = get_active_watches()
    now = datetime.now(timezone.utc)

    for watch in watches:
        try:
            if watch["last_alerted"]:
                last = datetime.fromisoformat(watch["last_alerted"])
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if now - last < timedelta(hours=watch["cooldown_hours"]):
                    continue

            all_results = []
            for query in watch["queries"]:
                results = _search(query, days=1)
                if results:
                    all_results.append(results)

            if not all_results:
                continue

            combined = "\n\n".join(all_results)
            if not _check_watch_hit(combined, watch["description"]):
                continue

            alert = _draft_watch_alert(combined, watch["description"])
            send_sms(watch["phone"], alert)
            save_message(watch["phone"], "assistant", alert)
            update_watch_alerted(watch["id"])
            print(f"Watch {watch['id']} triggered for {watch['phone']}: {alert[:80]}")

        except Exception as e:
            print(f"Watch {watch['id']} check failed: {e}")
