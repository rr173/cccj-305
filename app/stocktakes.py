"""Counter stocktakes ("盘点") and discrepancy review.

Business rules implemented here
-------------------------------
* A staff member opens a round for **their own** counter.  A counter has at
  most one open round.  Counting never locks the shelves: registration,
  dispatch, receipts and claims keep working while a round is open.
* Every scan carries a client-generated ``request_no``.  Re-sending the same
  number with the same content returns the original result; the same number
  with different content is rejected.  After the round is closed no new scan
  is accepted, but the original results remain retrievable.
* A scan is classified **once**, using the book state at the instant the
  server adopts it:

  - ``seen``        the object is a physical object claimable *at this
                    counter* (held, ``at_counter`` here, not confirmed lost);
  - ``off_counter`` everything else, including unknown codes.  Recording it
                    never moves the object or changes its custody.

* At close time the settlement is frozen from the objects claimable at this
  counter *at that moment*.  Each is ``seen`` only if a ``seen`` scan in this
  round points at the **current continuous on-shelf stay** at this counter.
  A scan taken before the object left, or before it was re-registered /
  returned, does not count.  Objects already confirmed lost and not found are
  not claimable, so they are not listed again as a new shortage.
* Pending items are reviewed by *another* staff member of the same counter
  (never the closer): either "still on the shelf" or "inventory loss".  A loss
  requires that this same continuous stay is still open — once it has ended,
  the old discrepancy can no longer be used to confirm a loss.  Confirming a
  loss cancels still-usable bookings (reason ``inventory_loss``) and blocks
  bookings, check-ins, deliveries and dispatch until the counter registers a
  find.  Check-ins and occupied reception seats already on record are kept.
* A find does **not** restore old bookings.  The object starts a fresh stay
  and ordinary rules apply again.
* Settlement rows are append-only (enforced by SQLite triggers); reviews and
  finds live in their own append-only tables.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import (
    audit,
    end_stay,
    next_seq,
    now_iso,
    norm,
    open_stay,
    start_stay,
    transaction,
)
from .services import api_error, new_id
from .transfers import (
    canonical_hash,
    counter_brief,
    find_item_by_ref,
    staff_brief,
)

ROUND_OPEN = "open"
ROUND_CLOSED = "closed"

SEEN = "seen"
OFF_COUNTER = "off_counter"

PENDING_REVIEW = "pending_review"
CONFIRMED_AT_COUNTER = "confirmed_at_counter"
LOSS_CONFIRMED = "loss_confirmed"

INVENTORY_LOSS = "inventory_loss"


# --------------------------------------------------------------------------
# Lookups / access
# --------------------------------------------------------------------------


def get_round(conn: sqlite3.Connection, round_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM stocktakes WHERE id = ?", (round_id,)
    ).fetchone()
    if row is None:
        raise api_error(404, "stocktake_not_found", "盘点轮次不存在")
    return row


def assert_can_view(conn: sqlite3.Connection, staff: sqlite3.Row, round_row: sqlite3.Row) -> None:
    """Stocktake discrepancies are visible only to the counter's own staff
    and to administrators."""
    if staff["is_admin"]:
        return
    if staff["counter_id"] != round_row["counter_id"]:
        raise api_error(
            403,
            "wrong_counter",
            "盘点差异只向本柜台工作人员和管理员开放",
            that_counter=counter_brief(conn, round_row["counter_id"]),
        )


def assert_counter_member(staff: sqlite3.Row, round_row: sqlite3.Row) -> None:
    """Actions are performed by staff of the round's counter."""
    if staff["counter_id"] != round_row["counter_id"]:
        raise api_error(
            403,
            "wrong_counter",
            "只有盘点柜台的工作人员能办理该轮盘点",
        )


def _scan_by_request_no(
    conn: sqlite3.Connection, round_id: str, request_no: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM stocktake_scans WHERE round_id = ? AND request_no = ?",
        (round_id, request_no),
    ).fetchone()


