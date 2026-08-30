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
    BLOB_COL = "BYTEA"
    def _conn():
        return psycopg2.connect(_DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
else:
    import sqlite3
    _DB_PATH = Path(__file__).parent / "palmer.db"
    PH = "?"
    ID_COL = "INTEGER PRIMARY KEY AUTOINCREMENT"
    BLOB_COL = "BLOB"
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
    cur.execute("""
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
        CREATE TABLE IF NOT EXISTS artifacts (
            token TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            body {BLOB_COL} NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
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
    # Forecast accuracy log. Written by the daily audit job: today's forecast
    # from each source goes in with `actual` NULL, and the next day's run fills
    # the actual in from reanalysis. Keyed by city+date+source so a re-run is
    # idempotent rather than duplicating a day.
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS forecast_audit (
            id {ID_COL},
            city TEXT NOT NULL,
            target_date TEXT NOT NULL,
            source TEXT NOT NULL,
            forecast_high REAL,
            actual_high REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS forecast_audit_key "
                "ON forecast_audit (city, target_date, source)")

    # Flight watches. A route plus dates is the identity, so there is no
    # product_name equivalent — cancelling matches on the route text instead.
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS flight_watches (
            id {ID_COL},
            phone TEXT NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            outbound_date TEXT NOT NULL,
            return_date TEXT,
            target_price REAL,
            baseline_price REAL,
            last_seen_price REAL,
            last_alerted TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # What a user was last TOLD about a game. The comparison that decides
    # whether a moment is worth a text is against this, not against the previous
    # poll — otherwise a score arriving in the same tick as a lead change reads
    # as two separate events.
    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS game_alerts (
            id {ID_COL},
            phone TEXT NOT NULL,
            game_id TEXT NOT NULL,
            home_score INTEGER NOT NULL DEFAULT 0,
            away_score INTEGER NOT NULL DEFAULT 0,
            leader TEXT,
            state TEXT,
            alert_count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS game_alerts_key "
                "ON game_alerts (phone, game_id)")

    watches_new_cols = [
        "last_alert_summary TEXT",
        "daily_alert_count INTEGER DEFAULT 0",
        "daily_alert_date TEXT",
        "recent_summaries TEXT",
        "genre TEXT",
        "story_state TEXT",
        "last_alert_url TEXT",
        "last_alert_domain TEXT",
    ]
    for col_def in watches_new_cols:
        if _DATABASE_URL:
            cur.execute(f"ALTER TABLE watches ADD COLUMN IF NOT EXISTS {col_def}")
        else:
            try:
                cur.execute(f"ALTER TABLE watches ADD COLUMN {col_def}")
            except Exception:
                pass  # already exists

    messages_new_cols = [
        # Which job sent an assistant message. Nothing could tell a morning from
        # a chat reply, so morning.py's anti-repetition guard was comparing
        # today's line against ordinary conversation instead of yesterday's
        # morning. NULL for everything written before this and for inbound.
        "kind TEXT",
    ]
    for col_def in messages_new_cols:
        if _DATABASE_URL:
            cur.execute(f"ALTER TABLE messages ADD COLUMN IF NOT EXISTS {col_def}")
        else:
            try:
                cur.execute(f"ALTER TABLE messages ADD COLUMN {col_def}")
            except Exception:
                pass  # already exists

    reminders_new_cols = [
        "recurrence TEXT",  # NULL = one-shot; see timeutil.RECURRENCES
    ]
    for col_def in reminders_new_cols:
        if _DATABASE_URL:
            cur.execute(f"ALTER TABLE reminders ADD COLUMN IF NOT EXISTS {col_def}")
        else:
            try:
                cur.execute(f"ALTER TABLE reminders ADD COLUMN {col_def}")
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

    # The messages table had no index at all, and it is the hottest table here:
    # every inbound turn reads it, every proactive sender's dedup gate reads it
    # twice, and each read is `WHERE phone = ? ORDER BY created_at DESC`. Both
    # backends accept IF NOT EXISTS.
    cur.execute("CREATE INDEX IF NOT EXISTS messages_phone_time "
                "ON messages (phone, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS messages_phone_role_time "
                "ON messages (phone, role, created_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS reminders_pending "
                "ON reminders (sent, due_at)")

    conn.commit()
    conn.close()

    # Repair reminders written before save_reminder canonicalized due_at. Cheap
    # and idempotent once the table is clean; see normalize_due_at_rows.
    try:
        normalize_due_at_rows()
    except Exception as e:
        print(f"init_db: due_at normalization skipped: {type(e).__name__}: {e}")


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


def save_message(phone: str, role: str, content: str, kind: str | None = None):
    """created_at is set explicitly (rather than left to the column's DB-side
    CURRENT_TIMESTAMP default) so it's always the same ISO8601+offset format used
    everywhere else timestamps are compared as strings in this codebase (reminders,
    watches, price watches). SQLite's CURRENT_TIMESTAMP produces a bare
    'YYYY-MM-DD HH:MM:SS' string with no 'T' or offset, which sorts incorrectly
    against ISO-format cutoffs for same-day comparisons (space < 'T' lexicographically),
    silently breaking any query that filters created_at against a Python-generated
    timestamp.

    `kind` records which job sent an assistant message ("morning", "followup",
    "alert", "watch", "price", "flight", "reminder", "reply"). Optional so every
    existing call site keeps working; NULL means "written before this existed",
    which readers must tolerate."""
    conn = _conn()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute(
        f"INSERT INTO messages (phone, role, content, created_at, kind) "
        f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH})",
        (phone, role, content, now, kind),
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
    """Merge `updates` into the stored profile.

    Read and write share ONE connection, and on Postgres the read takes a row
    lock — this used to be two connections with an unsynchronised gap between
    them, so two concurrent writers (an inbound turn and a scheduler job) could
    each read the same profile and the second write would drop the first's
    fields. Same rationale as claim_daily_guard below."""
    conn = _conn()
    cur = conn.cursor()
    if _DATABASE_URL:
        cur.execute(f"SELECT profile FROM users WHERE phone = {PH} FOR UPDATE", (phone,))
    else:
        cur.execute(f"SELECT profile FROM users WHERE phone = {PH}", (phone,))
    row = cur.fetchone()
    profile = json.loads(row["profile"]) if row else {}
    # A None value DELETES the key rather than storing a null. Callers already
    # use None to mean "clear this" (release a send guard, retire an alias) and
    # every reader goes through .get(), so null and absent are equivalent to
    # them — but a stored null still costs prompt tokens, because the whole
    # profile is dumped as JSON into every system prompt.
    for key, value in updates.items():
        if value is None:
            profile.pop(key, None)
        else:
            profile[key] = value
    cur.execute(
        f"INSERT INTO users (phone, profile) VALUES ({PH}, {PH}) "
        f"ON CONFLICT(phone) DO UPDATE SET profile = EXCLUDED.profile",
        (phone, json.dumps(profile)),
    )
    conn.commit()
    conn.close()


def get_all_profiles() -> list[tuple[str, dict]]:
    """Every (phone, profile) in one query.

    The scheduler jobs used to call get_all_phones() and then get_profile() per
    user — one connection each, N+1 per tick across morning, alerts and
    followups."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute("SELECT phone, profile FROM users")
    rows = cur.fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            out.append((r["phone"], json.loads(r["profile"]) if r["profile"] else {}))
        except (ValueError, TypeError):
            out.append((r["phone"], {}))
    return out


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


# Function words plus recurrence words. The recurrence words are stripped
# because cadence is a column now, so "daily" inside the text is noise that
# would otherwise make "Daily X update" look unlike "X update".
_REMINDER_STOPWORDS = {
    "a", "an", "the", "to", "for", "of", "and", "or", "is", "it", "on", "at",
    "in", "my", "me", "you", "your", "user", "about", "how", "did", "go",
    "went", "get", "that", "this", "with", "please", "remind", "reminder",
    "daily", "weekly", "every", "each", "day",
}


def _reminder_tokens(text: str) -> set:
    cleaned = "".join(c if c.isalnum() else " " for c in (text or "").lower())
    return {t for t in cleaned.split() if t and t not in _REMINDER_STOPWORDS}


def _similar_reminder_text(a: str, b: str, threshold: float = 0.5) -> bool:
    """Token-overlap (Jaccard) similarity, no model call — this runs on the
    write path inside a live conversation turn.

    Exact-match dedup was not enough because the text is drafted by a model, so
    near-identical-but-not-identical is the expected case rather than the
    exception: two rows that differed only by an em-dash vs a hyphen both got
    stored, and the user was texted twice in the same minute."""
    ta, tb = _reminder_tokens(a), _reminder_tokens(b)
    if not ta or not tb:
        return (a or "").strip().lower() == (b or "").strip().lower()
    return len(ta & tb) / len(ta | tb) >= threshold


def _parse_due(value) -> datetime | None:
    """Parse a stored/incoming due_at. Tolerates the trailing 'Z' form the tool
    schema asks for as well as offset forms already in the table."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def save_reminder(phone: str, text: str, due_at: str, recurrence: str | None = None):
    """Store a reminder, skipping one that duplicates a pending reminder.

    A duplicate is same-time AND similar-text; the old guard was similar-text
    alone (in fact exact-text), ignoring due_at entirely, which was wrong in both
    directions. It let four rephrasings of one ask through at the same minute,
    and it would silently drop a legitimate second "call mom" set for next week
    while the first was still pending.
    """
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, text, due_at FROM reminders WHERE phone = {PH} AND sent = 0",
        (phone,),
    )
    target = _parse_due(due_at)
    if target is None:
        # An unparseable due_at would sit in the table forever: claim_due_reminders
        # compares strings, so it would either never match or match wrongly. Drop
        # it here rather than storing something no reader can honour.
        print(f"save_reminder: unparseable due_at {due_at!r} for {phone}, not saved")
        conn.close()
        return
    # ONE canonical shape, always. claim_due_reminders decides due-ness with a
    # LEXICOGRAPHIC `due_at <= now` against a Python `+00:00` isoformat, so that
    # comparison is correct only while every writer agrees on the format — and
    # until now nothing made them. A '-05:00' offset from the model was read as
    # if it were UTC and fired five hours early.
    due_at = target.astimezone(timezone.utc).isoformat(timespec="seconds")
    for row in cur.fetchall():
        existing = _parse_due(row["due_at"])
        if existing is None:
            continue
        # Same minute: the model re-drafting the same ask lands on the same
        # timestamp, so time is the strong signal and text disambiguates two
        # genuinely different reminders that happen to share a slot.
        if abs((existing - target).total_seconds()) <= 60 and _similar_reminder_text(row["text"], text):
            conn.close()
            return
    cur.execute(
        f"INSERT INTO reminders (phone, text, due_at, recurrence) VALUES ({PH}, {PH}, {PH}, {PH})",
        (phone, text, due_at, recurrence),
    )
    conn.commit()
    conn.close()


def cancel_reminders(phone: str, text_match: str = None) -> int:
    conn = _conn()
    cur = conn.cursor()
    # recurrence = NULL as well as sent = 1: marking it sent alone would be
    # undone by the next re-arm, and it is also what makes a cancel that lands
    # between claim and re-arm stick (see rearm_reminder).
    if text_match:
        pattern = f"%{text_match.lower().strip()}%"
        cur.execute(
            f"UPDATE reminders SET sent = 1, recurrence = NULL "
            f"WHERE phone = {PH} AND LOWER(text) LIKE {PH} AND sent = 0",
            (phone, pattern),
        )
    else:
        cur.execute(
            f"UPDATE reminders SET sent = 1, recurrence = NULL "
            f"WHERE phone = {PH} AND sent = 0",
            (phone,),
        )
    count = cur.rowcount
    conn.commit()
    conn.close()
    return count


def normalize_due_at_rows() -> int:
    """Rewrite pending reminders into the canonical UTC string. Returns the count.

    `due_at` is TEXT and claim_due_reminders orders it lexicographically, so a
    row stored with a non-UTC offset or without seconds sorts wrongly and fires
    early. save_reminder now normalizes on write; this repairs what was written
    before it did. Idempotent — a row already canonical is skipped, so running
    it from init_db on every boot costs one select once the table is clean.

    A row that will not parse is left alone and logged: it can never be claimed,
    which is worth seeing rather than silently rewriting."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"SELECT id, due_at FROM reminders WHERE sent = 0")
    fixed = 0
    for row in cur.fetchall():
        parsed = _parse_due(row["due_at"])
        if parsed is None:
            print(f"normalize_due_at_rows: reminder {row['id']} has unparseable "
                  f"due_at {row['due_at']!r} — it can never fire")
            continue
        canonical = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        if canonical != row["due_at"]:
            cur.execute(f"UPDATE reminders SET due_at = {PH} WHERE id = {PH}",
                        (canonical, row["id"]))
            fixed += 1
    if fixed:
        conn.commit()
        print(f"normalize_due_at_rows: repaired {fixed} reminder(s)")
    conn.close()
    return fixed


def claim_due_reminders() -> list[dict]:
    """Atomically mark due reminders as sent and return them. Prevents double-sends.

    The `due_at <= now` below is a LEXICOGRAPHIC comparison on a TEXT column, so
    it equals a chronological comparison only while every row holds the same
    shape: `YYYY-MM-DDTHH:MM:SS+00:00`. save_reminder guarantees that on write
    and normalize_due_at_rows repairs the rest; do not write due_at anywhere
    else without going through one of them.

    Widening this predicate and re-filtering in Python is not an option — on
    Postgres this is a single UPDATE ... RETURNING, so a wider window would mark
    not-yet-due reminders as sent."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
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
            RETURNING id, phone, text, due_at, recurrence
            """,
            (now,),
        )
        rows = cur.fetchall()
    else:
        cur.execute(
            "SELECT id, phone, text, due_at, recurrence FROM reminders WHERE due_at <= ? AND sent = 0",
            (now,),
        )
        rows = cur.fetchall()
        if rows:
            ids = [r["id"] for r in rows]
            cur.execute(f"UPDATE reminders SET sent = 1 WHERE id IN ({','.join(['?'] * len(ids))})", ids)
    conn.commit()
    conn.close()
    return [{"id": r["id"], "phone": r["phone"], "text": r["text"],
             "due_at": r["due_at"], "recurrence": r["recurrence"]} for r in rows]


def rearm_reminder(reminder_id: int, next_due_at: str) -> bool:
    """Re-arm a recurring reminder for its next occurrence. Returns False if the
    row is no longer recurring.

    The `recurrence IS NOT NULL` guard is what closes the cancel/re-arm race.
    Claiming and cancelling both set sent = 1, so a cancelled row is
    indistinguishable from a claimed one by `sent` alone — but cancel_reminders
    also nulls recurrence, so a reminder cancelled in the window between claim
    and re-arm stays dead instead of being resurrected."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE reminders SET due_at = {PH}, sent = 0 "
        f"WHERE id = {PH} AND recurrence IS NOT NULL",
        (next_due_at, reminder_id),
    )
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return bool(changed)


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
        "SELECT id, description, cooldown_hours, last_alerted, genre, last_alert_url, last_alert_domain "
        "FROM watches WHERE phone = %s AND active = 1" if _DATABASE_URL
        else "SELECT id, description, cooldown_hours, last_alerted, genre, last_alert_url, last_alert_domain "
             "FROM watches WHERE phone = ? AND active = 1",
        (phone,),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"id": r["id"], "description": r["description"], "cooldown_hours": r["cooldown_hours"],
             "last_alerted": r["last_alerted"], "genre": r["genre"],
             "last_alert_url": r["last_alert_url"], "last_alert_domain": r["last_alert_domain"]}
            for r in rows]


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


def update_watch_alerted(watch_id: int, summary: str, recent_summaries: list[str],
                          url: str | None = None, domain: str | None = None):
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
            daily_alert_date = {PH},
            last_alert_url = {PH},
            last_alert_domain = {PH}
        WHERE id = {PH}""",
        (now, summary, json.dumps(recent_summaries), today, today, url, domain, watch_id),
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


def get_recent_messages_of_kind(phone: str, kind: str, limit: int = 4) -> list[str]:
    """The last `limit` assistant messages of one kind, oldest first.

    Lets a sender compare against its OWN prior sends rather than against
    whatever happened to be in the conversation. Returns [] for a user whose
    history predates the kind column, and callers must handle that."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT content FROM messages WHERE phone = {PH} AND role = 'assistant' "
        f"AND kind = {PH} ORDER BY created_at DESC LIMIT {PH}",
        (phone, kind, limit),
    )
    rows = list(reversed(cur.fetchall()))
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
        f"last_seen_url, last_seen_merchant, cooldown_hours, last_alerted "
        f"FROM price_watches WHERE phone = {PH} AND active = 1",
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
            "last_seen_url": r["last_seen_url"],
            "last_seen_merchant": r["last_seen_merchant"],
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
    """Record a fired alert. Also RE-BASELINES to the alerted price: the user has
    now been told about this level, so the next alert must clear the drop bar
    again from here rather than re-reporting the same discount forever. Without
    this the baseline is written once at creation and never moves, so a price
    that settles below the bar keeps qualifying on every tick — bounded only by
    the daily cap, which resets each day. That stayed hidden while the bar was
    15% (rare, usually transient); at a 5% bar it would surface as a daily
    repeat. _is_duplicate_subject cannot cover it — its window is 6h and the
    price-watch cadence is 12h."""
    now = datetime.now(timezone.utc).isoformat()
    today = datetime.now(timezone.utc).date().isoformat()
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"""UPDATE price_watches SET
            last_alerted = {PH},
            last_alert_summary = {PH},
            baseline_price = {PH},
            last_seen_price = {PH},
            last_seen_url = {PH},
            last_seen_merchant = {PH},
            daily_alert_count = CASE WHEN daily_alert_date = {PH} THEN daily_alert_count + 1 ELSE 1 END,
            daily_alert_date = {PH}
        WHERE id = {PH}""",
        (now, summary, price, price, url, merchant, today, today, watch_id),
    )
    conn.commit()
    conn.close()


def save_artifact(token: str, kind: str, body: bytes, ttl_hours: int = 48) -> None:
    """Store a rendered artifact (a card PNG) for public fetch.

    Twilio fetches MMS media, and the recipient's phone fetches the og:image, so
    these URLs cannot be authenticated. The token is the only protection, hence
    128 bits of CSPRNG in artifacts.new_token, plus a TTL."""
    expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    conn = _conn()
    cur = conn.cursor()
    payload = psycopg2.Binary(body) if _DATABASE_URL else body
    cur.execute(
        f"INSERT INTO artifacts (token, kind, body, expires_at) VALUES ({PH}, {PH}, {PH}, {PH}) "
        f"ON CONFLICT(token) DO UPDATE SET body = EXCLUDED.body, expires_at = EXCLUDED.expires_at",
        (token, kind, payload, expires),
    )
    conn.commit()
    conn.close()


def get_artifact(token: str) -> tuple[str, bytes] | None:
    """(kind, body) if the token is live, else None. Expired reads as missing."""
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"SELECT kind, body, expires_at FROM artifacts WHERE token = {PH}", (token,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    expires = row["expires_at"]
    if isinstance(expires, str):
        expires = datetime.fromisoformat(expires)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        return None
    return row["kind"], bytes(row["body"])


# --- game alert state --------------------------------------------------------

def get_game_alert(phone: str, game_id: str) -> dict | None:
    """What this user was last told about this game."""
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute(f"SELECT * FROM game_alerts WHERE phone = {PH} AND game_id = {PH}",
                    (phone, str(game_id)))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def record_game_alert(phone: str, game_id: str, home_score: int, away_score: int,
                      leader: str | None, state: str, sent: bool) -> None:
    """Store what they now know. `sent` distinguishes a silent baseline write
    from an actual text, because only texts count against the per-game cap."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn(); cur = conn.cursor()
    try:
        bump = 1 if sent else 0
        if _DATABASE_URL:
            cur.execute(
                f"INSERT INTO game_alerts (phone, game_id, home_score, away_score, leader, "
                f"state, alert_count, updated_at) VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH}) "
                f"ON CONFLICT (phone, game_id) DO UPDATE SET home_score = EXCLUDED.home_score, "
                f"away_score = EXCLUDED.away_score, leader = EXCLUDED.leader, "
                f"state = EXCLUDED.state, updated_at = EXCLUDED.updated_at, "
                f"alert_count = game_alerts.alert_count + {PH}",
                (phone, str(game_id), home_score, away_score, leader, state, bump, now, bump))
        else:
            cur.execute(
                f"INSERT INTO game_alerts (phone, game_id, home_score, away_score, leader, "
                f"state, alert_count, updated_at) VALUES ({PH},{PH},{PH},{PH},{PH},{PH},{PH},{PH}) "
                f"ON CONFLICT (phone, game_id) DO UPDATE SET home_score = excluded.home_score, "
                f"away_score = excluded.away_score, leader = excluded.leader, "
                f"state = excluded.state, updated_at = excluded.updated_at, "
                f"alert_count = game_alerts.alert_count + {PH}",
                (phone, str(game_id), home_score, away_score, leader, state, bump, now, bump))
        conn.commit()
    finally:
        conn.close()


# --- forecast accuracy audit -------------------------------------------------
# Two sources disagree by up to 15F across LA's microclimates and neither wins
# everywhere: NWS is the best number in Culver City (+1.7F against actuals) and
# the worst in Woodland Hills (+5 to +11F). Choosing between them from two
# cities and four days would be fitting a rule to anecdotes, so this records
# both against reality and lets the answer come from data.

def record_forecast(city: str, target_date: str, source: str, forecast_high: float) -> None:
    """Log today's forecast. Idempotent — a second run for the same day is a
    no-op rather than a duplicate row."""
    conn = _conn(); cur = conn.cursor()
    try:
        if _DATABASE_URL:
            cur.execute(f"INSERT INTO forecast_audit (city, target_date, source, forecast_high) "
                        f"VALUES ({PH}, {PH}, {PH}, {PH}) "
                        f"ON CONFLICT (city, target_date, source) DO NOTHING",
                        (city, target_date, source, forecast_high))
        else:
            cur.execute(f"INSERT OR IGNORE INTO forecast_audit (city, target_date, source, forecast_high) "
                        f"VALUES ({PH}, {PH}, {PH}, {PH})",
                        (city, target_date, source, forecast_high))
        conn.commit()
    finally:
        conn.close()


def record_actual(city: str, target_date: str, actual_high: float) -> int:
    """Fill in what actually happened for every source logged for that day."""
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute(f"UPDATE forecast_audit SET actual_high = {PH} "
                    f"WHERE city = {PH} AND target_date = {PH} AND actual_high IS NULL",
                    (actual_high, city, target_date))
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def pending_actuals(before_date: str) -> list[tuple[str, str]]:
    """(city, target_date) pairs still missing an actual, oldest first."""
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute(f"SELECT DISTINCT city, target_date FROM forecast_audit "
                    f"WHERE actual_high IS NULL AND target_date < {PH} "
                    f"ORDER BY target_date", (before_date,))
        return [(r["city"], r["target_date"]) for r in cur.fetchall()]
    finally:
        conn.close()


def forecast_scores(days: int = 30) -> list[dict]:
    """Mean signed error and sample count per city and source. Positive means
    the source forecasts hotter than reality."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute(f"SELECT city, source, COUNT(*) AS n, "
                    f"AVG(forecast_high - actual_high) AS bias, "
                    f"AVG(ABS(forecast_high - actual_high)) AS mae "
                    f"FROM forecast_audit WHERE actual_high IS NOT NULL AND target_date >= {PH} "
                    f"GROUP BY city, source ORDER BY city, source", (cutoff,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# --- flight watches ----------------------------------------------------------
# Deliberately thinner than price_watches: a flight watch is checked once a day
# (SerpAPI is the only paid input and the account is on a 250/month plan), so
# there is no per-watch cooldown to track — the cadence IS the cooldown.

FLIGHT_WATCH_MAX = 3          # per user; each active watch costs ~30 searches/month


def save_flight_watch(phone: str, origin: str, destination: str, outbound_date: str,
                      return_date: str | None = None, target_price: float | None = None) -> int | None:
    """Create a watch, or None if the user is already at FLIGHT_WATCH_MAX or has
    this exact route and date pair already."""
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) AS n FROM flight_watches WHERE phone = {PH} AND active = 1", (phone,))
        if (cur.fetchone()["n"] or 0) >= FLIGHT_WATCH_MAX:
            return None
        cur.execute(
            f"SELECT id FROM flight_watches WHERE phone = {PH} AND active = 1 AND origin = {PH} "
            f"AND destination = {PH} AND outbound_date = {PH}",
            (phone, origin.upper(), destination.upper(), outbound_date))
        if cur.fetchone():
            return None
        cur.execute(
            f"INSERT INTO flight_watches (phone, origin, destination, outbound_date, return_date, target_price) "
            f"VALUES ({PH}, {PH}, {PH}, {PH}, {PH}, {PH})",
            (phone, origin.upper(), destination.upper(), outbound_date, return_date, target_price))
        conn.commit()
        return 1
    finally:
        conn.close()


def get_active_flight_watches() -> list[dict]:
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM flight_watches WHERE active = 1 ORDER BY id")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def get_user_flight_watches(phone: str) -> list[dict]:
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute(f"SELECT * FROM flight_watches WHERE phone = {PH} AND active = 1 ORDER BY id", (phone,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def cancel_flight_watches(phone: str, text_match: str | None = None) -> int:
    conn = _conn(); cur = conn.cursor()
    try:
        if text_match:
            like = f"%{text_match.upper()}%"
            cur.execute(
                f"UPDATE flight_watches SET active = 0 WHERE phone = {PH} AND active = 1 "
                f"AND (UPPER(origin) LIKE {PH} OR UPPER(destination) LIKE {PH})", (phone, like, like))
        else:
            cur.execute(f"UPDATE flight_watches SET active = 0 WHERE phone = {PH} AND active = 1", (phone,))
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def update_flight_watch_price(watch_id: int, price: float, baseline: bool = False,
                              alerted: bool = False) -> None:
    from datetime import datetime, timezone
    sets = [f"last_seen_price = {PH}"]
    params: list = [price]
    if baseline:
        sets.append(f"baseline_price = {PH}"); params.append(price)
    if alerted:
        sets.append(f"last_alerted = {PH}"); params.append(datetime.now(timezone.utc).isoformat())
        # Re-baseline on every alert, exactly as the product watch does, so the
        # next alert measures from what the user was last told.
        sets.append(f"baseline_price = {PH}"); params.append(price)
    params.append(watch_id)
    conn = _conn(); cur = conn.cursor()
    try:
        cur.execute(f"UPDATE flight_watches SET {', '.join(sets)} WHERE id = {PH}", params)
        conn.commit()
    finally:
        conn.close()
