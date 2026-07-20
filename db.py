import os
import json
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
    conn.commit()
    conn.close()


def get_history(phone: str, limit: int = 15) -> list[dict]:
    conn = _conn()
    cur = conn.cursor()
    cur.execute(
        f"SELECT role, content FROM messages WHERE phone = {PH} ORDER BY created_at DESC LIMIT {PH}",
        (phone, limit),
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