def _content_hash(item: sqlite3.Row | None, raw_ref: str) -> str:
    if item is not None:
        return canonical_hash({"item": item["id"]})
    return canonical_hash({"unknown": norm(raw_ref)})


# --------------------------------------------------------------------------
# Opening a round
# --------------------------------------------------------------------------


def open_round(conn: sqlite3.Connection, staff: sqlite3.Row) -> dict[str, Any]:
    counter_id = staff["counter_id"]
    with transaction(conn):
        existing = conn.execute(
            "SELECT * FROM stocktakes WHERE counter_id = ? AND status = 'open'",
            (counter_id,),
        ).fetchone()
        if existing is not None:
            raise api_error(
                409,
                "open_round_exists",
                "本柜台已有一轮未结的盘点；请先关轮",
                round_id=existing["id"],
                opened_at=existing["opened_at"],
            )
        round_id = "stk_" + new_id()[:16]
        at = now_iso()
        seq = next_seq(conn)
        conn.execute(
            "INSERT INTO stocktakes(id, counter_id, status, opened_by, opened_at, seq)"
            " VALUES (?,?,'open',?,?,?)",
            (round_id, counter_id, staff["id"], at, seq),
        )
        audit(
            conn,
            f"staff:{staff['id']}",
            "stocktake_opened",
            f"round={round_id} counter={counter_id}",
        )
        return round_view(conn, get_round(conn, round_id))


def list_rounds(
    conn: sqlite3.Connection, staff: sqlite3.Row, counter_ref: str | None = None
) -> dict[str, Any]:
    if counter_ref is not None:
        row = conn.execute(
            "SELECT * FROM counters WHERE id = ? OR code = ?", (counter_ref, counter_ref)
        ).fetchone()
        if row is None:
            raise api_error(404, "counter_not_found", "柜台不存在", counter_ref=counter_ref)
        counter_id = row["id"]
        if not staff["is_admin"] and staff["counter_id"] != counter_id:
            raise api_error(403, "wrong_counter", "只能查看本柜台的盘点记录")
    elif staff["is_admin"]:
        counter_id = None
    else:
        counter_id = staff["counter_id"]

    if counter_id is None:
        rows = conn.execute(
            "SELECT id FROM stocktakes ORDER BY opened_at DESC, rowid DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM stocktakes WHERE counter_id = ?"
            " ORDER BY opened_at DESC, rowid DESC",
            (counter_id,),
        ).fetchall()
    return {"rounds": [round_view(conn, get_round(conn, r["id"])) for r in rows]}


# --------------------------------------------------------------------------
# Scans
# --------------------------------------------------------------------------


def _classify(
    conn: sqlite3.Connection, item: sqlite3.Row | None, counter_id: str
) -> tuple[str, str]:
    """Classify a scan by the book state right now.  Never mutates anything."""
    if item is None:
        return OFF_COUNTER, "unknown_code"
    if item["status"] == "delivered":
        return OFF_COUNTER, "already_delivered"
    if item["status"] != "held":
        return OFF_COUNTER, "not_held"
    if item["inventory_loss_review_id"] is not None:
        return OFF_COUNTER, "confirmed_loss"
    state = item["custody_state"]
    if state != "at_counter":
        reasons = {
            "in_transit": "in_transit",
            "missing": "missing",
            "quarantined": "quarantined",
        }
        return OFF_COUNTER, reasons.get(state, "not_at_counter")
    if item["custody_counter_id"] != counter_id:
        return OFF_COUNTER, "at_other_counter"
    return SEEN, "claimable_at_counter"


