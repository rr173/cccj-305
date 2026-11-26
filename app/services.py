"""Domain rules: clue validity, eligibility, bookings, and delivery snapshots.

The central rule, in one place:

An *answer* row is immutable evidence of what a claimant typed at a point in
time.  It is a **valid private clue** for a detail when all of these hold:

1. its correction epoch equals the detail's current correction epoch
   (any staff correction permanently invalidates answers from before it,
   even if the answer is later changed back);
2. its text equals the answer that was registered when it was submitted
   (comparison = leading/trailing whitespace removed, otherwise exact);
3. it was submitted before the detail was first published (sequence number
   lower than ``details.published_seq``), or the detail was never published.

Two distinct valid private clues are required for on-site delivery eligibility.
Resubmitting identical text reuses the original answer row, so neither the
submission time nor eligibility changes.
"""

from __future__ import annotations

import secrets
import sqlite3
import string
import uuid
from typing import Any, TypedDict

from fastapi import HTTPException

from .db import audit, next_seq, now_iso, norm, transaction

MIN_PRIVATE_CLUES = 2

class APIErrorPayload(TypedDict):
    code: str
    message: str


VALID_PRIVATE = "valid_private"
CORRECT_BUT_PUBLIC = "correct_but_public"
INCORRECT = "incorrect"
INVALIDATED = "invalidated_by_correction"


def api_error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    payload: dict[str, Any] = {"code": code, "message": message}
    payload.update(extra)
    return HTTPException(status_code=status, detail=payload)


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------


def new_id() -> str:
    return uuid.uuid4().hex


def new_public_code(conn: sqlite3.Connection) -> str:
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(20):
        code = "LF-" + "".join(secrets.choice(alphabet) for _ in range(6))
        if not conn.execute("SELECT 1 FROM items WHERE public_code = ?", (code,)).fetchone():
            return code
    raise RuntimeError("could not allocate a unique public code")


def get_item(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise api_error(404, "item_not_found", "失物编号不存在")
    return row


def get_item_by_public_code(conn: sqlite3.Connection, public_code: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM items WHERE public_code = ?", (public_code,)
    ).fetchone()
    if row is None:
        raise api_error(404, "item_not_found", "失物编号不存在")
    return row


def get_detail(conn: sqlite3.Connection, item_id: str, detail_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM details WHERE id = ? AND item_id = ?", (detail_id, item_id)
    ).fetchone()
    if row is None:
        raise api_error(404, "detail_not_found", "细节编号不存在")
    return row


def get_detail_by_code(conn: sqlite3.Connection, item_id: str, code: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM details WHERE item_id = ? AND code = ?", (item_id, code)
    ).fetchone()
    if row is None:
        raise api_error(404, "detail_not_found", "细节编号不存在")
    return row


def assert_desk_can_handle_item(
    conn: sqlite3.Connection, item: sqlite3.Row, staff: sqlite3.Row
) -> None:
    """Counter custody gate for bookings and deliveries.

    Eligibility rules are unchanged; this adds the physical-custody rule:
    only the counter currently holding the item (state ``at_counter``) may
    process a claim.  In transit / missing / quarantined items and items at
    another counter are all rejected inside the caller's transaction.
    """
    state = item["custody_state"] if "custody_state" in item.keys() else "at_counter"
    if state != "at_counter":
        messages = {
            "in_transit": ("item_in_transit", "失物正在调拨在途，不能办理预约或交付"),
            "missing": ("item_missing", "失物已登记缺件、尚未找到，不能办理领取"),
            "quarantined": (
                "item_quarantined",
                "失物为错箱隔离状态，清点对上之前不能办理领取",
            ),
        }
        code, message = messages.get(
            state, ("item_not_at_counter", "失物当前不在可领取柜台")
        )
        raise api_error(409, code, message, custody_state=state)

    holder_id = item["custody_counter_id"] if "custody_counter_id" in item.keys() else None
    staff_counter_id = staff["counter_id"] if "counter_id" in staff.keys() else None
    if holder_id is not None and staff_counter_id != holder_id:
        holder = conn.execute(
            "SELECT id, code, name FROM counters WHERE id = ?", (holder_id,)
        ).fetchone()
        raise api_error(
            403,
            "wrong_counter",
            "该失物现由其他柜台保管，只有实际保管柜台能办理领取",
            holding_counter=None if holder is None else dict(holder),
        )


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def classify(row: sqlite3.Row | dict[str, Any]) -> str:
    epoch_at_submission = row["epoch_at_submission"]
    correction_epoch = row["correction_epoch"]
    if epoch_at_submission != correction_epoch:
        return INVALIDATED
    if norm(row["text"]) != norm(row["expected_at_submission"]):
        return INCORRECT
    published_seq = row["published_seq"]
    if published_seq is not None and row["seq"] > published_seq:
        return CORRECT_BUT_PUBLIC
    return VALID_PRIVATE


_LATEST_SELECT = """
SELECT a.id            AS answer_id,
       a.claimant_id,
       a.detail_id,
       a.text,
       a.submitted_at,
       a.seq,
       a.epoch_at_submission,
       a.expected_at_submission,
       d.code          AS detail_code,
       d.label         AS detail_label,
       d.answer        AS current_answer,
       d.is_public,
       d.published_seq,
       d.correction_epoch
  FROM answers a
  JOIN (SELECT claimant_id, detail_id, MAX(id) AS mid
          FROM answers
         GROUP BY claimant_id, detail_id) m
    ON m.mid = a.id
  JOIN details d ON d.id = a.detail_id
 WHERE d.item_id = ?
"""


def latest_answers_for_claimant(
    conn: sqlite3.Connection, item_id: str, claimant_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        _LATEST_SELECT + " AND a.claimant_id = ? ORDER BY d.code", (item_id, claimant_id)
    ).fetchall()
    return [{**dict(r), "clue_status": classify(r)} for r in rows]


def latest_answers_by_claimant(
    conn: sqlite3.Connection, item_id: str
) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(_LATEST_SELECT + " ORDER BY d.code", (item_id,)).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["claimant_id"], []).append(
            {**dict(r), "clue_status": classify(r)}
        )
    return grouped


