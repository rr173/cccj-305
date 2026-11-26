"""Reception slots, duty rosters, capacity and actual check-in.

Reception capacity is shared between counters.  A slot is a fixed 30-minute
window starting on a UTC full or half hour.  An admin sets (or replaces as a
whole) the slot's roster: which staff member may receive at which counters.
Rostering does not change a person's home counter or any existing rights.

One person may receive at most one booking in a slot.  A booking is only
confirmed when every still-active, not-yet-checked-in booking in the slot can
be assigned to a *different* available rostered person at its own counter.
Old bookings are never cancelled to make room for a new one.

When a roster shrinks, already-registered check-ins are left untouched and
occupy their person; among the remaining un-checked-in bookings as many as
possible are kept, and among equal counts the earlier-confirmed bookings win.
The rest are cancelled with reason ``roster_changed`` and never reactivated.

A check-in reserves its person until the slot ends, even if the booking is
delivered early or later cancelled.  Check-ins and deliveries are append-only
history.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .db import audit, next_seq, now_iso, transaction
from .services import api_error, get_item

SLOT_MINUTES = 30
SLOT_ID_RE = re.compile(r"^slot_(\d{8}T\d{4})Z$")

ROSTER_CANCELLED = "roster_changed"
SLOT_ENDED = "slot_ended"


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------


def parse_slot_start(value: str) -> datetime:
    """Parse an ISO-8601 instant and require a UTC full/half-hour boundary."""
    raw = (value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise api_error(422, "invalid_slot_start", "时段开始时间不是合法的 ISO 时间")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise api_error(
            422,
            "slot_start_needs_timezone",
            "时段开始时间必须带时区（例如以 Z 结尾的 UTC 时间）",
        )
    start = parsed.astimezone(timezone.utc)
    if start.second != 0 or start.microsecond != 0 or start.minute not in (0, 30):
        raise api_error(
            422,
            "slot_start_not_aligned",
            "接待时段固定为 UTC 整点或半点开始的 30 分钟",
        )
    return start.replace(second=0, microsecond=0)


def slot_id_for(start: datetime) -> str:
    return "slot_" + start.strftime("%Y%m%dT%H%MZ")


def _start_from_slot_id(slot_id: str) -> datetime | None:
    match = SLOT_ID_RE.match(slot_id or "")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def slot_iso(start: datetime) -> str:
    return start.isoformat(timespec="microseconds")


def get_or_create_slot(conn: sqlite3.Connection, start: datetime) -> sqlite3.Row:
    slot_id = slot_id_for(start)
    row = conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()
    if row is not None:
        return row
    at = now_iso()
    conn.execute(
        "INSERT INTO slots(id, starts_at, ends_at, created_at) VALUES (?,?,?,?)",
        (slot_id, slot_iso(start), slot_iso(start + timedelta(minutes=SLOT_MINUTES)), at),
    )
    audit(conn, "system:scheduling", "slot_created", f"slot={slot_id}")
    return conn.execute("SELECT * FROM slots WHERE id = ?", (slot_id,)).fetchone()


def resolve_slot(
    conn: sqlite3.Connection, ref: str, *, create: bool
) -> sqlite3.Row:
    """Resolve a canonical slot id or an ISO start instant."""
    ref = (ref or "").strip()
    start = _start_from_slot_id(ref)
    if start is not None:
        if create:
            return get_or_create_slot(conn, start)
        row = conn.execute("SELECT * FROM slots WHERE id = ?", (ref,)).fetchone()
    else:
        start = parse_slot_start(ref)
        if create:
            return get_or_create_slot(conn, start)
        row = conn.execute(
            "SELECT * FROM slots WHERE starts_at = ?", (slot_iso(start),)
        ).fetchone()
    if row is None:
        raise api_error(404, "slot_not_found", "该接待时段不存在", slot_ref=ref)
    return row


def slot_brief(row: sqlite3.Row, now: str | None = None) -> dict[str, Any]:
    now = now or now_iso()
    return {
        "slot_id": row["id"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "ended": row["ends_at"] <= now,
    }


def slot_not_ended(row: sqlite3.Row, now: str | None = None) -> bool:
    return row["ends_at"] > (now or now_iso())


def assert_slot_not_ended(row: sqlite3.Row) -> None:
    if not slot_not_ended(row):
        raise api_error(409, SLOT_ENDED, "该接待时段已结束", slot=slot_brief(row))


def sweep_expired_bookings(conn: sqlite3.Connection) -> list[int]:
    """Expire active bookings whose slot has ended without delivery.

    Check-in and delivery history is retained.  Expiry releases the item's
    single active booking, so a later slot can be booked.  Must run inside the
    caller's transaction.
    """
    now = now_iso()
    rows = conn.execute(
        "SELECT b.*, s.ends_at FROM bookings b JOIN slots s ON s.id = b.slot_id"
        " WHERE b.status = 'active' AND s.ends_at <= ?",
        (now,),
    ).fetchall()
    expired: list[int] = []
    for row in rows:
        conn.execute(
            "UPDATE bookings SET status = 'expired', ended_at = ?,"
            " ended_reason = ? WHERE id = ?",
            (now, SLOT_ENDED, row["id"]),
        )
        audit(
            conn,
            "system:scheduling",
            "booking_expired",
            f"booking={row['id']} slot={row['slot_id']} reason={SLOT_ENDED}",
        )
        expired.append(row["id"])
    return expired


def effective_booking_ended(booking: sqlite3.Row, now: str | None = None) -> tuple[str, str | None]:
    """Status/reason as shown to readers; does not write."""
    now = now or now_iso()
    if (
        booking["status"] == "active"
        and booking["slot_id"] is not None
        and booking["slot_ends_at"] is not None
        and booking["slot_ends_at"] <= now
    ):
        return "expired", SLOT_ENDED
    return booking["status"], booking["ended_reason"]


# --------------------------------------------------------------------------
# Rosters
# --------------------------------------------------------------------------


def roster_map(
    conn: sqlite3.Connection, slot_id: str
) -> dict[str, set[str]]:
    rows = conn.execute(
        "SELECT staff_id, counter_id FROM slot_rosters WHERE slot_id = ?",
        (slot_id,),
    ).fetchall()
    result: dict[str, set[str]] = {}
    for row in rows:
        result.setdefault(row["staff_id"], set()).add(row["counter_id"])
    return result


def checked_in_staff(conn: sqlite3.Connection, slot_id: str) -> set[str]:
    rows = conn.execute(
        "SELECT staff_id FROM reception_checkins WHERE slot_id = ?", (slot_id,)
    ).fetchall()
    return {row["staff_id"] for row in rows}


def slot_checkins(conn: sqlite3.Connection, slot_id: str) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM reception_checkins WHERE slot_id = ? ORDER BY id", (slot_id,)
    ).fetchall()
    return {row["booking_id"]: row for row in rows}


def active_slot_bookings(conn: sqlite3.Connection, slot_id: str) -> list[sqlite3.Row]:
    """Active bookings for a slot, earliest confirmation first."""
    return conn.execute(
        "SELECT b.*, s.ends_at AS slot_ends_at FROM bookings b"
        " JOIN slots s ON s.id = b.slot_id"
        " WHERE b.slot_id = ? AND b.status = 'active'"
        " ORDER BY b.seq, b.id",
        (slot_id,),
    ).fetchall()


def _match_bookings(
    bookings: Iterable[sqlite3.Row],
    roster: dict[str, set[str]],
    occupied: set[str],
) -> tuple[dict[str, int], list[sqlite3.Row]]:
    """Greedy (Kuhn) one-booking-per-person matching in priority order.

    ``bookings`` must be ordered earliest-confirmed first.  As each booking is
    attempted, an augmenting path is searched only through still-available
    staff; previously kept bookings stay matched.  The kept set is therefore
    a maximum-cardinality matching and, among those, the earliest-priority
    one: a later booking is dropped only when including it would make some
    earlier booking unable to be received.
    """
    available = {
        staff_id for staff_id in roster if staff_id not in occupied
    }
    assignment: dict[str, int] = {}  # staff_id -> booking_id

    def augment(booking_id: int, seen: set[str]) -> bool:
        counter_id = booking_counter.get(booking_id)
        for staff_id in sorted(roster):
            if staff_id not in available or staff_id in seen:
                continue
            if counter_id not in roster[staff_id]:
                continue
            seen.add(staff_id)
            holder = assignment.get(staff_id)
            if holder is None or augment(holder, seen):
                assignment[staff_id] = booking_id
                return True
        return False

    booking_counter: dict[int, str] = {}
    kept: list[sqlite3.Row] = []
    for booking in bookings:
        booking_counter[booking["id"]] = booking["counter_id"]
        if augment(booking["id"], set()):
            kept.append(booking)
    return assignment, kept


def _cancel_roster_excess(
    conn: sqlite3.Connection, slot_id: str, at: str, actor: str
) -> list[int]:
    """Cancel only the un-checked-in bookings the new roster cannot cover."""
    bookings = active_slot_bookings(conn, slot_id)
    checkins = slot_checkins(conn, slot_id)
    occupied = {row["staff_id"] for row in checkins.values()}
    roster = roster_map(conn, slot_id)
    pending = [b for b in bookings if b["id"] not in checkins]
    _, kept = _match_bookings(pending, roster, occupied)
    kept_ids = {b["id"] for b in kept}
    cancelled: list[int] = []
    for booking in pending:
        if booking["id"] in kept_ids:
            continue
        conn.execute(
            "UPDATE bookings SET status = 'cancelled', ended_at = ?,"
            " ended_reason = ? WHERE id = ?",
            (at, ROSTER_CANCELLED, booking["id"]),
        )
        audit(
            conn,
            actor,
            "booking_cancelled",
            f"booking={booking['id']} reason={ROSTER_CANCELLED} slot={slot_id}",
        )
        cancelled.append(booking["id"])
    return cancelled


def set_roster(
    conn: sqlite3.Connection,
    actor: sqlite3.Row,
    slot_ref: str,
    entries: list[Any],
) -> dict[str, Any]:
    """Replace the whole roster of one slot (admin only)."""
    if not actor["is_admin"]:
        raise api_error(403, "forbidden", "仅管理员可设置接待名单")

    with transaction(conn):
        slot = resolve_slot(conn, slot_ref, create=True)
        sweep_expired_bookings(conn)
        assert_slot_not_ended(slot)
        now = now_iso()

        previous = roster_map(conn, slot["id"])
        previous_snapshot = {
            staff_id: sorted(counters) for staff_id, counters in previous.items()
        }

        new_rows: list[tuple[str, str]] = []
        seen_pairs: set[tuple[str, str]] = set()
        for entry in entries:
            staff = conn.execute(
                "SELECT * FROM staff WHERE id = ?", (entry.staff_id,)
            ).fetchone()
            if staff is None:
                raise api_error(
                    404, "staff_not_found", "接待名单中的工作人员不存在",
                    staff_id=entry.staff_id,
                )
            for counter_ref in entry.counter_ids:
                counter = conn.execute(
                    "SELECT * FROM counters WHERE id = ? OR code = ?",
                    (counter_ref, counter_ref),
                ).fetchone()
                if counter is None:
                    raise api_error(
                        404, "counter_not_found", "接待名单中的柜台不存在",
                        counter_ref=counter_ref,
                    )
                pair = (staff["id"], counter["id"])
                if pair in seen_pairs:
                    raise api_error(
                        422, "duplicate_roster_entry",
                        "同一工作人员在同一柜台重复列入名单",
                        staff_id=staff["id"], counter=counter["id"],
                    )
                seen_pairs.add(pair)
                new_rows.append(pair)

        conn.execute("DELETE FROM slot_rosters WHERE slot_id = ?", (slot["id"],))
        for staff_id, counter_id in new_rows:
            conn.execute(
                "INSERT INTO slot_rosters(slot_id, staff_id, counter_id, set_by, set_at)"
                " VALUES (?,?,?,?,?)",
                (slot["id"], staff_id, counter_id, actor["id"], now),
            )

        cancelled = _cancel_roster_excess(
            conn, slot["id"], now, f"staff:{actor['id']}:roster"
        )

        seq = next_seq(conn)
        version = (
            conn.execute(
                "SELECT COUNT(*) AS n FROM roster_changes WHERE slot_id = ?",
                (slot["id"],),
            ).fetchone()["n"]
            + 1
        )
        conn.execute(
            "INSERT INTO roster_changes(slot_id, version, changed_by, changed_at,"
            " entry_count, cancelled_bookings, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                slot["id"],
                version,
                actor["id"],
                now,
                len(new_rows),
                json.dumps(cancelled),
                json.dumps(
                    {
                        "previous": previous_snapshot,
                        "seat": seq,
                        "policy": "keep_checked_in; keep_earliest_confirmed",
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        audit(
            conn,
            f"staff:{actor['id']}",
            "roster_replaced",
            f"slot={slot['id']} entries={len(new_rows)} version={version}"
            f" cancelled={cancelled}",
        )
        return roster_view(conn, slot, cancelled_bookings=cancelled, version=version)


def roster_view(
    conn: sqlite3.Connection,
    slot: sqlite3.Row,
    *,
    cancelled_bookings: list[int] | None = None,
    version: int | None = None,
) -> dict[str, Any]:
    roster = roster_map(conn, slot["id"])
    checkins = checked_in_staff(conn, slot["id"])
    rows = []
    for staff_id, counter_ids in sorted(roster.items()):
        staff = conn.execute(
            "SELECT id, name, counter_id FROM staff WHERE id = ?", (staff_id,)
        ).fetchone()
        if staff is None:
            continue
        rows.append(
            {
                "staff_id": staff_id,
                "name": staff["name"],
                "counters": [
                    _counter_brief(conn, cid) for cid in sorted(counter_ids)
                ],
                "checked_in": staff_id in checkins,
            }
        )
    if version is None:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM roster_changes WHERE slot_id = ?",
            (slot["id"],),
        ).fetchone()
        version = row["v"] if row is not None else None
    return {
        "slot": slot_brief(slot),
        "version": version,
        "entries": rows,
        # A person rostered at several counters still only provides one seat.
        "seat_count": len(roster),
        "cancelled_bookings": cancelled_bookings or [],
        "note": (
            "名单只说明每名值班人在本时段能在哪些柜台接待；"
            "一人同一时段至多接待一单，不改变其所属柜台或原有权限。"
        ),
    }


def _counter_brief(conn: sqlite3.Connection, counter_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, code, name FROM counters WHERE id = ?", (counter_id,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "code": row["code"], "name": row["name"]}


# --------------------------------------------------------------------------
# Booking-side capacity checks (called inside services' transaction)
# --------------------------------------------------------------------------


def assert_booking_fits(
    conn: sqlite3.Connection, slot: sqlite3.Row, new_booking_counter: str
) -> dict[str, Any]:
    """Every active, un-checked-in booking in the slot must fit distinct people.

    Includes the booking being confirmed (its counter is supplied separately,
    because its row does not exist yet).  Never cancels an existing booking.
    """
    bookings = active_slot_bookings(conn, slot["id"])
    checkins = slot_checkins(conn, slot["id"])
    pending = [b for b in bookings if b["id"] not in checkins]
    occupied = {row["staff_id"] for row in checkins.values()}
    roster = roster_map(conn, slot["id"])

    # Represent the prospective booking as a plain dict-shaped row.
    prospective = {
        "id": -1,
        "counter_id": new_booking_counter,
        "seq": float("inf"),
    }
    _, kept = _match_bookings([*pending, prospective], roster, occupied)  # type: ignore[list-item]
    if not any(b["id"] == -1 for b in kept):
        raise api_error(
            409,
            "slot_capacity_unavailable",
            "该时段无法再为这名认领者安排不会冲突的值班人；未取消任何既有预约",
            slot=slot_brief(slot),
            active_unreported_bookings=len(pending),
            available_reception_people=len(
                {sid for sid in roster if sid not in occupied}
            ),
        )
    return {
        "slot": slot_brief(slot),
        "active_unreported_bookings": len(pending) + 1,
        "available_reception_people": len({sid for sid in roster if sid not in occupied}),
    }


def assert_checkin_fits(
    conn: sqlite3.Connection, slot: sqlite3.Row, booking: sqlite3.Row,
    reception_staff_id: str,
) -> dict[str, Any]:
    """Check that reserving this person leaves every other booking servable."""
    others = [
        b for b in active_slot_bookings(conn, slot["id"]) if b["id"] != booking["id"]
    ]
    checkins = slot_checkins(conn, slot["id"])
    others = [b for b in others if b["id"] not in checkins]
    occupied = {row["staff_id"] for row in checkins.values()}
    occupied.add(reception_staff_id)
    roster = roster_map(conn, slot["id"])
    _, kept = _match_bookings(others, roster, occupied)
    if len(kept) != len(others):
        raise api_error(
            409,
            "checkin_would_strand_other_bookings",
            "若由这名值班人接待，本时段其他未报到预约将无人可接；请改派其他值班人",
            slot=slot_brief(slot),
            other_unreported_bookings=len(others),
            could_receive=len(kept),
        )
    return {"other_unreported_bookings": len(others)}


# --------------------------------------------------------------------------
# Check-in
# --------------------------------------------------------------------------


def check_in(
    conn: sqlite3.Connection,
    item_id: str,
    booking_id: int,
    reception_staff_id: str,
    actor: sqlite3.Row,
) -> dict[str, Any]:
    from .services import assert_desk_can_handle_item

    with transaction(conn):
        item = get_item(conn, item_id)
        booking = conn.execute(
            "SELECT * FROM bookings WHERE id = ? AND item_id = ?", (booking_id, item_id)
        ).fetchone()
        if booking is None:
            raise api_error(404, "booking_not_found", "预约不存在")
        assert_desk_can_handle_item(conn, item, actor)

        sweep_expired_bookings(conn)
        booking = conn.execute(
            "SELECT b.*, s.ends_at AS slot_ends_at, s.starts_at AS slot_starts_at"
            " FROM bookings b LEFT JOIN slots s ON s.id = b.slot_id"
            " WHERE b.id = ?",
            (booking_id,),
        ).fetchone()
        if booking["status"] != "active":
            raise api_error(
                409, "booking_not_active", "该预约已结束，不能报到",
                booking_status=booking["status"], ended_reason=booking["ended_reason"],
            )
        if booking["slot_id"] is None:
            raise api_error(
                409, "slot_required",
                "旧预约尚未补约接待时段，不能报到；请先补约时段",
            )
        if booking["counter_id"] != actor["counter_id"]:
            raise api_error(
                403, "wrong_counter",
                "只有该预约所在保管柜台能登记实际接待人",
            )
        now = now_iso()
        slot = conn.execute(
            "SELECT * FROM slots WHERE id = ?", (booking["slot_id"],)
        ).fetchone()
        if not (slot["starts_at"] <= now < slot["ends_at"]):
            if slot["ends_at"] <= now:
                raise api_error(409, SLOT_ENDED, "该接待时段已结束，不能报到",
                                slot=slot_brief(slot))
            raise api_error(
                409, "slot_not_started", "该接待时段尚未开始，不能报到",
                slot=slot_brief(slot),
            )

        existing = conn.execute(
            "SELECT * FROM reception_checkins WHERE booking_id = ?", (booking_id,)
        ).fetchone()
        if existing is not None:
            raise api_error(
                409, "already_checked_in", "该预约已登记实际接待人",
                checkin=_checkin_view(conn, existing),
            )
        reception_staff = conn.execute(
            "SELECT * FROM staff WHERE id = ?", (reception_staff_id,)
        ).fetchone()
        if reception_staff is None:
            raise api_error(404, "staff_not_found", "实际接待的值班人不存在")
        roster = roster_map(conn, slot["id"])
        if (
            reception_staff_id not in roster
            or booking["counter_id"] not in roster[reception_staff_id]
        ):
            raise api_error(
                409, "not_on_counter_roster",
                "该值班人不在本时段本柜台的接待名单中",
                slot=slot_brief(slot), counter_id=booking["counter_id"],
            )
        if reception_staff_id in checked_in_staff(conn, slot["id"]):
            raise api_error(
                409, "reception_person_already_used",
                "该值班人在本时段已接待过一单，不能再用同一名额",
            )

        fits = assert_checkin_fits(conn, slot, booking, reception_staff_id)

        seq = next_seq(conn)
        at = now_iso()
        cur = conn.execute(
            "INSERT INTO reception_checkins(booking_id, slot_id, staff_id, counter_id,"
            " checked_in_at, registered_by, seq)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                booking_id,
                slot["id"],
                reception_staff_id,
                booking["counter_id"],
                at,
                actor["id"],
                seq,
            ),
        )
        checkin_row = conn.execute(
            "SELECT * FROM reception_checkins WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        audit(
            conn,
            f"staff:{actor['id']}",
            "booking_checked_in",
            f"booking={booking_id} slot={slot['id']} reception_staff={reception_staff_id}"
            f" counter={booking['counter_id']}",
        )
        return {
            "booking_id": booking_id,
            "item_id": item_id,
            "slot": slot_brief(slot),
            "counter": _counter_brief(conn, booking["counter_id"]),
            "reception_staff": _staff_brief(conn, reception_staff_id),
            "checked_in_at": at,
            "checkin": _checkin_view(conn, checkin_row),
            **fits,
            "note": (
                "报到后该值班人的本时段名额即被保留到时段结束，"
                "即使提前交付或预约后来取消也不会释放给他人。"
            ),
        }


def _staff_brief(conn: sqlite3.Connection, staff_id: str | None) -> dict[str, Any] | None:
    if staff_id is None:
        return None
    row = conn.execute("SELECT id, name FROM staff WHERE id = ?", (staff_id,)).fetchone()
    return None if row is None else {"id": row["id"], "name": row["name"]}


def _checkin_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    staff = _staff_brief(conn, row["staff_id"])
    return {
        "checkin_id": row["id"],
        "staff_id": row["staff_id"],
        "staff_name": None if staff is None else staff["name"],
        "checked_in_at": row["checked_in_at"],
        "registered_by": _staff_brief(conn, row["registered_by"]),
        "counter": _counter_brief(conn, row["counter_id"]),
    }


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


def booking_slot_brief(booking: sqlite3.Row) -> dict[str, Any] | None:
    if booking["slot_id"] is None:
        return None
    return {
        "slot_id": booking["slot_id"],
        "starts_at": booking["slot_starts_at"],
        "ends_at": booking["slot_ends_at"],
        "ended": booking["slot_ends_at"] is not None
        and booking["slot_ends_at"] <= now_iso(),
    }


def staff_booking_view(
    conn: sqlite3.Connection, booking: sqlite3.Row, *, now: str | None = None
) -> dict[str, Any]:
    checkin = conn.execute(
        "SELECT * FROM reception_checkins WHERE booking_id = ?", (booking["id"],)
    ).fetchone()
    delivery = conn.execute(
        "SELECT * FROM deliveries WHERE booking_id = ?", (booking["id"],)
    ).fetchone()
    status, ended_reason = effective_booking_ended(booking, now)
    claimant = conn.execute(
        "SELECT display_name FROM claimants WHERE id = ?", (booking["claimant_id"],)
    ).fetchone()
    item = conn.execute(
        "SELECT public_code, title FROM items WHERE id = ?", (booking["item_id"],)
    ).fetchone()
    return {
        "id": booking["id"],  # legacy alias kept for existing staff clients
        "booking_id": booking["id"],
        "item_id": booking["item_id"],
        "public_code": None if item is None else item["public_code"],
        "title": None if item is None else item["title"],
        "claimant_id": booking["claimant_id"],
        "claimant_display_name": None if claimant is None else claimant["display_name"],
        "counter": _counter_brief(conn, booking["counter_id"])
        if booking["counter_id"]
        else None,
        "slot": booking_slot_brief(booking),
        "status": status,
        "created_at": booking["created_at"],
        "ended_at": booking["ended_at"],
        "ended_reason": ended_reason,
        "checkin": None if checkin is None else _checkin_view(conn, checkin),
        "delivery": None
        if delivery is None
        else {
            "delivery_id": delivery["id"],
            "delivered_at": delivery["delivered_at"],
            "delivered_by": _staff_brief(conn, delivery["delivered_by"]),
            "reception_staff": _staff_brief(conn, delivery["reception_staff_id"]),
        },
    }


def slot_bookings_view(
    conn: sqlite3.Connection, slot_ref: str, counter_ref: str | None = None
) -> dict[str, Any]:
    slot = resolve_slot(conn, slot_ref, create=False)
    counter_id = None
    if counter_ref is not None:
        row = conn.execute(
            "SELECT * FROM counters WHERE id = ? OR code = ?", (counter_ref, counter_ref)
        ).fetchone()
        if row is None:
            raise api_error(404, "counter_not_found", "柜台不存在", counter_ref=counter_ref)
        counter_id = row["id"]
    now = now_iso()
    rows = conn.execute(
        "SELECT b.*, s.starts_at AS slot_starts_at, s.ends_at AS slot_ends_at"
        " FROM bookings b LEFT JOIN slots s ON s.id = b.slot_id"
        " WHERE b.slot_id = ? ORDER BY b.seq, b.id",
        (slot["id"],),
    ).fetchall()

    grouped: dict[str | None, list[dict[str, Any]]] = {}
    for booking in rows:
        if counter_id is not None and booking["counter_id"] != counter_id:
            continue
        grouped.setdefault(booking["counter_id"], []).append(
            staff_booking_view(conn, booking, now=now)
        )

    counters = []
    for cid, bookings in sorted(grouped.items(), key=lambda kv: (kv[0] or "",)):
        counters.append(
            {
                "counter": _counter_brief(conn, cid) if cid else None,
                "bookings": bookings,
                "counts": {
                    "active": sum(1 for b in bookings if b["status"] == "active"),
                    "checked_in": sum(1 for b in bookings if b["checkin"] is not None),
                    "delivered": sum(1 for b in bookings if b["delivery"] is not None),
                    "cancelled": sum(1 for b in bookings if b["status"] == "cancelled"),
                    "expired": sum(1 for b in bookings if b["status"] == "expired"),
                },
            }
        )
    return {
        "slot": slot_brief(slot, now),
        "roster": roster_view(conn, slot)["entries"],
        "counters": counters,
        "booking_count": sum(len(c["bookings"]) for c in counters),
    }