def _scan_view(conn: sqlite3.Connection, scan: sqlite3.Row) -> dict[str, Any]:
    item = None
    if scan["item_id"] is not None:
        row = conn.execute(
            "SELECT public_code, title FROM items WHERE id = ?", (scan["item_id"],)
        ).fetchone()
        if row is not None:
            item = {"public_code": row["public_code"], "title": row["title"]}
    return {
        "scan_id": scan["id"],
        "request_no": scan["request_no"],
        "raw_ref": scan["raw_ref"],
        "item_id": scan["item_id"],
        "public_code": None if item is None else item["public_code"],
        "title": None if item is None else item["title"],
        "classification": scan["classification"],
        "reason": scan["reason"],
        "custody_state_at_scan": scan["custody_state_at_scan"],
        "custody_counter_at_scan": counter_brief(
            conn, scan["custody_counter_id_at_scan"]
        ),
        "stay_id": scan["stay_id"],
        "adopted_at": scan["adopted_at"],
        "scanned_by": staff_brief(conn, scan["scanned_by"]),
    }


def submit_scan(
    conn: sqlite3.Connection,
    staff: sqlite3.Row,
    round_id: str,
    request_no: str,
    item_ref: str,
) -> dict[str, Any]:
    request_no = request_no.strip()
    item_ref = item_ref.strip()
    with transaction(conn):
        round_row = get_round(conn, round_id)
        assert_counter_member(staff, round_row)

        item = find_item_by_ref(conn, item_ref) if item_ref else None
        content_hash = _content_hash(item, item_ref)
        prior = _scan_by_request_no(conn, round_id, request_no)
        if prior is not None:
            if prior["content_hash"] != content_hash:
                raise api_error(
                    409,
                    "scan_content_conflict",
                    "该请求号已用于扫描另一件失物，不能覆盖原结果",
                    request_no=request_no,
                    scan_id=prior["id"],
                )
            view = _scan_view(conn, prior)
            view["replayed"] = True
            view["round_closed"] = round_row["status"] == ROUND_CLOSED
            return view

        if round_row["status"] != ROUND_OPEN:
            raise api_error(
                409,
                "round_closed",
                "本轮盘点已关轮，不再接收新的扫描；原扫描结果仍可重取",
                round_id=round_id,
            )

        classification, reason = _classify(conn, item, round_row["counter_id"])
        stay_id = None
        if classification == SEEN and item is not None:
            stay = open_stay(conn, item["id"])
            if stay is None:
                # Defensive: an at_counter item should always have a stay.
                stay_id = start_stay(
                    conn, item["id"], round_row["counter_id"], "migrated"
                )
            else:
                stay_id = int(stay["id"])

        at = now_iso()
        seq = next_seq(conn)
        cur = conn.execute(
            "INSERT INTO stocktake_scans(round_id, request_no, content_hash, raw_ref,"
            " item_id, stay_id, classification, reason, custody_state_at_scan,"
            " custody_counter_id_at_scan, adopted_at, scanned_by, seq)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                round_id,
                request_no,
                content_hash,
                item_ref,
                None if item is None else item["id"],
                stay_id,
                classification,
                reason,
                None if item is None else item["custody_state"],
                None if item is None else item["custody_counter_id"],
                at,
                staff["id"],
                seq,
            ),
        )
        scan = conn.execute(
            "SELECT * FROM stocktake_scans WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        audit(
            conn,
            f"staff:{staff['id']}",
            "stocktake_scanned",
            f"round={round_id} request_no={request_no} ref={item_ref}"
            f" classification={classification} reason={reason}",
        )
        view = _scan_view(conn, scan)
        view["replayed"] = False
        view["round_closed"] = False
        return view


def get_scan_result(
    conn: sqlite3.Connection, staff: sqlite3.Row, round_id: str, request_no: str
) -> dict[str, Any]:
    round_row = get_round(conn, round_id)
    assert_can_view(conn, staff, round_row)
    scan = _scan_by_request_no(conn, round_id, request_no.strip())
    if scan is None:
        raise api_error(
            404, "scan_not_found", "该请求号在本轮没有扫描记录", request_no=request_no
        )
    view = _scan_view(conn, scan)
    view["replayed"] = True
    view["round_closed"] = round_row["status"] == ROUND_CLOSED
    return view