def eligibility(conn: sqlite3.Connection, item_id: str, claimant_id: str) -> dict[str, Any]:
    answers = latest_answers_for_claimant(conn, item_id, claimant_id)
    private = [a for a in answers if a["clue_status"] == VALID_PRIVATE]
    return {
        "eligible": len(private) >= MIN_PRIVATE_CLUES,
        "valid_private_clue_count": len(private),
        "required_private_clues": MIN_PRIVATE_CLUES,
        "answers": answers,
    }


# --------------------------------------------------------------------------
# Claimant answer submission
# --------------------------------------------------------------------------


def submit_answer(
    conn: sqlite3.Connection,
    claimant_id: str,
    public_code: str,
    detail_code: str,
    text: str,
) -> dict[str, Any]:
    with transaction(conn):
        item = get_item_by_public_code(conn, public_code)
        detail = get_detail_by_code(conn, item["id"], detail_code)
        cleaned = text.strip()
        expected = detail["answer"].strip()

        latest = conn.execute(
            "SELECT * FROM answers WHERE claimant_id = ? AND detail_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (claimant_id, detail["id"]),
        ).fetchone()

        if latest is not None and norm(latest["text"]) == cleaned:
            # "原样重送不刷新这次回答的提交时点": the resend is the *same*
            # answer row.  In particular, if that row was invalidated by a
            # correction, replaying it cannot resurrect it.
            replayed = True
            answer = latest
        else:
            replayed = False
            seq = next_seq(conn)
            submitted_at = now_iso()
            cur = conn.execute(
                "INSERT INTO answers(claimant_id, detail_id, text, submitted_at, seq,"
                " epoch_at_submission, expected_at_submission)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    claimant_id,
                    detail["id"],
                    cleaned,
                    submitted_at,
                    seq,
                    detail["correction_epoch"],
                    expected,
                ),
            )
            answer = conn.execute(
                "SELECT * FROM answers WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            audit(
                conn,
                f"claimant:{claimant_id}",
                "answer_submitted",
                f"item={item['public_code']} detail={detail_code} answer_id={answer['id']}",
            )

        cancel_lost_bookings(conn, item["id"], claimant_id, actor="system:eligibility")
        elig = eligibility(conn, item["id"], claimant_id)
        return {
            "public_code": item["public_code"],
            "detail_code": detail["code"],
            # Until a detail is published, even its prompt stays internal.
            "detail_label": detail["label"] if detail["is_public"] else None,
            "detail_is_public": bool(detail["is_public"]),
            "submitted_text": answer["text"],
            "submitted_at": answer["submitted_at"],
            "replayed_identical_answer": replayed,
            "eligible": elig["eligible"],
            # Deliberately no per-detail correctness: the claimant must not see
            # the internal comparison.  Only the aggregate status is exposed.
        }


