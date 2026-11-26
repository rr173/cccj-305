"""SQLite schema and low-level helpers.

The app deliberately keeps the data model explicit (no ORM) so that the
eligibility rules are visible in SQL.  Every state change that can affect a
claimant's eligibility runs in a single ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "lost_found.db"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS staff (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claimants (
    id            TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL
);

-- Monotonic event sequence.  Used to decide whether an answer was submitted
-- before a detail was first published (robust against equal clock readings).
CREATE TABLE IF NOT EXISTS seq_counter (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id                 TEXT PRIMARY KEY,
    public_code        TEXT NOT NULL UNIQUE,
    title              TEXT NOT NULL,
    public_description TEXT NOT NULL,
    found_at           TEXT NOT NULL,
    found_location     TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'held',    -- held | delivered
    created_by         TEXT NOT NULL REFERENCES staff(id),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS details (
    id              TEXT PRIMARY KEY,
    item_id         TEXT NOT NULL REFERENCES items(id),
    code            TEXT NOT NULL,
    label           TEXT NOT NULL,
    answer          TEXT NOT NULL,
    is_public       INTEGER NOT NULL DEFAULT 0,
    published_seq   INTEGER,          -- first publication event, immutable
    published_at    TEXT,
    correction_epoch INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE (item_id, code)
);

-- One row per answer revision; revisions are never deleted.  "Latest wins"
-- for eligibility, but old revisions still matter for audit and delivery.
CREATE TABLE IF NOT EXISTS answers (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    claimant_id          TEXT NOT NULL REFERENCES claimants(id),
    detail_id            TEXT NOT NULL REFERENCES details(id),
    text                 TEXT NOT NULL,
    submitted_at         TEXT NOT NULL,
    seq                  INTEGER NOT NULL,
    epoch_at_submission  INTEGER NOT NULL,
    expected_at_submission TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_answers_detail ON answers(detail_id, claimant_id, id);
CREATE INDEX IF NOT EXISTS idx_answers_claimant ON answers(claimant_id, id);

CREATE TABLE IF NOT EXISTS detail_corrections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    detail_id   TEXT NOT NULL REFERENCES details(id),
    old_answer  TEXT NOT NULL,
    new_answer  TEXT NOT NULL,
    old_epoch   INTEGER NOT NULL,
    new_epoch   INTEGER NOT NULL,
    corrected_by TEXT NOT NULL REFERENCES staff(id),
    at          TEXT NOT NULL,
    seq         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id      TEXT NOT NULL REFERENCES items(id),
    claimant_id  TEXT NOT NULL REFERENCES claimants(id),
    status       TEXT NOT NULL,        -- active | cancelled | fulfilled
    created_by   TEXT NOT NULL REFERENCES staff(id),
    created_at   TEXT NOT NULL,
    ended_at     TEXT,
    ended_reason TEXT,                 -- staff_cancelled | eligibility_lost
    seq          INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking_per_item
    ON bookings(item_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS deliveries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id             TEXT NOT NULL UNIQUE REFERENCES items(id),
    claimant_id         TEXT NOT NULL REFERENCES claimants(id),
    booked_by           TEXT NOT NULL REFERENCES staff(id),
    delivered_by        TEXT NOT NULL REFERENCES staff(id),
    delivered_at        TEXT NOT NULL,
    private_clue_count  INTEGER NOT NULL,
    eligibility_note    TEXT NOT NULL,
    seq                 INTEGER NOT NULL
);

-- Frozen snapshot of the evidence at delivery.  Never updated afterwards.
CREATE TABLE IF NOT EXISTS delivery_evidence (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id         INTEGER NOT NULL REFERENCES deliveries(id),
    detail_id           TEXT NOT NULL REFERENCES details(id),
    detail_code         TEXT NOT NULL,
    detail_label        TEXT NOT NULL,
    answer_at_delivery  TEXT NOT NULL,
    answer_at_submission TEXT NOT NULL,
    submitted_text      TEXT NOT NULL,
    counted_as_private  INTEGER NOT NULL,
    was_public_at_delivery INTEGER NOT NULL,
    epoch_at_submission INTEGER NOT NULL,
    submitted_seq       INTEGER NOT NULL,
    submitted_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL,
    action  TEXT NOT NULL,
    detail  TEXT NOT NULL
);
"""


def db_path_from_env() -> Path:
    return Path(os.environ.get("LOF_DB", DEFAULT_DB))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO seq_counter(id, value) VALUES (1, 0)")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def next_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "UPDATE seq_counter SET value = value + 1 WHERE id = 1 RETURNING value"
    ).fetchone()
    assert row is not None
    return int(row["value"])


def audit(conn: sqlite3.Connection, actor: str, action: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO audit_log(at, actor, action, detail) VALUES (?,?,?,?)",
        (now_iso(), actor, action, detail),
    )


def norm(text: str) -> str:
    """Comparison form: remove leading/trailing whitespace only."""
    return text.strip()
