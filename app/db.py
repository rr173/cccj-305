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

MAIN_COUNTER_ID = "cnt_main"
MAIN_COUNTER_CODE = "MAIN"
MAIN_COUNTER_NAME = "总台"

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Lost-and-found counters.  Every member of staff belongs to exactly one
-- counter; the main desk is the upgrade fallback for legacy staff/items.
CREATE TABLE IF NOT EXISTS counters (
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    is_main     INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS staff (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    counter_id    TEXT NOT NULL DEFAULT 'cnt_main' REFERENCES counters(id),
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
    -- Physical custody independent of the claim workflow:
    -- at_counter | in_transit | missing | quarantined.
    custody_state      TEXT NOT NULL DEFAULT 'at_counter',
    -- Counter currently holding the object (at_counter), the finder counter
    -- while quarantined, or the expecting destination when missing.
    -- NULL only while in_transit.
    custody_counter_id TEXT REFERENCES counters(id),
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

-- --------------------------------------------------------------------------
-- Cross-counter transfers ("boxes") and open-box receipts.
--
-- A box has one dispatch record (unique box_no) and at most one receipt,
-- submitted once by its destination counter.  Outbound labels identify a
-- trip by (label_box_no, item_id).  A label scanned inside a different box
-- is a wrong-box finding and drives the two-box reconciliation.
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS boxes (
    box_no                  TEXT PRIMARY KEY,
    sender_counter_id       TEXT NOT NULL REFERENCES counters(id),
    destination_counter_id  TEXT NOT NULL REFERENCES counters(id),
    dispatched_by           TEXT NOT NULL REFERENCES staff(id),
    dispatched_at           TEXT NOT NULL,
    item_count              INTEGER NOT NULL,
    -- Canonical hash of destination + resolved item-id set (order-insensitive);
    -- powers idempotent redelivery detection and content-conflict rejection.
    request_hash            TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'dispatched', -- dispatched | received
    receipt_id              INTEGER,
    seq                     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS box_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    box_no              TEXT NOT NULL REFERENCES boxes(box_no),
    item_id             TEXT NOT NULL REFERENCES items(id),
    ord                 INTEGER NOT NULL DEFAULT 0,
    -- in_transit until the unique receipt processes the line:
    -- arrived = own label scanned; missing = not scanned.
    result              TEXT NOT NULL DEFAULT 'in_transit',
    result_receipt_id   INTEGER,
    processed_at        TEXT,
    UNIQUE (box_no, item_id)
);

-- An item can belong to at most one still-open trip.  Finished lines keep
-- history but release the item for a later transfer from its new counter.
CREATE UNIQUE INDEX IF NOT EXISTS one_open_box_line_per_item
    ON box_items(item_id) WHERE result = 'in_transit';

CREATE TABLE IF NOT EXISTS receipts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    box_no                  TEXT NOT NULL UNIQUE REFERENCES boxes(box_no),
    received_by             TEXT NOT NULL REFERENCES staff(id),
    receiving_counter_id    TEXT NOT NULL REFERENCES counters(id),
    received_at             TEXT NOT NULL,
    scanned_count           INTEGER NOT NULL,
    -- Canonical hash of the scanned (label_box_no, item_id) set.
    request_hash            TEXT NOT NULL,
    seq                     INTEGER NOT NULL
);

-- One immutable row per accepted scanned label.  A label can only be accepted
-- once ever: re-scanning an arrived label is an "old label", and re-reporting
-- a wrong-box label is a duplicate finding.  Reconciliation only updates the
-- find_state/match columns; the original scan fact is never deleted.
CREATE TABLE IF NOT EXISTS receipt_scans (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id               INTEGER NOT NULL REFERENCES receipts(id),
    label_box_no             TEXT NOT NULL,
    item_id                  TEXT NOT NULL REFERENCES items(id),
    scan_kind                TEXT NOT NULL,          -- own | wrong_box
    find_state               TEXT NOT NULL DEFAULT 'arrived',
        -- own labels: arrived
        -- wrong_box labels: open (quarantined) | matched
    matched_at               TEXT,
    matched_missing_receipt_id INTEGER,
    created_at               TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_accepted_scan_per_label
    ON receipt_scans(label_box_no, item_id);
CREATE INDEX IF NOT EXISTS idx_receipt_scans_receipt ON receipt_scans(receipt_id);
CREATE INDEX IF NOT EXISTS idx_receipt_scans_label
    ON receipt_scans(label_box_no, item_id);

-- The reconciliation pair "original box signed it missing" <-> "another box
-- physically found its label".  Preserved after matching.
CREATE TABLE IF NOT EXISTS mismatch_matches (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id                 TEXT NOT NULL REFERENCES items(id),
    missing_box_no          TEXT NOT NULL,
    missing_receipt_id      INTEGER NOT NULL REFERENCES receipts(id),
    find_scan_id            INTEGER NOT NULL UNIQUE REFERENCES receipt_scans(id),
    find_receipt_id         INTEGER NOT NULL REFERENCES receipts(id),
    finder_counter_id       TEXT NOT NULL REFERENCES counters(id),
    matched_at              TEXT NOT NULL,
    UNIQUE (missing_box_no, item_id)
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
    # Fixed-id main desk so legacy rows can be upgraded to a stable counter.
    conn.execute(
        "INSERT OR IGNORE INTO counters(id, code, name, is_main, created_at)"
        " VALUES (?,?,?,1,?)",
        (MAIN_COUNTER_ID, MAIN_COUNTER_CODE, MAIN_COUNTER_NAME, now_iso()),
    )
    migrate(conn)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    """Add transfer columns to databases created before the counter upgrade.

    Legacy staff and every item already in the museum are assigned to the
    main desk.  New databases already contain the columns in ``SCHEMA``.
    """
    staff_cols = _table_columns(conn, "staff")
    if "counter_id" not in staff_cols:
        # SQLite cannot ADD a REFERENCES column with a non-NULL default, so
        # the upgraded column omits the inline FK; application code and the
        # fresh-install schema enforce counter integrity.
        conn.execute(
            "ALTER TABLE staff ADD COLUMN counter_id TEXT NOT NULL DEFAULT 'cnt_main'"
        )
        conn.execute("UPDATE staff SET counter_id = 'cnt_main' WHERE counter_id IS NULL")

    item_cols = _table_columns(conn, "items")
    if "custody_state" not in item_cols:
        conn.execute(
            "ALTER TABLE items ADD COLUMN custody_state TEXT NOT NULL DEFAULT 'at_counter'"
        )
    if "custody_counter_id" not in item_cols:
        conn.execute(
            "ALTER TABLE items ADD COLUMN custody_counter_id TEXT DEFAULT 'cnt_main'"
        )
        # Upgrade rule: items still in the museum belong to the main desk.
        conn.execute(
            "UPDATE items SET custody_counter_id = 'cnt_main' "
            "WHERE custody_counter_id IS NULL"
        )


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