def claimant_item_status(
    conn: sqlite3.Connection, claimant_id: str, public_code: str
) -> dict[str, Any]:
    item = get_item_by_public_code(conn, public_code)
    elig = eligibility(conn, item["id"], claimant_id)
    booking = conn.execute(
        "SELECT b.*, s.starts_at AS slot_starts_at, s.ends_at AS slot_ends_at"
        "  FROM bookings b LEFT JOIN slots s ON s.id = b.slot_id"
        " WHERE b.item_id = ? AND b.claimant_id = ? ORDER BY b.id DESC LIMIT 1",
        (item["id"], claimant_id),
    ).fetchone()
    delivery = conn.execute(
        "SELECT * FROM deliveries WHERE item_id = ? AND claimant_id = ?",
        (item["id"], claimant_id),
    ).fetchone()

    booking_view: dict[str, Any] | None = None
    if booking is not None:
        from .scheduling import booking_slot_brief, effective_booking_ended

        status, ended_reason = effective_booking_ended(booking)
        counter = None
        if booking["counter_id"] is not None:
            row = conn.execute(
                "SELECT id, code, name FROM counters WHERE id = ?",
                (booking["counter_id"],),
            ).fetchone()
            counter = None if row is None else dict(row)
        checkin = conn.execute(
            "SELECT id FROM reception_checkins WHERE booking_id = ?", (booking["id"],)
        ).fetchone()
        booking_view = {
            "id": booking["id"],
            "status": status,
            "created_at": booking["created_at"],
            "ended_at": booking["ended_at"],
            "ended_reason": ended_reason,
            # The claimant may see where and when to come, but never the
            # internal roster or which specific person will receive them
            # before they actually check in.
            "counter": counter,
            "slot": booking_slot_brief(booking),
            "checked_in": checkin is not None,
        }

    return {
        "public_code": item["public_code"],
        "title": item["title"],
        "item_status": item["status"],
        "eligible_for_delivery": elig["eligible"],
        "required_private_clues": MIN_PRIVATE_CLUES,
        "your_answers": [
            {
                "detail_code": a["detail_code"],
                "detail_label": a["detail_label"] if a["is_public"] else None,
                "submitted_text": a["text"],
                "submitted_at": a["submitted_at"],
            }
            for a in elig["answers"]
        ],
        "booking": booking_view,
        "delivered_to_you": delivery is not None,
        "delivered_at": None if delivery is None else delivery["delivered_at"],
    }


# --------------------------------------------------------------------------
# Staff operations
# --------------------------------------------------------------------------


def add_detail(
    conn: sqlite3.Connection,
    item_id: str,
    staff_id: str,
    code: str,
    label: str,
    answer: str,
) -> sqlite3.Row:
    with transaction(conn):
        get_item(conn, item_id)
        existing = conn.execute(
            "SELECT 1 FROM details WHERE item_id = ? AND code = ?", (item_id, code)
        ).fetchone()
        if existing:
            raise api_error(409, "detail_code_taken", "该细节编号已存在")
        now = now_iso()
        detail_id = new_id()
        conn.execute(
            "INSERT INTO details(id, item_id, code, label, answer, is_public,"
            " published_seq, published_at, correction_epoch, created_at, updated_at)"
            " VALUES (?,?,?,?,?,0,NULL,NULL,0,?,?)",
            (detail_id, item_id, code, label, answer.strip(), now, now),
        )
        audit(
            conn,
            f"staff:{staff_id}",
            "detail_added",
            f"item_id={item_id} detail_id={detail_id} code={code}",
        )
        return conn.execute("SELECT * FROM details WHERE id = ?", (detail_id,)).fetchone()


def publish_detail(
    conn: sqlite3.Connection, item_id: str, detail_id: str, staff_id: str
) -> dict[str, Any]:
    with transaction(conn):
        item = get_item(conn, item_id)
        detail = get_detail(conn, item_id, detail_id)
        if detail["is_public"]:
            return {
                "detail_id": detail_id,
                "code": detail["code"],
                "answer": detail["answer"],
                "is_public": True,
                "published_at": detail["published_at"],
                "already_public": True,
                "note": "该细节已公开；公开状态不可撤销，以后展示的始终是更正后的答案。",
            }
        seq = next_seq(conn)
        at = now_iso()
        conn.execute(
            "UPDATE details SET is_public = 1, published_seq = ?, published_at = ?,"
            " updated_at = ? WHERE id = ?",
            (seq, at, at, detail_id),
        )
        audit(
            conn,
            f"staff:{staff_id}",
            "detail_published",
            f"item={item['public_code']} detail={detail['code']} seq={seq}",
        )
        return {
            "detail_id": detail_id,
            "code": detail["code"],
            "label": detail["label"],
            "answer": detail["answer"],
            "is_public": True,
            "published_at": at,
            "already_public": False,
            "note": "公开后不能重新转为秘密；提交于公开之前的正确回答仍可作为私有线索。",
        }