def list_scans(
    conn: sqlite3.Connection, staff: sqlite3.Row, round_id: str
) -> dict[str, Any]:
    round_row = get_round(conn, round_id)
    assert_can_view(conn, staff, round_row)
    rows = conn.execute(
        "SELECT * FROM stocktake_scans WHERE round_id = ? ORDER BY id", (round_id,)
    ).fetchall()
    return {
        "round_id": round_id,
        "scans": [_scan_view(conn, r) for r in rows],
        "count": len(rows),
    }


# --------------------------------------------------------------------------
# Closing and settlement
# --------------------------------------------------------------------------


def close_round(
    conn: sqlite3.Connection, staff: sqlite3.Row, round_id: str
) -> dict[str, Any]:
    with transaction(conn):
        round_row = get_round(conn, round_id)
        assert_counter_member(staff, round_row)
        if round_row["status"] == ROUND_CLOSED:
            view = settlement_view(conn, round_row)
            view["already_closed"] = True
            return view

        counter_id = round_row["counter_id"]
        # Claimable at this counter *right now*: held, physically here, and
        # not blocked by a confirmed, un-recovered inventory loss.
        claimable = conn.execute(
            "SELECT * FROM items"
            " WHERE status = 'held' AND custody_state = 'at_counter'"
            "   AND custody_counter_id = ? AND inventory_loss_review_id IS NULL"
            " ORDER BY public_code",
            (counter_id,),
        ).fetchall()

        at = now_iso()
        seen_count = 0
        pending_count = 0
        for item in claimable:
            stay = open_stay(conn, item["id"])
            if stay is None:
                stay_id = start_stay(conn, item["id"], counter_id, "migrated", at)
            else:
                stay_id = int(stay["id"])

            first_seen = conn.execute(
                "SELECT id, adopted_at FROM stocktake_scans"
                " WHERE round_id = ? AND item_id = ? AND classification = ?"
                "   AND stay_id = ?"
                " ORDER BY id LIMIT 1",
                (round_id, item["id"], SEEN, stay_id),
            ).fetchone()
            seen_n = conn.execute(
                "SELECT COUNT(*) AS n FROM stocktake_scans"
                " WHERE round_id = ? AND item_id = ? AND classification = ?"
                "   AND stay_id = ?",
                (round_id, item["id"], SEEN, stay_id),
            ).fetchone()["n"]
            classification = PENDING_REVIEW if first_seen is None else SEEN
            if classification == SEEN:
                seen_count += 1
            else:
                pending_count += 1
            conn.execute(
                "INSERT INTO stocktake_settlement_items(round_id, item_id, stay_id,"
                " public_code, title, classification, first_seen_at,"
                " first_seen_scan_id, seen_scan_count, settled_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    round_id,
                    item["id"],
                    stay_id,
                    item["public_code"],
                    item["title"],
                    classification,
                    None if first_seen is None else first_seen["adopted_at"],
                    None if first_seen is None else first_seen["id"],
                    int(seen_n),
                    at,
                ),
            )

        off_count = conn.execute(
            "SELECT COUNT(*) AS n FROM stocktake_scans"
            " WHERE round_id = ? AND classification = ?",
            (round_id, OFF_COUNTER),
        ).fetchone()["n"]

        conn.execute(
            "UPDATE stocktakes SET status = 'closed', closed_by = ?, closed_at = ?,"
            " claimable_count = ?, seen_count = ?, pending_count = ?,"
            " off_counter_scan_count = ? WHERE id = ?",
            (
                staff["id"],
                at,
                len(claimable),
                seen_count,
                pending_count,
                int(off_count),
                round_id,
            ),
        )
        audit(
            conn,
            f"staff:{staff['id']}",
            "stocktake_closed",
            f"round={round_id} counter={counter_id} claimable={len(claimable)}"
            f" seen={seen_count} pending={pending_count} off_counter={off_count}",
        )
        view = settlement_view(conn, get_round(conn, round_id))
        view["already_closed"] = False
        return view


