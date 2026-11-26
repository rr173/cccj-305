"""Bookings, staff selection, cancellation, reception check-in and delivery."""

from __future__ import annotations

from .conftest import Staff, current_slot_start, eligible_claimant, setUpReception

DETAILS = [
    {"code": "1", "label": "杯底刻字", "answer": "L.K. 2024"},
    {"code": "2", "label": "贴纸形状", "answer": "星星"},
    {"code": "3", "label": "内胆颜色", "answer": "奶油白"},
]


def answer(staff: Staff, headers: dict, item: dict, code: str, text: str):
    return staff.client.post(
        "/api/claimant/answers",
        json={"public_code": item["public_code"], "detail_code": code, "text": text},
        headers=headers,
    )


def make_eligible(staff: Staff, headers: dict, item: dict, first="L.K. 2024", second="星星"):
    answer(staff, headers, item, "1", first)
    answer(staff, headers, item, "2", second)


def detail_id(item: dict, code: str) -> str:
    return next(d["id"] for d in item["details"] if d["code"] == code)


def test_booking_requires_eligibility_slot_and_one_at_a_time(staff: Staff):
    item = staff.create_item(DETAILS)
    a_id, a_headers = staff.create_claimant("认领者甲")
    b_id, b_headers = staff.create_claimant("认领者乙")

    # A slot is mandatory for new bookings.
    r = staff.book(item["id"], a_id)
    assert r.status_code == 422

    # An arbitrary, non-aligned slot start is rejected (30-minute UTC grid).
    r = staff.book(item["id"], a_id, "2026-11-26T03:17:00Z")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "slot_start_not_aligned"

    r = staff.book(item["id"], a_id, current_slot_start())
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_eligible"

    make_eligible(staff, a_headers, item)
    make_eligible(staff, b_headers, item, "L.K. 2024", "星星")

    duty = staff.create_operator("MAIN", "总台值班甲")
    setUpReception(staff, current_slot_start(), [(duty, "MAIN")])

    r = staff.book(item["id"], a_id, current_slot_start())
    assert r.status_code == 201, r.text
    assert r.json()["item_status"] == "held"  # booking never moves the object
    assert r.json()["slot"]["slot_id"].startswith("slot_")
    assert r.json()["counter"]["code"] == "MAIN"
    booking_id = r.json()["booking_id"]

    r = staff.book(item["id"], b_id, current_slot_start())
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "item_already_booked"

    # Staff may switch only after cancelling the first booking.
    assert (
        staff.post(
            f"/api/staff/items/{item['id']}/bookings/{booking_id}/cancel"
        ).status_code
        == 200
    )
    r = staff.book(item["id"], b_id, current_slot_start())
    assert r.status_code == 201


def test_correction_cancels_booking_and_rebook_selection(staff: Staff):
    item = staff.create_item(DETAILS)
    a_id, a_headers = staff.create_claimant("认领者甲")
    b_id, b_headers = staff.create_claimant("认领者乙")
    make_eligible(staff, a_headers, item)
    make_eligible(staff, b_headers, item)

    duty = staff.create_operator("MAIN", "总台值班")
    setUpReception(staff, current_slot_start(), [(duty, "MAIN")])

    first = staff.book(item["id"], a_id, current_slot_start())
    assert first.status_code == 201

    r = staff.post(
        f"/api/staff/items/{item['id']}/details/{detail_id(item, '2')}/correct",
        json={"answer": "五角星"},
    )
    assert r.status_code == 200
    assert r.json()["invalidated_bookings"] == [first.json()["booking_id"]]
    assert r.json()["remaining_active_bookings"] == []

    # Booking is cancelled, and it does NOT come back automatically.
    assert (
        staff.client.get(
            f"/api/claimant/items/{item['public_code']}/status", headers=a_headers
        ).json()["booking"]["status"]
        == "cancelled"
    )
    assert (
        staff.client.get(
            f"/api/claimant/items/{item['public_code']}/status", headers=a_headers
        ).json()["booking"]["ended_reason"]
        == "eligibility_lost"
    )

    # Claimant A re-answers correctly -> eligible again, but not rebooked.
    make_eligible(staff, a_headers, item, "L.K. 2024", "五角星")
    assert (
        staff.client.get(
            f"/api/claimant/items/{item['public_code']}/status", headers=a_headers
        ).json()["booking"]["status"]
        == "cancelled"
    )
    active = staff.get(f"/api/staff/items/{item['id']}/bookings").json()["bookings"]
    assert all(b["status"] != "active" for b in active)

    # Staff may now choose B, once B's answer reflects the corrected answer.
    answer(staff, b_headers, item, "2", "五角星")
    r = staff.book(item["id"], b_id, current_slot_start())
    assert r.status_code == 201, r.text
    assert r.json()["claimant_id"] == b_id