def correct_detail(
    conn: sqlite3.Connection,
    item_id: str,
    detail_id: str,
    staff_id: str,
    new_answer: str,
) -> dict[str, Any]:
    cleaned = new_answer.strip()
    with transaction(conn):
        item = get_item(conn, item_id)
        detail = get_detail(conn, item_id, detail_id)
        old_answer = detail["answer"]
        if norm(old_answer) == cleaned:
            return {
                "detail_id": detail_id,
                "code": detail["code"],
                "answer": old_answer,
                "correction_epoch": detail["correction_epoch"],
                "changed": False,
                "invalidated_bookings": [],
                "note": "新答案与原答案相同，未记作一次更正，既有回答的资格不受影响。",
            }

        old_epoch = detail["correction_epoch"]
        new_epoch = old_epoch + 1
        seq = next_seq(conn)
        at = now_iso()
        conn.execute(
            "UPDATE details SET answer = ?, correction_epoch = ?, updated_at = ?"
            " WHERE id = ?",
            (cleaned, new_epoch, at, detail_id),
        )
        conn.execute(
            "INSERT INTO detail_corrections(detail_id, old_answer, new_answer, old_epoch,"
            " new_epoch, corrected_by, at, seq) VALUES (?,?,?,?,?,?,?,?)",
            (detail_id, old_answer, cleaned, old_epoch, new_epoch, staff_id, at, seq),
        )
        invalidated = cancel_lost_bookings(
            conn, item_id, None, actor=f"staff:{staff_id}:correction"
        )
        audit(
            conn,
            f"staff:{staff_id}",
            "detail_corrected",
            f"item={item['public_code']} detail={detail['code']} "
            f"old_epoch={old_epoch} new_epoch={new_epoch}",
        )
        # Re-read the active booking picture after possible cancellations.
        active = conn.execute(
            "SELECT id, claimant_id FROM bookings WHERE item_id = ? AND status = 'active'",
            (item_id,),
        ).fetchall()
        return {
            "detail_id": detail_id,
            "code": detail["code"],
            "label": detail["label"],
            "old_answer": old_answer,
            "answer": cleaned,
            "is_public": bool(detail["is_public"]),
            "correction_epoch": new_epoch,
            "changed": True,
            "invalidated_bookings": invalidated,
            "remaining_active_bookings": [dict(r) for r in active],
            "note": (
                "该细节在更正前的所有回答已永久失去资格，即使以后改回原答案也不会恢复。"
                + ("该细节已公开，公开展示将使用更正后的答案。" if detail["is_public"] else "")
            ),
        }


def cancel_lost_bookings(
    conn: sqlite3.Connection,
    item_id: str,
    claimant_id: str | None,
    actor: str,
) -> list[int]:
    """Cancel active bookings whose claimant is no longer eligible.

    Cancellation is one-way: regaining eligibility later never re-activates a
    booking.  Must be called inside an open transaction.
    """
    query = "SELECT * FROM bookings WHERE item_id = ? AND status = 'active'"
    params: list[Any] = [item_id]
    if claimant_id is not None:
        query += " AND claimant_id = ?"
        params.append(claimant_id)

    cancelled: list[int] = []
    for booking in conn.execute(query, params).fetchall():
        elig = eligibility(conn, item_id, booking["claimant_id"])
        if not elig["eligible"]:
            at = now_iso()
            conn.execute(
                "UPDATE bookings SET status = 'cancelled', ended_at = ?,"
                " ended_reason = 'eligibility_lost' WHERE id = ?",
                (at, booking["id"]),
            )
            audit(
                conn,
                actor,
                "booking_cancelled",
                f"booking={booking['id']} reason=eligibility_lost",
            )
            cancelled.append(booking["id"])
    return cancelled


