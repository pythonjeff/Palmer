import os
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

_DATABASE_URL = os.environ.get("DATABASE_URL", "")

if _DATABASE_URL:
    import psycopg2
    import psycopg2.extras
    if _DATABASE_URL.startswith("postgres://"):
        _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql://", 1)
    PH = "%s"
    ID_COL = "SERIAL PRIMARY KEY"
    def _conn():
        return psycopg2.connect(_DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
else:
    import sqlite3
    _DB_PATH = Path(__file__).parent / "palmer.db"
    PH = "?"
    ID_COL = "INTEGER PRIMARY KEY AUTOINCREMENT"
    def _conn():
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def init_db():
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS messages (
            id {ID_COL},
            phone TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS users (
            phone TEXT PRIMARY KEY,
            profile TEXT NOT NULL DEFAULT '{{}}'
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS reminders (
            id {ID_COL},
            phone TEXT NOT NULL,
            text TEXT NOT NULL,
            due_at TEXT NOT NULL,
            sent INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS watches (
            id {ID_COL},
            phone TEXT NOT NULL,
            description TEXT NOT NULL,
            queries TEXT NOT NULL,
            cooldown_hours INTEGER NOT NULL DEFAULT 4,
            last_alerted TEXT,
            last_alert_summary TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS price_watches (
            id {ID_COL},
            phone TEXT NOT NULL,
            product_name TEXT NOT NULL,
            target_price REAL,
            currency TEXT NOT NULL DEFAULT 'USD',
            baseline_price REAL,
            last_seen_price REAL,
            last_seen_url TEXT,
            last_seen_merchant TEXT,
            cooldown_hours INTEGER NOT NULL DEFAULT 12,
            last_alerted TEXT,
            last_alert_summary TEXT,
            source TEXT NOT NULL DEFAULT 'shopping',
            asin TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migrations for columns added after initial deploy
    watches_new_cols = [
        "last_alert_summary TEXT",
        "daily_alert_count INTEGER DEFAULT 0",
        "daily_alert_date TEXT",
        "recent_summaries TEXT",
        "genre TEXT",
        "story_state TEXT",
    ]
    for col_def in watches_new_cols:
        if _DATABASE_URL:
            cur.execute(f"ALTER TABLE watches ADD COLUMN IF NOT EXISTS {col_def}")
        else:
            try:
                cur.execute(f"ALTER TABLE watches ADD COLUMN {col_def}")
            except Exception:
                pass  # already exists

    price_watches_new_cols = [
        "source TEXT NOT NULL DEFAULT 'shopping'",
        "asin TEXT",
        "daily_alert_count INTEGER DEFAULT 0",
        "daily_alert_date TEXT",
    ]
    for col_def in price_watches_new_cols:
        if _DATABASE_URL:
            cur.execute(f"ALTER TABLE price_watches ADD COLUMN IF NOT EXISTS {col_def}")
        else:
            try:
                cur.execute(f"ALTER TABLE price_watches ADD COLUMN {col_def}")
            except Exception:
                pass  # already exists

    conn.commit()
    conn.close()


HISTORY_LIMIT = 20


def get_history(phone: str, limit: int = HISTORY_LIMIT) -> list[dict]:
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT role, content FROM messages WHERE phone = {PH} ORDER BY created_at DESC LIMIT {PH}",
        (phone, limit),
    )
    rows = list(reversed(cur.fetchall()))
    conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def get_message_count(phone: str) -> int:
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) AS cnt FROM messages WHERE phone = {PH}", (phone,))
    row = cur.fetchone()
    conn.close()
    return row["cnt"] if row else 0


def get_older_messages(phone: str, skip_recent: int = HISTORY_LIMIT) -> list[dict]:
    """Return messages older than the most recent skip_recent, for consolidation."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT role, content FROM messages
        WHERE phone = {PH}
        ORDER BY created_at DESC
        LIMIT 100 OFFSET {PH}
        """,
        (phone, skip_recent),
    )
    rows = list(reversed(cur.fetchall()))
    conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def save_message(phone: str, role: str, content: str):
    """created_at is set explicitly (rather than left to the column's DB-side
    CURRENT_TIMESTAMP default) so it's always the same ISO8601+offset format used
    everywhere else timestamps are compared as strings in this codebase (reminders,
    watches, price watches). SQLite's CURRENT_TIMESTAMP produces a bare
    'YYYY-MM-DD HH:MM:SS' string with no 'T' or offset, which sorts incorrectly
    against ISO-format cutoffs for same-day comparisons (space < 'T' lexicographically),
    silently breaking any query that filters created_at against a Python-generated
    timestamp."""
    conn = _conn()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        f"INSERT INTO messages (phone, role, content, created_at) VALUES ({PH}, {PH}, {PH}, {PH})",
        (phone, role, content, now),
    )
    conn.commit()
    conn.close()


def get_profile(phone: str) -> dict:
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"SELECT profile FROM users WHERE phone = {PH}", (phone,))
    row = cur.fetchone()
    conn.close()
    return json.loads(row["profile"]) if row else {}


def get_all_phones() -> list[str]:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT phone FROM users")
    phones = [r["phone"] for r in cur.fetchall()]
    conn.close()
    return phones


def upsert_profile(phone: str, updates: dict):
    profile = get_profile(phone)
    profile.update(updates)
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO users (phone, profile) VALUES ({PH}, {PH}) "
        f"ON CONFLICT(phone) DO UPDATE SET profile = EXCLUDED.profile",
        (phone, json.dumps(profile)),
    )
    conn.commit()
    conn.close()