def _review_view(conn: sqlite3.Connection, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "review_id": row["id"],
        "outcome": row["outcome"],
        "reviewer": staff_brief(conn, row["reviewer_id"]),
        "reviewed_at": row["reviewed_at"],
        "note": row["note"],
    }


def _find_view(conn: sqlite3.Connection, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "find_id": row["id"],
        "found_by": staff_brief(conn, row["found_by"]),
        "found_at": row["found_at"],
        "counter": counter_brief(conn, row["counter_id"]),
        "note": row["note"],
    }


def _settlement_item_view(
    conn: sqlite3.Connection, row: sqlite3.Row, counter_id: str
) -> dict[str, Any]:
    review = conn.execute(
        "SELECT * FROM stocktake_reviews WHERE settlement_item_id = ?", (row["id"],)
    ).fetchone()
    find = conn.execute(
        "SELECT * FROM stocktake_finds WHERE settlement_item_id = ?", (row["id"],)
    ).fetchone()

    item = conn.execute(
        "SELECT * FROM items WHERE id = ?", (row["item_id"],)
    ).fetchone()
    stay = open_stay(conn, row["item_id"])
    current_stay_id = None if stay is None else int(stay["id"])
    same_stay_open = current_stay_id == int(row["stay_id"])

    # Only a still-open, unchanged continuous stay can carry a review.
    if review is None and row["classification"] == SEEN:
        can_confirm = False
        can_confirm_loss = False
        blocked_reason = "already_seen"
    elif review is None:
        if same_stay_open and item is not None:
            can_confirm = (
                item["status"] == "held"
                and item["custody_state"] == "at_counter"
                and item["custody_counter_id"] == counter_id
                and item["inventory_loss_review_id"] is None
            )
        else:
            can_confirm = False
        can_confirm_loss = can_confirm
        blocked_reason = (
            None
            if can_confirm
            else (
                "stay_ended_after_settlement"
                if not same_stay_open
                else "item_not_claimable_at_counter"
            )
        )
    else:
        can_confirm = False
        can_confirm_loss = False
        blocked_reason = "already_reviewed"

    can_register_find = (
        review is not None
        and review["outcome"] == LOSS_CONFIRMED
        and find is None
        and item is not None
        and item["status"] != "delivered"
        and item["inventory_loss_review_id"] is not None
    )

    return {
        "settlement_item_id": row["id"],
        "item_id": row["item_id"],
        "public_code": row["public_code"],
        "title": row["title"],
        "classification": row["classification"],
        "seen": row["classification"] == SEEN,
        "first_seen_at": row["first_seen_at"],
        "seen_scan_count": row["seen_scan_count"],
        "settled_stay_id": row["stay_id"],
        "review": _review_view(conn, review),
        "find": _find_view(conn, find),
        "review_permitted": {
            "confirm_at_counter": can_confirm,
            "confirm_loss": can_confirm_loss,
            "blocked_reason": blocked_reason,
            "already_reviewed": review is not None,
            "register_find": can_register_find,
        },
    }


def settlement_view(conn: sqlite3.Connection, round_row: sqlite3.Row) -> dict[str, Any]:
    items = conn.execute(
        "SELECT * FROM stocktake_settlement_items WHERE round_id = ? ORDER BY id",
        (round_row["id"],),
    ).fetchall()
    rows = [
        _settlement_item_view(conn, r, round_row["counter_id"]) for r in items
    ]
    off_scans = conn.execute(
        "SELECT * FROM stocktake_scans WHERE round_id = ? AND classification = ?"
        " ORDER BY id",
        (round_row["id"], OFF_COUNTER),
    ).fetchall()
    return {
        "round_id": round_row["id"],
        "counter": counter_brief(conn, round_row["counter_id"]),
        "status": round_row["status"],
        "opened_at": round_row["opened_at"],
        "closed_at": round_row["closed_at"],
        "closed_by": staff_brief(conn, round_row["closed_by"]),
        "sealed": round_row["status"] == ROUND_CLOSED,
        "counts": {
            "claimable": round_row["claimable_count"],
            "seen": round_row["seen_count"],
            "pending_review": round_row["pending_count"],
            "off_counter_scans": round_row["off_counter_scan_count"],
        },
        "seen_items": [r for r in rows if r["classification"] == SEEN],
        "pending_review_items": [
            r for r in rows if r["classification"] == PENDING_REVIEW
        ],
        "items": rows,
        "off_counter_scans": [_scan_view(conn, s) for s in off_scans],
        "note": (
            "结算清单在关轮时封存：后续调拨或复核不会改写当时见到/待复核的结论；"
            "盘外扫描单独保留。"
        ),
    }