def create_booking(
    conn: sqlite3.Connection,
    item_id: str,
    claimant_id: str,
    staff_id: str,
    slot_ref: str,
) -> dict[str, Any]:
    from . import scheduling

    with transaction(conn):
        scheduling.sweep_expired_bookings(conn)
        item = get_item(conn, item_id)
        if item["status"] != "held":
            raise api_error(409, "item_not_held", "该失物已交付，不能再预约")
        actor_row = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
        assert_desk_can_handle_item(conn, item, actor_row)
        claimant = conn.execute(
            "SELECT * FROM claimants WHERE id = ?", (claimant_id,)
        ).fetchone()
        if claimant is None:
            raise api_error(404, "claimant_not_found", "认领者不存在")

        # Slot format / existence is validated before eligibility so that a
        # malformed request fails fast and never creates anything.
        slot = scheduling.resolve_slot(conn, slot_ref, create=True)
        scheduling.assert_slot_not_ended(slot)

        elig = eligibility(conn, item_id, claimant_id)
        if not elig["eligible"]:
            raise api_error(
                409,
                "not_eligible",
                "该认领者当前不满足两条有效私有线索，不能安排到场",
                valid_private_clue_count=elig["valid_private_clue_count"],
                required_private_clues=MIN_PRIVATE_CLUES,
            )
        active = conn.execute(
            "SELECT * FROM bookings WHERE item_id = ? AND status = 'active'", (item_id,)
        ).fetchone()
        if active is not None:
            if active["claimant_id"] == claimant_id:
                raise api_error(
                    409, "already_booked", "该认领者已有有效预约", booking_id=active["id"]
                )
            raise api_error(
                409,
                "item_already_booked",
                "该失物已有其他认领者的有效预约，请先取消原预约",
                booking_id=active["id"],
            )

        capacity = scheduling.assert_booking_fits(conn, slot, item["custody_counter_id"])

        seq = next_seq(conn)
        at = now_iso()
        cur = conn.execute(
            "INSERT INTO bookings(item_id, claimant_id, status, created_by, created_at,"
            " slot_id, counter_id, seq)"
            " VALUES (?,?,'active',?,?,?,?,?)",
            (
                item_id,
                claimant_id,
                staff_id,
                at,
                slot["id"],
                item["custody_counter_id"],
                seq,
            ),
        )
        booking_id = cur.lastrowid
        audit(
            conn,
            f"staff:{staff_id}",
            "booking_created",
            f"item={item['public_code']} booking={booking_id} claimant={claimant_id}"
            f" slot={slot['id']} counter={item['custody_counter_id']}",
        )
        return {
            "booking_id": booking_id,
            "item_id": item_id,
            "public_code": item["public_code"],
            "claimant_id": claimant_id,
            "claimant_display_name": claimant["display_name"],
            "status": "active",
            "created_at": at,
            "item_status": "held",
            "counter": scheduling._counter_brief(conn, item["custody_counter_id"]),
            "slot": scheduling.slot_brief(slot),
            **capacity,
            "note": (
                "仅安排到场，不改变实物持有；已确认该时段所有未报到预约都能"
                "分给不同的可用值班人。失去资格或时段结束会取消/到期，不会自动恢复。"
            ),
        }


def assign_legacy_booking_slot(
    conn: sqlite3.Connection,
    item_id: str,
    booking_id: int,
    slot_ref: str,
    actor: sqlite3.Row,
) -> dict[str, Any]:
    """Give a slot once to a legacy (slotless) active booking.

    The original claimant is kept; the booking may also simply be cancelled
    through the normal cancel endpoint.  After assignment, every normal
    capacity/eligibility rule applies and the booking can be received.
    """
    from . import scheduling

    with transaction(conn):
        scheduling.sweep_expired_bookings(conn)
        item = get_item(conn, item_id)
        booking = conn.execute(
            "SELECT * FROM bookings WHERE id = ? AND item_id = ?", (booking_id, item_id)
        ).fetchone()
        if booking is None:
            raise api_error(404, "booking_not_found", "预约不存在")
        assert_desk_can_handle_item(conn, item, actor)
        if booking["status"] != "active":
            raise api_error(
                409, "booking_not_active", "该预约已结束", booking_status=booking["status"]
            )
        if booking["slot_id"] is not None:
            raise api_error(
                409, "slot_already_assigned",
                "该预约已有接待时段；如需改时段请取消后重新预约",
                booking_id=booking_id,
            )
        slot = scheduling.resolve_slot(conn, slot_ref, create=False)
        scheduling.assert_slot_not_ended(slot)
        capacity = scheduling.assert_booking_fits(
            conn, slot, item["custody_counter_id"]
        )
        at = now_iso()
        conn.execute(
            "UPDATE bookings SET slot_id = ?, counter_id = ? WHERE id = ?",
            (slot["id"], item["custody_counter_id"], booking_id),
        )
        audit(
            conn,
            f"staff:{actor['id']}",
            "legacy_booking_slot_assigned",
            f"item={item['public_code']} booking={booking_id} slot={slot['id']}"
            f" counter={item['custody_counter_id']}",
        )
        return {
            "booking_id": booking_id,
            "item_id": item_id,
            "claimant_id": booking["claimant_id"],
            "status": "active",
            "counter": scheduling._counter_brief(conn, item["custody_counter_id"]),
            "slot": scheduling.slot_brief(slot),
            **capacity,
            "note": "仍为原认领者补约时段；补约后按正常规则报到与交付。",
        }


def cancel_booking(
    conn: sqlite3.Connection, item_id: str, booking_id: int, staff_id: str
) -> dict[str, Any]:
    with transaction(conn):
        item = get_item(conn, item_id)
        booking = conn.execute(
            "SELECT * FROM bookings WHERE id = ? AND item_id = ?", (booking_id, item_id)
        ).fetchone()
        if booking is None:
            raise api_error(404, "booking_not_found", "预约不存在")
        if booking["status"] != "active":
            raise api_error(409, "booking_not_active", "该预约已结束", booking_status=booking["status"])
        at = now_iso()
        conn.execute(
            "UPDATE bookings SET status = 'cancelled', ended_at = ?,"
            " ended_reason = 'staff_cancelled' WHERE id = ?",
            (at, booking_id),
        )
        audit(
            conn,
            f"staff:{staff_id}",
            "booking_cancelled",
            f"item={item['public_code']} booking={booking_id} reason=staff_cancelled",
        )
        return {
            "booking_id": booking_id,
            "status": "cancelled",
            "ended_at": at,
            "ended_reason": "staff_cancelled",
        }