def claim_daily_guard(phone: str, field: str, value: str) -> bool:
    """Atomically claim a one-shot send guard on the user's profile. Returns True only
    if this call transitions `field` away from `value` — i.e. this caller won the race
    and should proceed. A concurrent caller trying to claim the same (phone, field, value)
    blocks on the Postgres row lock, then sees the field already set and gets False.
    Closes the check-then-act race in the recurring send jobs (e.g. two dynos briefly
    overlapping during a Heroku deploy)."""
    conn = _conn()
    cur = conn.cursor()
    if _DATABASE_URL:
        cur.execute(f"SELECT profile FROM users WHERE phone = {PH} FOR UPDATE", (phone,))
    else:
        cur.execute(f"SELECT profile FROM users WHERE phone = {PH}", (phone,))
    row = cur.fetchone()
    profile = json.loads(row["profile"]) if row else {}
    if profile.get(field) == value:
        conn.close()
        return False
    profile[field] = value
    cur.execute(
        f"INSERT INTO users (phone, profile) VALUES ({PH}, {PH}) "
        f"ON CONFLICT(phone) DO UPDATE SET profile = EXCLUDED.profile",
        (phone, json.dumps(profile)),
    )
    conn.commit()
    conn.close()
    return True


def save_reminder(phone: str, text: str, due_at: str):
    normalized = text.lower().strip().rstrip("!")
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT id FROM reminders WHERE phone = {PH} AND LOWER(TRIM(REPLACE(text, '!', ''))) = {PH} AND sent = 0",
        (phone, normalized),
    )
    if cur.fetchone():
        conn.close()
        return
    cur.execute(
        f"INSERT INTO reminders (phone, text, due_at) VALUES ({PH}, {PH}, {PH})",
        (phone, text, due_at),
    )
    conn.commit()
    conn.close()


def cancel_reminders(phone: str, text_match: str = None) -> int:
    conn = _conn()
    cur = conn.cursor()
    if text_match:
        pattern = f"%{text_match.lower().strip()}%"
        cur.execute(
            f"UPDATE reminders SET sent = 1 WHERE phone = {PH} AND LOWER(text) LIKE {PH} AND sent = 0",
            (phone, pattern),
        )
    else:
        cur.execute(
            f"UPDATE reminders SET sent = 1 WHERE phone = {PH} AND sent = 0",
            (phone,),
        )
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count


