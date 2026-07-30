import os
import json
from datetime import datetime, timezone
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
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
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
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO messages (phone, role, content) VALUES ({PH}, {PH}, {PH})",
        (phone, role, content),
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
    cur.execute("SELECT DISTINCT phone FROM messages")
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
    cur.execute("SELECT id, phone, description, queries, cooldown_hours, last_alerted FROM watches WHERE active = 1")
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
        }
        for r in rows
    ]


def get_user_watches(phone: str) -> list[dict]:
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, description, cooldown_hours, last_alerted FROM watches WHERE phone = %s AND active = 1" if _DATABASE_URL
        else "SELECT id, description, cooldown_hours, last_alerted FROM watches WHERE phone = ? AND active = 1",
        (phone,),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"id": r["id"], "description": r["description"], "cooldown_hours": r["cooldown_hours"], "last_alerted": r["last_alerted"]} for r in rows]


def update_watch_alerted(watch_id: int):
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE watches SET last_alerted = {PH} WHERE id = {PH}", (now, watch_id))
    conn.commit()
    conn.close()


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