def deliver(
    conn: sqlite3.Connection, item_id: str, claimant_id: str, staff_id: str
) -> dict[str, Any]:
    from . import scheduling

    fail: APIErrorPayload | None = None
    result: dict[str, Any] | None = None
    with transaction(conn):
        item = get_item(conn, item_id)
        if item["status"] == "delivered":
            existing = conn.execute(
                "SELECT * FROM deliveries WHERE item_id = ?", (item_id,)
            ).fetchone()
            raise api_error(
                409,
                "already_delivered",
                "该失物已交付，不能重复办理",
                delivered_to_claimant_id=None if existing is None else existing["claimant_id"],
            )

        actor_row = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
        assert_desk_can_handle_item(conn, item, actor_row)

        booking = conn.execute(
            "SELECT * FROM bookings WHERE item_id = ? AND status = 'active'"
            " ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        if booking is None:
            raise api_error(409, "not_booked", "没有有效预约，不能办理交付")
        if booking["claimant_id"] != claimant_id:
            raise api_error(
                409,
                "booking_mismatch",
                "当前有效预约属于另一名认领者，不能向其交付",
                booked_claimant_id=booking["claimant_id"],
            )
        if booking["slot_id"] is None:
            raise api_error(
                409, "slot_required",
                "旧预约尚未补约接待时段，不能交付；请先补约时段并报到",
            )
        slot = conn.execute(
            "SELECT * FROM slots WHERE id = ?", (booking["slot_id"],)
        ).fetchone()
        assert slot is not None

        if slot["ends_at"] <= now_iso():
            # The slot has ended and the appointment was not delivered in
            # time.  Expire it (committing below by failing *after* the
            # transaction), so it cannot be checked in or delivered later.
            at = now_iso()
            conn.execute(
                "UPDATE bookings SET status = 'expired', ended_at = ?,"
                " ended_reason = ? WHERE id = ?",
                (at, scheduling.SLOT_ENDED, booking["id"]),
            )
            audit(
                conn,
                f"staff:{staff_id}",
                "booking_expired",
                f"booking={booking['id']} reason=slot_ended_at_delivery",
            )
            fail = {
                "code": scheduling.SLOT_ENDED,
                "message": "该预约的接待时段已结束且未交付，不能补办；请重新预约新的时段",
                "booking_id": booking["id"],
                "slot": scheduling.slot_brief(slot),
            }
        else:
            checkin = conn.execute(
                "SELECT * FROM reception_checkins WHERE booking_id = ?",
                (booking["id"],),
            ).fetchone()
            if checkin is None:
                raise api_error(
                    409, "not_checked_in",
                    "交付前必须先由保管柜台登记实际接待人",
                    booking_id=booking["id"],
                )

            elig = eligibility(conn, item_id, claimant_id)
            if not elig["eligible"]:
                # Eligibility loss normally cancels the booking when it
                # happens; this is the mandatory re-check at the desk.  The
                # checked-in person's seat stays reserved until the slot ends.
                at = now_iso()
                conn.execute(
                    "UPDATE bookings SET status = 'cancelled', ended_at = ?,"
                    " ended_reason = 'eligibility_lost' WHERE id = ?",
                    (at, booking["id"]),
                )
                audit(
                    conn,
                    f"staff:{staff_id}",
                    "booking_cancelled",
                    f"booking={booking['id']} reason=eligibility_lost_at_delivery",
                )
                fail = {
                    "code": "evidence_invalid",
                    "message": "交付时复核未通过：有效私有线索不足，不能交付",
                    "valid_private_clue_count": elig["valid_private_clue_count"],
                    "required_private_clues": MIN_PRIVATE_CLUES,
                }
            else:
                private_rows = [
                    a for a in elig["answers"] if a["clue_status"] == VALID_PRIVATE
                ]
                at = now_iso()
                seq = next_seq(conn)
                cur = conn.execute(
                    "INSERT INTO deliveries(item_id, claimant_id, booked_by, delivered_by,"
                    " delivered_at, private_clue_count, eligibility_note, seq,"
                    " booking_id, slot_id, reception_staff_id, counter_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item_id,
                        claimant_id,
                        booking["created_by"],
                        staff_id,
                        at,
                        len(private_rows),
                        (
                            f"交付时复核：{len(private_rows)} 条有效私有线索（"
                            + "、".join(r["detail_code"] for r in private_rows)
                            + f"），预约 #{booking['id']} 有效，已在时段 "
                            + f"{slot['id']} 报到，实物仍在馆。"
                        ),
                        seq,
                        booking["id"],
                        slot["id"],
                        checkin["staff_id"],
                        checkin["counter_id"],
                    ),
                )
                delivery_id = cur.lastrowid

                for row in private_rows:
                    conn.execute(
                        "INSERT INTO delivery_evidence(delivery_id, detail_id, detail_code,"
                        " detail_label, answer_at_delivery, answer_at_submission, submitted_text,"
                        " counted_as_private, was_public_at_delivery, epoch_at_submission,"
                        " submitted_seq, submitted_at)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            delivery_id,
                            row["detail_id"],
                            row["detail_code"],
                            row["detail_label"],
                            row["current_answer"],
                            row["expected_at_submission"],
                            row["text"],
                            1,
                            1 if row["is_public"] else 0,
                            row["epoch_at_submission"],
                            row["seq"],
                            row["submitted_at"],
                        ),
                    )

                conn.execute(
                    "UPDATE bookings SET status = 'fulfilled', ended_at = ? WHERE id = ?",
                    (at, booking["id"]),
                )
                conn.execute(
                    "UPDATE items SET status = 'delivered', updated_at = ? WHERE id = ?",
                    (at, item_id),
                )
                audit(
                    conn,
                    f"staff:{staff_id}",
                    "delivery_completed",
                    f"item={item['public_code']} delivery={delivery_id}"
                    f" claimant={claimant_id} booking={booking['id']}",
                )
                result = delivery_view(conn, item_id)

    if fail is not None:
        raise HTTPException(status_code=409, detail=fail)
    assert result is not None
    return result


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


