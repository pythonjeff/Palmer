import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent / "palmer.db"


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                phone TEXT PRIMARY KEY,
                profile TEXT NOT NULL DEFAULT '{}'
            )
        """)


def get_history(phone: str, limit: int = 40) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE phone = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (phone, limit),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def save_message(phone: str, role: str, content: str):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO messages (phone, role, content) VALUES (?, ?, ?)",
            (phone, role, content),
        )


def get_profile(phone: str) -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT profile FROM users WHERE phone = ?", (phone,)
        ).fetchone()
    return json.loads(row["profile"]) if row else {}


def upsert_profile(phone: str, updates: dict):
    profile = get_profile(phone)
    profile.update(updates)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO users (phone, profile) VALUES (?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET profile = excluded.profile",
            (phone, json.dumps(profile)),
        )