def test_changed_answer_also_cancels_booking(staff: Staff):
    item = staff.create_item(DETAILS)
    a_id, a_headers = staff.create_claimant("认领者改答")
    make_eligible(staff, a_headers, item)
    duty = staff.create_operator("MAIN", "值班")
    setUpReception(staff, current_slot_start(), [(duty, "MAIN")])
    staff.book(item["id"], a_id, current_slot_start())

    answer(staff, a_headers, item, "2", "月亮")  # now wrong -> only 1 valid clue
    bookings = staff.get(f"/api/staff/items/{item['id']}/bookings").json()["bookings"]
    assert bookings[0]["status"] == "cancelled"
    assert bookings[0]["ended_reason"] == "eligibility_lost"


def test_delivery_guards(staff: Staff):
    item = staff.create_item(DETAILS)
    a_id, a_headers = staff.create_claimant("认领者甲")
    b_id, b_headers = staff.create_claimant("认领者乙")
    make_eligible(staff, a_headers, item)
    make_eligible(staff, b_headers, item)

    duty = staff.create_operator("MAIN", "总台值班甲")
    setUpReception(staff, current_slot_start(), [(duty, "MAIN")])

    # No booking yet.
    r = staff.deliver(item["id"], a_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_booked"

    booking = staff.book(item["id"], a_id, current_slot_start()).json()

    # Delivery before reception check-in is refused.
    r = staff.deliver(item["id"], a_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_checked_in"

    # Wrong claimant for the booking.
    r = staff.deliver(item["id"], b_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "booking_mismatch"

    # Register the actual reception person, then deliver.
    r = staff.checkin(item["id"], booking["booking_id"], duty.staff_id)
    assert r.status_code == 201, r.text
    assert r.json()["reception_staff"]["id"] == duty.staff_id

    r = staff.deliver(item["id"], a_id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["claimant_id"] == a_id
    assert body["private_clue_count"] == 2
    assert body["has_later_invalidated_evidence"] is False
    assert body["reception_staff"]["id"] == duty.staff_id
    assert body["slot"]["slot_id"] == booking["slot"]["slot_id"]

    # No second delivery, even to another person.
    r = staff.deliver(item["id"], b_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "already_delivered"

    public = staff.client.get(f"/api/public/items/{item['public_code']}").json()
    assert public["status"] == "delivered"


def test_evidence_invalid_at_delivery_time_blocks_delivery(staff: Staff):
    """Bypass auto-cancellation via a direct DB tweak to prove the re-check."""
    item = staff.create_item(DETAILS)
    a_id, a_headers = staff.create_claimant("认领者甲")
    make_eligible(staff, a_headers, item)
    duty = staff.create_operator("MAIN", "值班")
    setUpReception(staff, current_slot_start(), [(duty, "MAIN")])
    booking = staff.book(item["id"], a_id, current_slot_start()).json()
    assert staff.checkin(item["id"], booking["booking_id"], duty.staff_id).status_code == 201

    from app.db import connect

    conn = connect(staff.client.app.state.db_path)
    try:
        # Make the claimant's latest answer stale without touching eligibility
        # bookkeeping (simulates any future path that skips cancellation).
        conn.execute(
            "UPDATE details SET correction_epoch = correction_epoch + 1 WHERE item_id = ?",
            (item["id"],),
        )
    finally:
        conn.close()

    r = staff.deliver(item["id"], a_id)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "evidence_invalid"
    bookings = staff.get(f"/api/staff/items/{item['id']}/bookings").json()["bookings"]
    assert bookings[0]["status"] == "cancelled"
    # The check-in and its seat reservation remain on record.
    assert bookings[0]["checkin"] is not None


def test_delivery_after_correction_requires_new_valid_answers(staff: Staff):
    item = staff.create_item(DETAILS)
    a_id, a_headers = staff.create_claimant("认领者甲")
    make_eligible(staff, a_headers, item)
    duty = staff.create_operator("MAIN", "值班")
    setUpReception(staff, current_slot_start(), [(duty, "MAIN")])
    staff.book(item["id"], a_id, current_slot_start())

    staff.post(
        f"/api/staff/items/{item['id']}/details/{detail_id(item, '1')}/correct",
        json={"answer": "L.K.2025"},
    )
    # Booking was auto-cancelled; no check-in or delivery possible.
    r = staff.deliver(item["id"], a_id)
    assert r.status_code == 409