def public_item(conn: sqlite3.Connection, item: sqlite3.Row) -> dict[str, Any]:
    details = conn.execute(
        "SELECT code, label, answer, published_at FROM details"
        " WHERE item_id = ? AND is_public = 1 ORDER BY code",
        (item["id"],),
    ).fetchall()
    return {
        "public_code": item["public_code"],
        "title": item["title"],
        "public_description": item["public_description"],
        "found_at": item["found_at"],
        "found_location": item["found_location"],
        "status": item["status"],
        "public_details": [dict(d) for d in details],
    }


def _active_booking_now(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    """Active booking that has not effectively expired with its slot."""
    return conn.execute(
        "SELECT b.*, c.display_name, s.ends_at AS slot_ends_at"
        "  FROM bookings b JOIN claimants c ON c.id = b.claimant_id"
        "  LEFT JOIN slots s ON s.id = b.slot_id"
        " WHERE b.item_id = ? AND b.status = 'active'"
        "   AND (s.ends_at IS NULL OR s.ends_at > ?)"
        " ORDER BY b.id DESC LIMIT 1",
        (item_id, now_iso()),
    ).fetchone()


def staff_item(conn: sqlite3.Connection, item: sqlite3.Row) -> dict[str, Any]:
    details = conn.execute(
        "SELECT * FROM details WHERE item_id = ? ORDER BY code", (item["id"],)
    ).fetchall()
    corrections = conn.execute(
        "SELECT c.* FROM detail_corrections c JOIN details d ON d.id = c.detail_id"
        " WHERE d.item_id = ? ORDER BY c.id",
        (item["id"],),
    ).fetchall()
    booking = _active_booking_now(conn, item["id"])
    delivery = conn.execute(
        "SELECT * FROM deliveries WHERE item_id = ?", (item["id"],)
    ).fetchone()
    holder = None
    custody_state = item["custody_state"] if "custody_state" in item.keys() else "at_counter"
    holder_id = item["custody_counter_id"] if "custody_counter_id" in item.keys() else None
    if holder_id is not None:
        counter = conn.execute(
            "SELECT id, code, name, is_main FROM counters WHERE id = ?", (holder_id,)
        ).fetchone()
        holder = None if counter is None else dict(counter)
    return {
        "id": item["id"],
        "public_code": item["public_code"],
        "title": item["title"],
        "public_description": item["public_description"],
        "found_at": item["found_at"],
        "found_location": item["found_location"],
        "status": item["status"],
        "custody_state": custody_state,
        "holding_counter": holder,
        "claim_processing_available": item["status"] == "held" and custody_state == "at_counter",
        "details": [
            {
                "id": d["id"],
                "code": d["code"],
                "label": d["label"],
                "answer": d["answer"],
                "is_public": bool(d["is_public"]),
                "published_at": d["published_at"],
                "correction_epoch": d["correction_epoch"],
            }
            for d in details
        ],
        "corrections": [dict(c) for c in corrections],
        "active_booking": None
        if booking is None
        else {
            "booking_id": booking["id"],
            "claimant_id": booking["claimant_id"],
            "claimant_display_name": booking["display_name"],
            "created_at": booking["created_at"],
        },
        "has_delivery": delivery is not None,
    }


def item_claimants(conn: sqlite3.Connection, item: sqlite3.Row) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT DISTINCT c.id, c.display_name FROM claimants c"
        " JOIN answers a ON a.claimant_id = c.id"
        " JOIN details d ON d.id = a.detail_id"
        " WHERE d.item_id = ? ORDER BY c.display_name",
        (item["id"],),
    ).fetchall()
    matrix = latest_answers_by_claimant(conn, item["id"])
    booking = _active_booking_now(conn, item["id"])
    delivery = conn.execute(
        "SELECT * FROM deliveries WHERE item_id = ?", (item["id"],)
    ).fetchone()

    out = []
    for claimant in rows:
        answers = matrix.get(claimant["id"], [])
        private = [a for a in answers if a["clue_status"] == VALID_PRIVATE]
        out.append(
            {
                "claimant_id": claimant["id"],
                "display_name": claimant["display_name"],
                "eligible": len(private) >= MIN_PRIVATE_CLUES,
                "valid_private_clue_count": len(private),
                "booked": booking is not None and booking["claimant_id"] == claimant["id"],
                "delivered_to": delivery is not None
                and delivery["claimant_id"] == claimant["id"],
                "answers": [
                    {
                        "detail_code": a["detail_code"],
                        "detail_label": a["detail_label"],
                        "submitted_text": a["text"],
                        "submitted_at": a["submitted_at"],
                        "clue_status": a["clue_status"],
                        "counts_as_private_clue": a["clue_status"] == VALID_PRIVATE,
                    }
                    for a in answers
                ],
            }
        )
    return out


