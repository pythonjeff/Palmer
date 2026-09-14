# Db

The database layer. Governs `db.py`.

Each section below was written when the behaviour it describes was decided, usually after a production incident, and is kept verbatim. Read the one for a module before changing that module.


## DB layer is dual-backend

`db.py` transparently switches between Postgres (when `DATABASE_URL` is set) and SQLite (`palmer.db` in repo root) using a `PH` placeholder (`%s` vs `?`) — every query in `db.py` uses `PH` and `_conn()`. Any new DB code must follow this pattern; do not hard-code `%s` or `?`.

Reminder delivery uses `FOR UPDATE SKIP LOCKED` on Postgres in `claim_due_reminders()` so multiple scheduler ticks are safe against double-sends. The SQLite branch does a plain select-then-update (fine for single-process local dev).

Schema is created lazily in `init_db()`, called at import time from `agent.py`. New columns on existing tables must be added to the `new_cols` migration list — Postgres uses `ADD COLUMN IF NOT EXISTS`, SQLite catches the duplicate-column exception.

## DB access patterns

- `get_all_profiles()` returns every `(phone, profile)` in ONE query. The scheduler jobs use it. Do not loop over phones calling `get_profile(phone)` per user — `_conn()` opens a fresh connection per call, so that is N+1 per tick.
- `upsert_profile()` does its read and write on one connection, and takes a row lock on Postgres. It used to be two connections with an unsynchronised gap, so concurrent writers could drop each other's fields.
- Pass profiles down rather than re-reading them. The reaction path once cost five `get_profile` calls for a single inbound tapback.