def claim_due_reminders() -> list[dict]:
    """Atomically mark due reminders as sent and return them. Prevents double-sends."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    cur = conn.cursor()
    if _DATABASE_URL:
        cur.execute(
            """
            UPDATE reminders SET sent = 1
            WHERE id IN (
                SELECT id FROM reminders WHERE due_at <= %s AND sent = 0
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, phone, text
            """,
            (now,),
        )
        rows = cur.fetchall()
    else:
        cur.execute(
            "SELECT id, phone, text FROM reminders WHERE due_at <= ? AND sent = 0",
            (now,),
        )
        rows = cur.fetchall()
        if rows:
            ids = [r["id"] for r in rows]
            cur.execute(f"UPDATE reminders SET sent = 1 WHERE id IN ({','.join(['?'] * len(ids))})", ids)
    conn.commit()
    conn.close()
    return [{"id": r["id"], "phone": r["phone"], "text": r["text"]} for r in rows]


def save_watch(phone: str, description: str, queries: list[str], cooldown_hours: int = 4) -> int:
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO watches (phone, description, queries, cooldown_hours) VALUES ({PH}, {PH}, {PH}, {PH})",
        (phone, description, json.dumps(queries), cooldown_hours),
    )
    watch_id = cur.lastrowid
    conn.commit()
    conn.close()
    return watch_id


def get_active_watches() -> list[dict]:
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, phone, description, queries, cooldown_hours, last_alerted, "
        "last_alert_summary, daily_alert_count, daily_alert_date, recent_summaries, "
        "genre, story_state "
        "FROM watches WHERE active = 1"
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "phone": r["phone"],
            "description": r["description"],
            "queries": json.loads(r["queries"]),
            "cooldown_hours": r["cooldown_hours"],
            "last_alerted": r["last_alerted"],
            "last_alert_summary": r["last_alert_summary"],
            "daily_alert_count": r["daily_alert_count"] or 0,
            "daily_alert_date": r["daily_alert_date"],
            "recent_summaries": json.loads(r["recent_summaries"]) if r["recent_summaries"] else [],
            "genre": r["genre"],
            "story_state": r["story_state"],
        }
        for r in rows
    ]


def get_user_watches(phone: str) -> list[dict]:
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, description, cooldown_hours, last_alerted, genre FROM watches WHERE phone = %s AND active = 1" if _DATABASE_URL
        else "SELECT id, description, cooldown_hours, last_alerted, genre FROM watches WHERE phone = ? AND active = 1",
        (phone,),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"id": r["id"], "description": r["description"], "cooldown_hours": r["cooldown_hours"],
             "last_alerted": r["last_alerted"], "genre": r["genre"]} for r in rows]


def set_watch_genre(watch_id: int, genre: str):
    """Persist a classified genre onto an existing watch row.
    Called lazily by _check_watch_hit the first time a watch is scored."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE watches SET genre = {PH} WHERE id = {PH}", (genre, watch_id))
    conn.commit()
    conn.close()


def update_watch_story(watch_id: int, story_summary: str):
    """Persist a rolling 1-2 sentence 'where we are in the story' summary onto a
    watch. Called after a successful alert send so the next scoring pass can
    ask Haiku 'does this candidate ADVANCE the story below, or rehash it?'
    instead of relying purely on title-level dedup."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE watches SET story_state = {PH} WHERE id = {PH}",
                (story_summary, watch_id))
    conn.commit()
    conn.close()


def claim_watch_alert(watch_id: int, cooldown_hours: float) -> bool:
    """Atomically re-check cooldown and claim this watch for an alert send, closing the
    race between the earlier get_active_watches() read and this write."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=cooldown_hours)).isoformat()
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"""UPDATE watches SET last_alerted = {PH}
            WHERE id = {PH} AND (last_alerted IS NULL OR last_alerted <= {PH})""",
        (now.isoformat(), watch_id, cutoff),
    )
    claimed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return claimed


def update_watch_alerted(watch_id: int, summary: str, recent_summaries: list[str]):
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"""UPDATE watches SET
            last_alerted = {PH},
            last_alert_summary = {PH},
            recent_summaries = {PH},
            daily_alert_count = CASE WHEN daily_alert_date = {PH} THEN daily_alert_count + 1 ELSE 1 END,
            daily_alert_date = {PH}
        WHERE id = {PH}""",
        (now, summary, json.dumps(recent_summaries), today, today, watch_id),
    )
    conn.commit()
    conn.close()


def get_messages_after(phone: str, since_iso: str) -> list[dict]:
    """Return messages for a phone sent after since_iso. Used for engagement detection."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT role FROM messages WHERE phone = {PH} AND created_at > {PH} ORDER BY created_at ASC",
        (phone, since_iso),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"role": r["role"]} for r in rows]


def get_recent_assistant_messages(phone: str, since_iso: str) -> list[str]:
    """Return the text of assistant messages sent to phone since since_iso, oldest
    first. Used for cross-job subject-dedup — checking a new proactive text against
    everything recently sent, regardless of which job sent it."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT content FROM messages WHERE phone = {PH} AND role = 'assistant' "
        f"AND created_at > {PH} ORDER BY created_at ASC",
        (phone, since_iso),
    )
    rows = cur.fetchall()
    conn.close()
    return [r["content"] for r in rows]