def get_settlement(
    conn: sqlite3.Connection, staff: sqlite3.Row, round_id: str
) -> dict[str, Any]:
    round_row = get_round(conn, round_id)
    assert_can_view(conn, staff, round_row)
    if round_row["status"] != ROUND_CLOSED:
        raise api_error(
            409,
            "round_not_closed",
            "本轮盘点尚未关轮，暂无封存的结算清单",
            round_id=round_id,
        )
    return settlement_view(conn, round_row)


# --------------------------------------------------------------------------
# Review and recovery
# --------------------------------------------------------------------------


def _settlement_item_by_ref(
    conn: sqlite3.Connection, round_id: str, item_ref: str
) -> sqlite3.Row:
    ref = item_ref.strip()
    row = conn.execute(
        "SELECT s.* FROM stocktake_settlement_items s"
        " WHERE s.round_id = ? AND (s.item_id = ? OR s.public_code = ?)",
        (round_id, ref, ref),
    ).fetchone()
    if row is None:
        raise api_error(
            404,
            "settlement_item_not_found",
            "该失物不在本轮结算清单中",
            round_id=round_id,
            item_ref=item_ref,
        )
    return row


def review_settlement_item(
    conn: sqlite3.Connection,
    staff: sqlite3.Row,
    round_id: str,
    item_ref: str,
    outcome: str,
    note: str,
) -> dict[str, Any]:
    """Second staff member of the counter verifies the object or signs a loss."""
    with transaction(conn):
        round_row = get_round(conn, round_id)
        assert_counter_member(staff, round_row)
        if round_row["status"] != ROUND_CLOSED:
            raise api_error(409, "round_not_closed", "关轮后才能复核差异")
        if staff["id"] == round_row["closed_by"]:
            raise api_error(
                403,
                "reviewer_is_closer",
                "待复核项须由本柜台另一位工作人员核实，关轮人不能复核自己关的轮",
            )
        settlement_item = _settlement_item_by_ref(conn, round_id, item_ref)
        if settlement_item["classification"] != PENDING_REVIEW:
            raise api_error(
                409,
                "item_already_seen",
                "该失物在本轮已记为见到，无需复核",
                item_id=settlement_item["item_id"],
            )
        existing = conn.execute(
            "SELECT * FROM stocktake_reviews WHERE settlement_item_id = ?",
            (settlement_item["id"],),
        ).fetchone()
        if existing is not None:
            raise api_error(
                409,
                "already_reviewed",
                "该待复核项已有复核结论，不能重复复核",
                review=_review_view(conn, existing),
            )

        item = conn.execute(
            "SELECT * FROM items WHERE id = ?", (settlement_item["item_id"],)
        ).fetchone()
        assert item is not None
        stay = open_stay(conn, item["id"])
        same_stay_open = stay is not None and int(stay["id"]) == int(
            settlement_item["stay_id"]
        )
        if not same_stay_open:
            raise api_error(
                409,
                "stay_ended_after_settlement",
                "结算后该件这一次在柜已经结束（离柜/入箱/已交付或已终结），"
                "原差异不能再据以确认盘亏或见到",
                item_id=item["id"],
            )
        if item["status"] != "held" or item["custody_state"] != "at_counter" or (
            item["custody_counter_id"] != round_row["counter_id"]
        ):
            raise api_error(
                409,
                "item_not_claimable_at_counter",
                "该失物已不能在本柜台办理领取，原差异不能再据以复核",
                item_id=item["id"],
            )
        if item["inventory_loss_review_id"] is not None:
            raise api_error(
                409,
                "already_confirmed_loss",
                "该失物已确认盘亏，不能再次复核；如找回请登记找回",
            )

        at = now_iso()
        seq = next_seq(conn)
        cur = conn.execute(
            "INSERT INTO stocktake_reviews(round_id, settlement_item_id, item_id,"
            " outcome, reviewer_id, reviewed_at, note, seq)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                round_id,
                settlement_item["id"],
                item["id"],
                outcome,
                staff["id"],
                at,
                note.strip(),
                seq,
            ),
        )
        review_id = int(cur.lastrowid)

        cancelled: list[int] = []
        if outcome == LOSS_CONFIRMED:
            # Cancel only bookings that are still usable; an appointment whose
            # slot already ended is expired, not an inventory-loss cancellation.
            from . import scheduling

            scheduling.sweep_expired_bookings(conn)
            now = now_iso()
            for booking in conn.execute(
                "SELECT id FROM bookings WHERE item_id = ? AND status = 'active'",
                (item["id"],),
            ).fetchall():
                conn.execute(
                    "UPDATE bookings SET status = 'cancelled', ended_at = ?,"
                    " ended_reason = ? WHERE id = ?",
                    (now, INVENTORY_LOSS, booking["id"]),
                )
                audit(
                    conn,
                    f"staff:{staff['id']}",
                    "booking_cancelled",
                    f"booking={booking['id']} reason={INVENTORY_LOSS}"
                    f" review={review_id}",
                )
                cancelled.append(int(booking["id"]))
            conn.execute(
                "UPDATE items SET inventory_loss_review_id = ?, inventory_loss_at = ?,"
                " updated_at = ? WHERE id = ?",
                (review_id, at, at, item["id"]),
            )
            end_stay(conn, item["id"], INVENTORY_LOSS, at)

        audit(
            conn,
            f"staff:{staff['id']}",
            "stocktake_reviewed",
            f"round={round_id} item={item['id']} outcome={outcome}"
            f" review={review_id} cancelled={cancelled}",
        )
        view = _settlement_item_view(
            conn,
            conn.execute(
                "SELECT * FROM stocktake_settlement_items WHERE id = ?",
                (settlement_item["id"],),
            ).fetchone(),
            round_row["counter_id"],
        )
        return {
            "round_id": round_id,
            "item_id": item["id"],
            "public_code": item["public_code"],
            "outcome": outcome,
            "review_id": review_id,
            "reviewed_at": at,
            "cancelled_booking_ids": cancelled,
            "settlement_item": view,
            "note": (
                "确认盘亏后：仍可办理的预约已取消，禁止再次预约、报到、交付或发箱；"
                "已发生的报到与接待占用保留。找回后按原规则重新办理，不恢复旧预约。"
                if outcome == LOSS_CONFIRMED
                else "已核实实物仍在柜，结论写入结算清单的复核记录。"
            ),        }


