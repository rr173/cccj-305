"""Reception staffing: slots, rosters, shared capacity, check-in, shrinking.

Covers the staffing guarantee end to end:

* slots are 30 minutes on the UTC full/half-hour grid;
* one duty person may be rostered at several counters but is one seat;
* a new booking is confirmed only if every un-checked-in booking in the slot
  (including it) can be matched to distinct, available rostered people;
* no old booking is ever cancelled to make room for a new one;
* a check-in requires the person on that counter's roster, unused in the
  slot, and must not strand other un-checked-in bookings;
* a check-in keeps its seat until the slot ends, even after early delivery
  or cancellation;
* shrinking a roster keeps check-ins and as many earliest-confirmed
  un-checked-in bookings as fit; the rest are cancelled one-way;
* slot end expires undelivered bookings without blocking later slots;
* staff can query per-counter slot results; claimants only see their own
  counter, slot and cancellation/expiry reason, never the roster;
* legacy slotless bookings are preserved and need a slot before check-in.
"""

from __future__ import annotations

from .conftest import Staff, current_slot_start, next_slot_start, past_slot_start

DETAILS = [
    {"code": "1", "label": "杯底刻字", "answer": "L.K. 2024"},
    {"code": "2", "label": "贴纸形状", "answer": "星星"},
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def make_item(staff: Staff, counter_ref: str = "MAIN") -> tuple[dict, Staff]:
    """Create an item at a counter and return (item, its desk operator)."""
    desk = staff if counter_ref == "MAIN" else staff.create_operator(counter_ref, "登记员")
    return desk.create_item(DETAILS), desk


def roster(staff: Staff, slot: str, entries: list[tuple[Staff, list[str]]]) -> dict:
    payload = [
        {"staff_id": person.staff_id, "counter_ids": counters}
        for person, counters in entries
    ]
    r = staff.put(f"/api/staff/slots/{slot}/roster", json={"entries": payload})
    assert r.status_code == 200, r.text
    return r.json()


def eligible(staff: Staff, item: dict, name: str) -> tuple[str, dict]:
    cid, headers = staff.create_claimant(name)
    for code, text in (("1", "L.K. 2024"), ("2", "星星")):
        r = staff.client.post(
            "/api/claimant/answers",
            json={
                "public_code": item["public_code"],
                "detail_code": code,
                "text": text,
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
    return cid, headers


def bookings(staff: Staff, item_id: str) -> list[dict]:
    return staff.get(f"/api/staff/items/{item_id}/bookings").json()["bookings"]


def booking_of(staff: Staff, item_id: str, booking_id: int) -> dict:
    return next(b for b in bookings(staff, item_id) if b["booking_id"] == booking_id)


def active_of(staff: Staff, item_id: str) -> list[dict]:
    return [b for b in bookings(staff, item_id) if b["status"] == "active"]


def book_ok(desk: Staff, item: dict, claimant_id: str, slot: str) -> dict:
    r = desk.book(item["id"], claimant_id, slot)
    assert r.status_code == 201, r.text
    return r.json()


def checkin_ok(desk: Staff, item: dict, booking_id: int, person: Staff) -> dict:
    r = desk.checkin(item["id"], booking_id, person.staff_id)
    assert r.status_code == 201, r.text
    return r.json()


def claimant_status(staff: Staff, headers: dict, item: dict) -> dict:
    return staff.client.get(
        f"/api/claimant/items/{item['public_code']}/status", headers=headers
    ).json()


# --------------------------------------------------------------------------
# Slot grid and roster basics
# --------------------------------------------------------------------------


def test_slot_must_be_utc_half_hour_and_not_ended(staff: Staff):
    duty = staff.create_operator("MAIN", "值班员")
    roster(staff, current_slot_start(), [(duty, ["MAIN"])])
    roster(staff, next_slot_start(), [(duty, ["MAIN"])])
    item, desk = make_item(staff)
    cid, _ = eligible(staff, item, "认领者甲")

    for bad in ("2026-11-26T03:17:00Z", "2026-11-26T03:30:45Z", "2026-11-26 03:30"):
        r = desk.book(item["id"], cid, bad)
        assert r.status_code == 422, bad
        assert r.json()["detail"]["code"] in {
            "slot_start_not_aligned",
            "slot_start_needs_timezone",
            "invalid_slot_start",
        }

    r = desk.book(item["id"], cid, past_slot_start())
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "slot_ended"

    ok = book_ok(desk, item, cid, next_slot_start())
    assert ok["slot"]["slot_id"].startswith("slot_")
    assert ok["slot"]["starts_at"].endswith(":00.000000+00:00")


def test_roster_entry_validation_and_replacement(staff: Staff):
    slot = current_slot_start()
    a = staff.create_operator("MAIN", "甲")
    b = staff.create_operator("MAIN", "乙")
    staff.create_counter("EAST", "东门台")

    view = roster(staff, slot, [(a, ["MAIN", "EAST"]), (b, ["MAIN"])])
    assert view["seat_count"] == 2  # two people, though a has two counters
    entries = {e["staff_id"]: e for e in view["entries"]}
    assert {c["code"] for c in entries[a.staff_id]["counters"]} == {"MAIN", "EAST"}

    replaced = roster(staff, slot, [(a, ["MAIN"])])
    assert replaced["version"] == 2
    assert {e["staff_id"] for e in replaced["entries"]} == {a.staff_id}

    r = staff.put(
        f"/api/staff/slots/{slot}/roster",
        json={"entries": [{"staff_id": a.staff_id, "counter_ids": ["MAIN", "MAIN"]}]},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "duplicate_roster_entry"

    r = staff.put(
        f"/api/staff/slots/{slot}/roster",
        json={"entries": [{"staff_id": "ghost", "counter_ids": ["MAIN"]}]},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "staff_not_found"

    r = staff.put(
        f"/api/staff/slots/{slot}/roster",
        json={"entries": [{"staff_id": a.staff_id, "counter_ids": ["NOPE"]}]},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "counter_not_found"

    # Only admins may set a roster; doing so never changes home counters.
    r = b.put(
        f"/api/staff/slots/{slot}/roster",
        json={"entries": [{"staff_id": b.staff_id, "counter_ids": ["MAIN"]}]},
    )
    assert r.status_code == 403
    assert b.get("/api/staff/me").json()["counter"]["code"] == "MAIN"


# --------------------------------------------------------------------------
# Capacity: shared seats, no stealing
# --------------------------------------------------------------------------


def test_one_person_at_several_counters_is_one_seat(staff: Staff):
    slot = next_slot_start()
    duty = staff.create_operator("MAIN", "共用值班")
    staff.create_counter("EAST", "东门台")
    roster(staff, slot, [(duty, ["MAIN", "EAST"])])

    main_item, main_desk = make_item(staff, "MAIN")
    east_item, east_desk = make_item(staff, "EAST")
    c1, _ = eligible(staff, main_item, "甲")
    c2, _ = eligible(staff, east_item, "乙")

    first = book_ok(main_desk, main_item, c1, slot)
    assert first["available_reception_people"] == 1

    # The same person appears on EAST's roster too, but the seat is taken.
    r = east_desk.book(east_item["id"], c2, slot)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "slot_capacity_unavailable"
    # The first booking was NOT cancelled to make room.
    assert {b["booking_id"] for b in active_of(staff, main_item["id"])} == {
        first["booking_id"]
    }
    assert active_of(staff, east_item["id"]) == []

    # Adding a second *person* makes both fit (still no old booking is lost).
    second_person = staff.create_operator("EAST", "东门第二值班")
    roster(staff, slot, [(duty, ["MAIN", "EAST"]), (second_person, ["EAST"])])
    again = book_ok(east_desk, east_item, c2, slot)
    assert {b["booking_id"] for b in active_of(staff, main_item["id"])} == {
        first["booking_id"]
    }
    assert {b["booking_id"] for b in active_of(staff, east_item["id"])} == {
        again["booking_id"]
    }


def test_new_booking_never_cancels_old_ones(staff: Staff):
    slot = next_slot_start()
    duty = staff.create_operator("MAIN", "值班")
    roster(staff, slot, [(duty, ["MAIN"])])
    item1, desk = make_item(staff)
    item2, _ = make_item(staff)
    c1, _ = eligible(staff, item1, "甲")
    c2, _ = eligible(staff, item2, "乙")

    first = book_ok(desk, item1, c1, slot)
    r = desk.book(item2["id"], c2, slot)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "slot_capacity_unavailable"
    assert {b["booking_id"] for b in active_of(staff, item1["id"])} == {
        first["booking_id"]
    }
    assert active_of(staff, item2["id"]) == []
    old = booking_of(staff, item1["id"], first["booking_id"])
    assert old["status"] == "active"
    assert old["slot"]["slot_id"] == first["slot"]["slot_id"]


# --------------------------------------------------------------------------
# Check-in
# --------------------------------------------------------------------------


def test_checkin_requires_roster_and_an_unused_seat(staff: Staff):
    slot = current_slot_start()
    duty_a = staff.create_operator("MAIN", "值班甲")
    duty_b = staff.create_operator("MAIN", "值班乙")
    outsider = staff.create_operator("MAIN", "名单外")
    staff.create_counter("EAST", "东门台")
    roster(staff, slot, [(duty_a, ["MAIN"]), (duty_b, ["MAIN"])])

    item, desk = make_item(staff)
    item2, _ = make_item(staff)
    c1, _ = eligible(staff, item, "甲")
    c2, _ = eligible(staff, item2, "乙")
    b1 = book_ok(desk, item, c1, slot)
    b2 = book_ok(desk, item2, c2, slot)

    # Not on the roster at all.
    r = desk.checkin(item["id"], b1["booking_id"], outsider.staff_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_on_counter_roster"

    # Rostered only at a different counter.
    roster(
        staff, slot, [(duty_a, ["MAIN"]), (duty_b, ["MAIN"]), (outsider, ["EAST"])]
    )
    r = desk.checkin(item["id"], b1["booking_id"], outsider.staff_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_on_counter_roster"

    # Same person cannot receive a second booking in the same slot.
    checkin_ok(desk, item, b1["booking_id"], duty_a)
    r = desk.checkin(item2["id"], b2["booking_id"], duty_a.staff_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "reception_person_already_used"
    # Another free rostered person can.
    checkin_ok(desk, item2, b2["booking_id"], duty_b)

    # Double check-in on the same booking is refused.
    r = desk.checkin(item2["id"], b2["booking_id"], duty_b.staff_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "already_checked_in"


def test_checkin_refused_when_it_would_strand_others(staff: Staff):
    """P1 is the only MAIN-capable person; taking the EAST booking at EAST
    would leave the MAIN booking with nobody, so it must be refused."""
    slot = current_slot_start()
    staff.create_counter("EAST", "东门台")
    p1 = staff.create_operator("MAIN", "值班一")
    p2 = staff.create_operator("EAST", "值班二")
    # p1 covers both counters; p2 covers only EAST.
    roster(staff, slot, [(p1, ["MAIN", "EAST"]), (p2, ["EAST"])])

    main_item, main_desk = make_item(staff, "MAIN")
    east_item, east_desk = make_item(staff, "EAST")
    c1, _ = eligible(staff, main_item, "甲")
    c2, _ = eligible(staff, east_item, "乙")
    bm = book_ok(main_desk, main_item, c1, slot)
    be = book_ok(east_desk, east_item, c2, slot)

    # A valid matching exists: MAIN->p1, EAST->p2.  But registering p1 for
    # the EAST booking would leave the MAIN booking unservable.
    r = east_desk.checkin(east_item["id"], be["booking_id"], p1.staff_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "checkin_would_strand_other_bookings"

    # The safe assignment works.
    checkin_ok(east_desk, east_item, be["booking_id"], p2)
    checkin_ok(main_desk, main_item, bm["booking_id"], p1)


# --------------------------------------------------------------------------
# Seat reservation persists
# --------------------------------------------------------------------------


def test_checkin_seat_survives_early_delivery_and_cancellation(staff: Staff):
    slot = current_slot_start()
    duty = staff.create_operator("MAIN", "值班")
    roster(staff, slot, [(duty, ["MAIN"])])
    item, desk = make_item(staff)
    cid, _ = eligible(staff, item, "甲")
    b1 = book_ok(desk, item, cid, slot)
    checkin_ok(desk, item, b1["booking_id"], duty)

    # Deliver early: the person's seat stays used until the slot ends.
    assert desk.deliver(item["id"], cid).status_code == 201
    item2, _ = make_item(staff)
    c2, _ = eligible(staff, item2, "乙")
    # No space even for a new booking (one seat, already used).
    r = desk.book(item2["id"], c2, slot)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "slot_capacity_unavailable"

    # Cancelling a checked-in booking also keeps the seat reserved.  A second
    # person lets us create the booking, then we shrink back to one person.
    other = staff.create_operator("MAIN", "值班二")
    roster(staff, slot, [(duty, ["MAIN"]), (other, ["MAIN"])])
    b2 = book_ok(desk, item2, c2, slot)
    assert desk.post(
        f"/api/staff/items/{item2['id']}/bookings/{b2['booking_id']}/cancel"
    ).status_code == 200
    # duty's seat remains used, so only `other` is free.
    item3, _ = make_item(staff)
    c3, _ = eligible(staff, item3, "丙")
    b3 = book_ok(desk, item3, c3, slot)
    r = desk.checkin(item3["id"], b3["booking_id"], duty.staff_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "reception_person_already_used"


# --------------------------------------------------------------------------
# Roster shrink
# --------------------------------------------------------------------------


def test_roster_shrink_keeps_checkins_and_earliest_confirmed(staff: Staff):
    slot = current_slot_start()
    a = staff.create_operator("MAIN", "值班甲")
    b = staff.create_operator("MAIN", "值班乙")
    c = staff.create_operator("MAIN", "值班丙")
    roster(staff, slot, [(a, ["MAIN"]), (b, ["MAIN"]), (c, ["MAIN"])])
    items = []
    desks = []
    for i in range(3):
        item, desk = make_item(staff)
        items.append(item)
        desks.append(desk)
    cids = [eligible(staff, item, f"认领者{i}")[0] for i, item in enumerate(items)]
    bids = [book_ok(desk, item, cid, slot)["booking_id"] for desk, item, cid in zip(desks, items, cids)]

    # Check in the *second* booking with b.
    checkin_ok(desks[1], items[1], bids[1], b)

    # Shrink to one person.  b's check-in keeps b's seat, so only one
    # un-checked-in booking survives: the earliest-confirmed (bid 0).
    view = roster(staff, slot, [(a, ["MAIN"])])
    assert set(view["cancelled_bookings"]) == {bids[2]}
    row = booking_of(staff, items[2]["id"], bids[2])
    assert row["status"] == "cancelled"
    assert row["ended_reason"] == "roster_changed"
    checked = booking_of(staff, items[1]["id"], bids[1])
    assert checked["status"] == "active"
    assert checked["checkin"]["staff_id"] == b.staff_id
    assert booking_of(staff, items[0]["id"], bids[0])["status"] == "active"

    # Restoring the roster does not resurrect the cancelled booking.
    roster(staff, slot, [(a, ["MAIN"]), (b, ["MAIN"])])
    assert booking_of(staff, items[2]["id"], bids[2])["status"] == "cancelled"


def test_roster_shrink_equal_candidates_keeps_earlier(staff: Staff):
    slot = next_slot_start()
    a = staff.create_operator("MAIN", "值班甲")
    b = staff.create_operator("MAIN", "值班乙")
    roster(staff, slot, [(a, ["MAIN"]), (b, ["MAIN"])])
    item1, desk = make_item(staff)
    item2, _ = make_item(staff)
    c1, _ = eligible(staff, item1, "甲")
    c2, _ = eligible(staff, item2, "乙")
    early = book_ok(desk, item1, c1, slot)["booking_id"]
    late = book_ok(desk, item2, c2, slot)["booking_id"]

    view = roster(staff, slot, [(a, ["MAIN"])])
    assert set(view["cancelled_bookings"]) == {late}
    assert booking_of(staff, item1["id"], early)["status"] == "active"


def test_clearing_roster_keeps_only_checkins(staff: Staff):
    slot = current_slot_start()
    a = staff.create_operator("MAIN", "值班")
    roster(staff, slot, [(a, ["MAIN"])])
    item, desk = make_item(staff)
    cid, _ = eligible(staff, item, "甲")
    bid = book_ok(desk, item, cid, slot)["booking_id"]
    checkin_ok(desk, item, bid, a)

    view = roster(staff, slot, [])
    assert view["seat_count"] == 0
    assert view["cancelled_bookings"] == []  # the only booking is checked in
    row = booking_of(staff, item["id"], bid)
    assert row["checkin"] is not None
    # Roster edits never make a checked-in valid appointment undeliverable.
    assert desk.deliver(item["id"], cid).status_code == 201


# --------------------------------------------------------------------------
# Slot end / expiry
# --------------------------------------------------------------------------


def test_slot_end_expires_and_does_not_block_later_slots(staff: Staff):
    a = staff.create_operator("MAIN", "值班")
    item, desk = make_item(staff)
    cid, _ = eligible(staff, item, "甲")

    # Normal booking into a past slot is refused outright.
    r = desk.book(item["id"], cid, past_slot_start())
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "slot_ended"

    # Simulate a booking made while a slot was open and has since ended.
    now_slot = current_slot_start()
    roster(staff, now_slot, [(a, ["MAIN"])])
    bid = book_ok(desk, item, cid, now_slot)["booking_id"]
    from datetime import datetime

    from app import scheduling
    from app.db import connect

    conn = connect(staff.client.app.state.db_path)
    try:
        start = datetime.fromisoformat(past_slot_start())
        past_slot = scheduling.get_or_create_slot(conn, start)
        conn.execute(
            "UPDATE bookings SET slot_id = ? WHERE id = ?", (past_slot["id"], bid)
        )
    finally:
        conn.close()

    future = current_slot_start()
    roster(staff, future, [(a, ["MAIN"])])
    rb = book_ok(desk, item, cid, future)  # the sweep expires the old booking
    old = booking_of(staff, item["id"], bid)
    assert old["status"] == "expired"
    assert old["ended_reason"] == "slot_ended"

    # Expired cannot be checked in or delivered...
    r = desk.checkin(item["id"], bid, a.staff_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "booking_not_active"
    r = desk.deliver(item["id"], cid)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_checked_in"

    # ...and it does not block the newly confirmed booking.
    checkin_ok(desk, item, rb["booking_id"], a)
    assert desk.deliver(item["id"], cid).status_code == 201


# --------------------------------------------------------------------------
# Staff / claimant views
# --------------------------------------------------------------------------


def test_staff_slot_view_lists_per_counter_results(staff: Staff):
    slot = current_slot_start()
    a = staff.create_operator("MAIN", "值班甲")
    b = staff.create_operator("MAIN", "值班乙")
    roster(staff, slot, [(a, ["MAIN"]), (b, ["MAIN"])])
    item1, desk = make_item(staff)
    item2, _ = make_item(staff)
    c1, _ = eligible(staff, item1, "甲")
    c2, _ = eligible(staff, item2, "乙")
    b1 = book_ok(desk, item1, c1, slot)
    b2 = book_ok(desk, item2, c2, slot)
    checkin_ok(desk, item1, b1["booking_id"], a)
    assert desk.deliver(item1["id"], c1).status_code == 201
    assert desk.post(
        f"/api/staff/items/{item2['id']}/bookings/{b2['booking_id']}/cancel"
    ).status_code == 200

    view = staff.get(f"/api/staff/slots/{slot}/bookings?counter=MAIN").json()
    block = next(c for c in view["counters"] if c["counter"]["code"] == "MAIN")
    assert block["counts"]["delivered"] == 1
    assert block["counts"]["cancelled"] == 1
    rows = {r["booking_id"]: r for r in block["bookings"]}
    assert rows[b1["booking_id"]]["delivery"]["delivery_id"] is not None
    assert rows[b1["booking_id"]]["checkin"]["staff_id"] == a.staff_id
    assert rows[b2["booking_id"]]["ended_reason"] == "staff_cancelled"

    whole = staff.get(f"/api/staff/slots/{slot}").json()
    assert whole["roster"]["seat_count"] == 2
    assert whole["bookings"]["booking_count"] == 2


def test_claimant_sees_own_counter_slot_and_reason_only(staff: Staff):
    slot = current_slot_start()
    a = staff.create_operator("MAIN", "值班甲")
    b = staff.create_operator("MAIN", "值班乙")
    roster(staff, slot, [(a, ["MAIN"]), (b, ["MAIN"])])
    item1, desk = make_item(staff)
    item2, _ = make_item(staff)
    c1, h1 = eligible(staff, item1, "甲")
    c2, h2 = eligible(staff, item2, "乙")
    b1 = book_ok(desk, item1, c1, slot)
    b2 = book_ok(desk, item2, c2, slot)

    body = claimant_status(staff, h1, item1)
    assert body["booking"]["counter"]["code"] == "MAIN"
    assert body["booking"]["slot"]["slot_id"] == b1["slot"]["slot_id"]
    assert body["booking"]["status"] == "active"
    text = staff.client.get(
        f"/api/claimant/items/{item1['public_code']}/status", headers=h1
    ).text
    for secret in ("值班甲", "值班乙", a.staff_id, b.staff_id, "roster", "seat_count"):
        assert secret not in text
    # Another claimant's booking is not exposed (own booking is id 1).
    assert f'"id": {b2["booking_id"]}' not in text and f'"id":{b2["booking_id"]}' not in text

    view = roster(staff, slot, [(a, ["MAIN"])])
    assert view["cancelled_bookings"] == [b2["booking_id"]]
    body2 = claimant_status(staff, h2, item2)
    assert body2["booking"]["status"] == "cancelled"
    assert body2["booking"]["ended_reason"] == "roster_changed"
    assert body2["booking"]["slot"]["slot_id"] == b2["slot"]["slot_id"]


# --------------------------------------------------------------------------
# Consistency with other workflows
# --------------------------------------------------------------------------


def test_dispatch_after_checkin_keeps_history_and_seat(staff: Staff):
    slot = current_slot_start()
    a = staff.create_operator("MAIN", "值班甲")
    b = staff.create_operator("MAIN", "值班乙")
    roster(staff, slot, [(a, ["MAIN"]), (b, ["MAIN"])])
    staff.create_counter("EAST", "东门台")
    item1, desk = make_item(staff)
    item2, _ = make_item(staff)
    c1, _ = eligible(staff, item1, "甲")
    c2, _ = eligible(staff, item2, "乙")
    b1 = book_ok(desk, item1, c1, slot)
    checkin_ok(desk, item1, b1["booking_id"], a)

    r = desk.post(
        "/api/staff/boxes",
        json={"box_no": "BX", "destination_counter_id": "EAST", "item_ids": [item1["id"]]},
    )
    assert r.status_code == 201, r.text
    row = booking_of(staff, item1["id"], b1["booking_id"])
    assert row["status"] == "cancelled"
    assert row["ended_reason"] == "item_dispatched"
    assert row["checkin"]["staff_id"] == a.staff_id

    b2 = book_ok(desk, item2, c2, slot)
    r = desk.checkin(item2["id"], b2["booking_id"], a.staff_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "reception_person_already_used"
    checkin_ok(desk, item2, b2["booking_id"], b)


def test_eligibility_loss_after_checkin_keeps_checkin_and_blocks_delivery(staff: Staff):
    slot = current_slot_start()
    a = staff.create_operator("MAIN", "值班")
    roster(staff, slot, [(a, ["MAIN"])])
    item, desk = make_item(staff)
    c1, _ = eligible(staff, item, "甲")
    b1 = book_ok(desk, item, c1, slot)
    checkin_ok(desk, item, b1["booking_id"], a)

    detail2 = next(d["id"] for d in item["details"] if d["code"] == "2")
    desk.post(
        f"/api/staff/items/{item['id']}/details/{detail2}/correct",
        json={"answer": "五角星"},
    )
    row = booking_of(staff, item["id"], b1["booking_id"])
    assert row["status"] == "cancelled"
    assert row["ended_reason"] == "eligibility_lost"
    assert row["checkin"] is not None
    assert desk.deliver(item["id"], c1).status_code == 409


# --------------------------------------------------------------------------
# Legacy migration / slotless bookings
# --------------------------------------------------------------------------


def test_legacy_slotless_booking_can_be_slotted_or_cancelled(staff: Staff):
    slot = current_slot_start()
    a = staff.create_operator("MAIN", "值班")
    roster(staff, slot, [(a, ["MAIN"])])
    item, desk = make_item(staff)
    c1, h1 = eligible(staff, item, "甲")
    bid = book_ok(desk, item, c1, slot)["booking_id"]

    from app.db import connect

    conn = connect(staff.client.app.state.db_path)
    try:
        conn.execute(
            "UPDATE bookings SET slot_id = NULL, counter_id = NULL WHERE id = ?", (bid,)
        )
    finally:
        conn.close()

    body = claimant_status(staff, h1, item)
    assert body["booking"] is not None
    assert body["booking"]["slot"] is None
    assert body["booking"]["status"] == "active"

    # Slotless cannot check in or deliver.
    r = desk.checkin(item["id"], bid, a.staff_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "slot_required"
    r = desk.deliver(item["id"], c1)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "slot_required"

    # Only the holding counter may assign the slot; the original claimant stays.
    staff.create_counter("EAST", "东门台")
    east_staff = staff.create_operator("EAST", "东员")
    r = east_staff.post(
        f"/api/staff/items/{item['id']}/bookings/{bid}/slot",
        json={"slot_start": slot},
    )
    assert r.status_code == 403

    r = desk.post(
        f"/api/staff/items/{item['id']}/bookings/{bid}/slot",
        json={"slot_start": slot},
    )
    assert r.status_code == 200, r.text
    assert r.json()["claimant_id"] == c1
    assert r.json()["slot"]["slot_id"].startswith("slot_")
    checkin_ok(desk, item, bid, a)
    assert desk.deliver(item["id"], c1).status_code == 201

    # A booking that already has a slot cannot be re-slotted.
    r = desk.post(
        f"/api/staff/items/{item['id']}/bookings/{bid}/slot",
        json={"slot_start": slot},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] in {"booking_not_active", "slot_already_assigned"}


def test_legacy_slotless_booking_can_be_cancelled(staff: Staff):
    slot = current_slot_start()
    a = staff.create_operator("MAIN", "值班")
    roster(staff, slot, [(a, ["MAIN"])])
    item, desk = make_item(staff)
    c1, h1 = eligible(staff, item, "甲")
    bid = book_ok(desk, item, c1, slot)["booking_id"]
    from app.db import connect

    conn = connect(staff.client.app.state.db_path)
    try:
        conn.execute("UPDATE bookings SET slot_id = NULL WHERE id = ?", (bid,))
    finally:
        conn.close()
    assert desk.post(
        f"/api/staff/items/{item['id']}/bookings/{bid}/cancel"
    ).status_code == 200
    assert claimant_status(staff, h1, item)["booking"]["ended_reason"] == "staff_cancelled"


def test_legacy_database_upgrades_and_preserves_records(tmp_path):
    """An old database upgrades cleanly and keeps old records readable."""
    from app.db import connect, init_db

    db = tmp_path / "legacy-slot.db"
    conn = connect(db)
    conn.executescript(
        """
        CREATE TABLE counters (id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, is_main INTEGER NOT NULL DEFAULT 0,
            created_by TEXT, created_at TEXT NOT NULL);
        CREATE TABLE staff (id TEXT PRIMARY KEY, name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE, is_admin INTEGER NOT NULL DEFAULT 0,
            counter_id TEXT NOT NULL DEFAULT 'cnt_main', created_at TEXT NOT NULL);
        CREATE TABLE claimants (id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
        CREATE TABLE items (id TEXT PRIMARY KEY, public_code TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL, public_description TEXT NOT NULL,
            found_at TEXT NOT NULL, found_location TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'held',
            custody_state TEXT NOT NULL DEFAULT 'at_counter',
            custody_counter_id TEXT, created_by TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE bookings (id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL, claimant_id TEXT NOT NULL,
            status TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
            ended_at TEXT, ended_reason TEXT, seq INTEGER NOT NULL);
        CREATE TABLE deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL UNIQUE, claimant_id TEXT NOT NULL,
            booked_by TEXT NOT NULL, delivered_by TEXT NOT NULL,
            delivered_at TEXT NOT NULL, private_clue_count INTEGER NOT NULL,
            eligibility_note TEXT NOT NULL, seq INTEGER NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO counters(id,code,name,is_main,created_at)"
        " VALUES ('cnt_main','MAIN','总台',1,'2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO staff(id,name,token_hash,is_admin,counter_id,created_at)"
        " VALUES ('s1','老值班','h1',1,'cnt_main','2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO claimants(id,display_name,token_hash,created_at)"
        " VALUES ('c1','认领者甲','h2','2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO items(id,public_code,title,public_description,found_at,found_location,"
        "status,custody_state,custody_counter_id,created_by,created_at,updated_at)"
        " VALUES ('i1','LF-OLD01','旧物','d','2026-01-01','大厅','held','at_counter',"
        "'cnt_main','s1','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO bookings(item_id,claimant_id,status,created_by,created_at,seq)"
        " VALUES ('i1','c1','active','s1','2026-01-01T00:00:00+00:00',1)"
    )
    conn.execute(
        "INSERT INTO items(id,public_code,title,public_description,found_at,found_location,"
        "status,custody_state,custody_counter_id,created_by,created_at,updated_at)"
        " VALUES ('i2','LF-OLD02','旧交付物','d','2026-01-02','大厅','delivered',"
        "'at_counter','cnt_main','s1','2026-01-02T00:00:00+00:00',"
        "'2026-01-02T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO deliveries(item_id,claimant_id,booked_by,delivered_by,delivered_at,"
        "private_clue_count,eligibility_note,seq)"
        " VALUES ('i2','c1','s1','s1','2026-01-05T00:00:00+00:00',2,'旧交付',2)"
    )
    init_db(conn)
    booking = conn.execute("SELECT * FROM bookings WHERE id = 1").fetchone()
    assert booking["status"] == "active"
    assert booking["slot_id"] is None
    assert booking["counter_id"] is None
    delivery = conn.execute("SELECT * FROM deliveries WHERE id = 1").fetchone()
    assert delivery["private_clue_count"] == 2
    assert delivery["slot_id"] is None
    assert delivery["reception_staff_id"] is None
    conn.close()


def test_delivery_after_slot_end_expires_and_commits(staff: Staff):
    """A checked-in booking whose slot ended can no longer be delivered; the
    expiry is persisted (it is not rolled back by the error response)."""
    slot = current_slot_start()
    a = staff.create_operator("MAIN", "值班")
    roster(staff, slot, [(a, ["MAIN"])])
    item, desk = make_item(staff)
    cid, _ = eligible(staff, item, "甲")
    bid = book_ok(desk, item, cid, slot)["booking_id"]
    checkin_ok(desk, item, bid, a)

    # Fast-forward the slot: mark it as already ended.
    from datetime import datetime, timedelta, timezone

    from app.db import connect

    conn = connect(staff.client.app.state.db_path)
    try:
        ended = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
            timespec="microseconds"
        )
        slot_id = conn.execute(
            "SELECT id FROM slots WHERE starts_at = ?",
            (slot.replace("+00:00", ".000000+00:00"),),
        ).fetchone()["id"]
        conn.execute("UPDATE slots SET ends_at = ? WHERE id = ?", (ended, slot_id))
    finally:
        conn.close()

    r = desk.deliver(item["id"], cid)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "slot_ended"
    row = booking_of(staff, item["id"], bid)
    assert row["status"] == "expired"
    assert row["ended_reason"] == "slot_ended"
    # Check-in history remains, but the booking cannot be revived.
    assert row["checkin"] is not None
    r = desk.checkin(item["id"], bid, a.staff_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "booking_not_active"