def delivery_view(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    delivery = conn.execute(
        "SELECT * FROM deliveries WHERE item_id = ?", (item_id,)
    ).fetchone()
    if delivery is None:
        raise api_error(404, "delivery_not_found", "该失物尚无交付记录")
    claimant = conn.execute(
        "SELECT display_name FROM claimants WHERE id = ?", (delivery["claimant_id"],)
    ).fetchone()

    def _staff_name(staff_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT id, name FROM staff WHERE id = ?", (staff_id,)
        ).fetchone()
        return None if row is None else {"id": row["id"], "name": row["name"]}

    def _counter_name(counter_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT id, code, name FROM counters WHERE id = ?", (counter_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def _delivery_slot(slot_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
        return None if row is None else {
            "slot_id": row["id"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
        }

    evidence = conn.execute(
        "SELECT e.*, d.correction_epoch AS current_epoch, d.answer AS current_answer,"
        " d.is_public AS currently_public"
        " FROM delivery_evidence e JOIN details d ON d.id = e.detail_id"
        " WHERE e.delivery_id = ? ORDER BY e.detail_code",
        (delivery["id"],),
    ).fetchall()

    items = []
    any_later_invalidated = False
    for e in evidence:
        later_invalidated = e["epoch_at_submission"] != e["current_epoch"]
        if later_invalidated:
            any_later_invalidated = True
        items.append(
            {
                "detail_code": e["detail_code"],
                "detail_label": e["detail_label"],
                "submitted_text": e["submitted_text"],
                "answer_at_delivery": e["answer_at_delivery"],
                "answer_at_submission": e["answer_at_submission"],
                "counted_as_private": bool(e["counted_as_private"]),
                "was_public_at_delivery": bool(e["was_public_at_delivery"]),
                "submitted_at": e["submitted_at"],
                "later_invalidated": later_invalidated,
                "invalidated_reason": (
                    "该细节的正确答案在交付后被工作人员更正；交付结论保持不变"
                    if later_invalidated
                    else None
                ),
                "current_answer": e["current_answer"],
            }
        )

    return {
        "delivery_id": delivery["id"],
        "public_code": conn.execute(
            "SELECT public_code FROM items WHERE id = ?", (item_id,)
        ).fetchone()["public_code"],
        "claimant_id": delivery["claimant_id"],
        "claimant_display_name": claimant["display_name"] if claimant else None,
        "delivered_at": delivery["delivered_at"],
        "private_clue_count": delivery["private_clue_count"],
        "eligibility_note": delivery["eligibility_note"],
        "slot": None
        if delivery["slot_id"] is None
        else _delivery_slot(delivery["slot_id"]),
        "reception_staff": None
        if delivery["reception_staff_id"] is None
        else _staff_name(delivery["reception_staff_id"]),
        "counter": None
        if delivery["counter_id"] is None
        else _counter_name(delivery["counter_id"]),
        "evidence": items,
        "has_later_invalidated_evidence": any_later_invalidated,
        "note": (
            "交付记录为历史快照，不会因后续更正而改动；"
            "later_invalidated 仅标注哪些当时依据后来失效。"
        ),
    }