def register_find(
    conn: sqlite3.Connection,
    staff: sqlite3.Row,
    round_id: str,
    item_ref: str,
    note: str,
) -> dict[str, Any]:
    """The counter registers that a confirmed-lost object was found."""
    with transaction(conn):
        round_row = get_round(conn, round_id)
        assert_counter_member(staff, round_row)
        if round_row["status"] != ROUND_CLOSED:
            raise api_error(409, "round_not_closed", "关轮后才能登记找回")
        settlement_item = _settlement_item_by_ref(conn, round_id, item_ref)
        review = conn.execute(
            "SELECT * FROM stocktake_reviews WHERE settlement_item_id = ?",
            (settlement_item["id"],),
        ).fetchone()
        if review is None or review["outcome"] != LOSS_CONFIRMED:
            raise api_error(
                409,
                "not_confirmed_loss",
                "只有已确认盘亏的失物才需要登记找回",
                item_id=settlement_item["item_id"],
            )
        existing = conn.execute(
            "SELECT * FROM stocktake_finds WHERE settlement_item_id = ?",
            (settlement_item["id"],),
        ).fetchone()
        if existing is not None:
            raise api_error(
                409,
                "already_recovered",
                "该失物已登记找回，不能重复登记",
                find=_find_view(conn, existing),
            )
        item = conn.execute(
            "SELECT * FROM items WHERE id = ?", (settlement_item["item_id"],)
        ).fetchone()
        assert item is not None
        if item["status"] == "delivered":
            raise api_error(
                409, "already_delivered", "该失物已交付，不能登记找回"
            )
        if item["inventory_loss_review_id"] is None:
            raise api_error(
                409,
                "not_confirmed_loss",
                "该失物的盘亏记录已解除，无需重复找回",
            )

        at = now_iso()
        seq = next_seq(conn)
        cur = conn.execute(
            "INSERT INTO stocktake_finds(round_id, settlement_item_id, item_id,"
            " counter_id, found_by, found_at, note, seq)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                round_id,
                settlement_item["id"],
                item["id"],
                round_row["counter_id"],
                staff["id"],
                at,
                note.strip(),
                seq,
            ),
        )
        find_id = int(cur.lastrowid)
        # Clearing the loss block and opening a fresh stay, in the same
        # transaction, means no concurrent booking/delivery sees a half state.
        conn.execute(
            "UPDATE items SET inventory_loss_review_id = NULL, inventory_loss_at = NULL,"
            " updated_at = ? WHERE id = ?",
            (at, item["id"]),
        )
        start_stay(conn, item["id"], round_row["counter_id"], "recovered", at)
        audit(
            conn,
            f"staff:{staff['id']}",
            "stocktake_item_recovered",
            f"round={round_id} item={item['id']} find={find_id}"
            f" counter={round_row['counter_id']}",
        )
        return {
            "round_id": round_id,
            "item_id": item["id"],
            "public_code": item["public_code"],
            "find_id": find_id,
            "found_at": at,
            "counter": counter_brief(conn, round_row["counter_id"]),
            "old_bookings_restored": False,
            "note": (
                "已登记找回：实物回到本柜台，按原资格规则重新预约与办理；"
                "此前的预约不会自动恢复。"
            ),
        }