def get_recent_user_messages(phone: str, since_iso: str) -> list[str]:
    """Return the text of user messages Palmer received since since_iso, oldest
    first. Used to suppress proactive alerts on stories the user already brought
    up themselves — 'did you see the Iran thing?' at 10am should stop an Iran
    watch fire at 2pm."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT content FROM messages WHERE phone = {PH} AND role = 'user' "
        f"AND created_at > {PH} ORDER BY created_at ASC",
        (phone, since_iso),
    )
    rows = cur.fetchall()
    conn.close()
    return [r["content"] for r in rows]


def cancel_watches(phone: str, text_match: str = None) -> int:
    conn = _conn()
    cur = conn.cursor()
    if text_match:
        pattern = f"%{text_match.lower().strip()}%"
        cur.execute(
            f"UPDATE watches SET active = 0 WHERE phone = {PH} AND LOWER(description) LIKE {PH} AND active = 1",
            (phone, pattern),
        )
    else:
        cur.execute(f"UPDATE watches SET active = 0 WHERE phone = {PH} AND active = 1", (phone,))
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count


def save_price_watch(phone: str, product_name: str, target_price: float | None = None,
                     currency: str = "USD", cooldown_hours: int = 12,
                     source: str = "shopping", asin: str | None = None) -> int:
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO price_watches (phone, product_name, target_price, currency, "
        f"cooldown_hours, source, asin) "
        f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
        (phone, product_name, target_price, currency, cooldown_hours, source, asin),
    )
    watch_id = cur.lastrowid
    conn.commit()
    conn.close()
    return watch_id


def get_active_price_watches() -> list[dict]:
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, phone, product_name, target_price, currency, baseline_price, "
        "last_seen_price, last_seen_url, last_seen_merchant, cooldown_hours, "
        "last_alerted, last_alert_summary, source, asin, "
        "daily_alert_count, daily_alert_date "
        "FROM price_watches WHERE active = 1"
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "phone": r["phone"],
            "product_name": r["product_name"],
            "target_price": r["target_price"],
            "currency": r["currency"],
            "baseline_price": r["baseline_price"],
            "last_seen_price": r["last_seen_price"],
            "last_seen_url": r["last_seen_url"],
            "last_seen_merchant": r["last_seen_merchant"],
            "cooldown_hours": r["cooldown_hours"],
            "last_alerted": r["last_alerted"],
            "last_alert_summary": r["last_alert_summary"],
            "source": r["source"] or "shopping",
            "asin": r["asin"],
            "daily_alert_count": r["daily_alert_count"] or 0,
            "daily_alert_date": r["daily_alert_date"],
        }
        for r in rows
    ]


def get_user_price_watches(phone: str) -> list[dict]:
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, product_name, target_price, currency, baseline_price, last_seen_price, "
        f"cooldown_hours, last_alerted FROM price_watches WHERE phone = {PH} AND active = 1",
        (phone,),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "product_name": r["product_name"],
            "target_price": r["target_price"],
            "currency": r["currency"],
            "baseline_price": r["baseline_price"],
            "last_seen_price": r["last_seen_price"],
            "cooldown_hours": r["cooldown_hours"],
            "last_alerted": r["last_alerted"],
        }
        for r in rows
    ]


def cancel_price_watches(phone: str, text_match: str = None) -> int:
    conn = _conn()
    cur = conn.cursor()
    if text_match:
        pattern = f"%{text_match.lower().strip()}%"
        cur.execute(
            f"UPDATE price_watches SET active = 0 WHERE phone = {PH} AND LOWER(product_name) LIKE {PH} AND active = 1",
            (phone, pattern),
        )
    else:
        cur.execute(f"UPDATE price_watches SET active = 0 WHERE phone = {PH} AND active = 1", (phone,))
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count


def set_price_watch_baseline(watch_id: int, price: float, url: str, merchant: str):
    """Called on the first successful check for a watch. Records the reference price
    without firing an alert."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE price_watches SET baseline_price = {PH}, last_seen_price = {PH}, "
        f"last_seen_url = {PH}, last_seen_merchant = {PH} WHERE id = {PH}",
        (price, price, url, merchant, watch_id),
    )
    conn.commit()
    conn.close()


def claim_price_watch_alert(watch_id: int, cooldown_hours: float) -> bool:
    """Same as claim_watch_alert, for the price_watches table."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=cooldown_hours)).isoformat()
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"""UPDATE price_watches SET last_alerted = {PH}
            WHERE id = {PH} AND (last_alerted IS NULL OR last_alerted <= {PH})""",
        (now.isoformat(), watch_id, cutoff),
    )
    claimed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return claimed


def release_price_watch_claim(watch_id: int):
    """Reset last_alerted after a failed send so the next tick can retry."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE price_watches SET last_alerted = NULL WHERE id = {PH}", (watch_id,))
    conn.commit()
    conn.close()


def update_price_watch_alerted(watch_id: int, price: float, url: str, merchant: str, summary: str):
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"""UPDATE price_watches SET
            last_alerted = {PH},
            last_alert_summary = {PH},
            last_seen_price = {PH},
            last_seen_url = {PH},
            last_seen_merchant = {PH},
            daily_alert_count = CASE WHEN daily_alert_date = {PH} THEN daily_alert_count + 1 ELSE 1 END,
            daily_alert_date = {PH}
        WHERE id = {PH}""",
        (now, summary, price, url, merchant, today, today, watch_id),
    )
    conn.commit()
    conn.close()