# --------------------------------------------------------------------------
# Round view
# --------------------------------------------------------------------------


def round_view(conn: sqlite3.Connection, round_row: sqlite3.Row) -> dict[str, Any]:
    scans = conn.execute(
        "SELECT * FROM stocktake_scans WHERE round_id = ? ORDER BY id",
        (round_row["id"],),
    ).fetchall()
    counts = conn.execute(
        "SELECT classification, COUNT(*) AS n FROM stocktake_scans"
        " WHERE round_id = ? GROUP BY classification",
        (round_row["id"],),
    ).fetchall()
    by_kind = {r["classification"]: int(r["n"]) for r in counts}
    settlement = None
    if round_row["status"] == ROUND_CLOSED:
        settlement = settlement_view(conn, round_row)
    return {
        "round_id": round_row["id"],
        "counter": counter_brief(conn, round_row["counter_id"]),
        "status": round_row["status"],
        "opened_by": staff_brief(conn, round_row["opened_by"]),
        "opened_at": round_row["opened_at"],
        "closed_by": staff_brief(conn, round_row["closed_by"]),
        "closed_at": round_row["closed_at"],
        "scan_counts": {
            "total": len(scans),
            "seen": by_kind.get(SEEN, 0),
            "off_counter": by_kind.get(OFF_COUNTER, 0),
        },
        "scans": [_scan_view(conn, s) for s in scans],
        "settlement": settlement,
    }
