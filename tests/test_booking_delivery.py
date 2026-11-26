"""Bookings, staff selection, cancellation, and delivery."""

from __future__ import annotations

from .conftest import Staff

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


def test_booking_requires_eligibility_and_one_at_a_time(staff: Staff):
    item = staff.create_item(DETAILS)
    a_id, a_headers = staff.create_claimant("认领者甲")
    b_id, b_headers = staff.create_claimant("认领者乙")

    r = staff.post(
        f"/api/staff/items/{item['id']}/bookings", json={"claimant_id": a_id}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_eligible"

    make_eligible(staff, a_headers, item)
    make_eligible(staff, b_headers, item, "L.K. 2024", "星星")

    r = staff.post(
        f"/api/staff/items/{item['id']}/bookings", json={"claimant_id": a_id}
    )
    assert r.status_code == 201, r.text
    assert r.json()["item_status"] == "held"  # booking never moves the object

    r = staff.post(
        f"/api/staff/items/{item['id']}/bookings", json={"claimant_id": b_id}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "item_already_booked"

    # Staff may switch only after cancelling the first booking.
    booking_id = staff.get(f"/api/staff/items/{item['id']}/bookings").json()["bookings"][0]["id"]
    assert (
        staff.post(
            f"/api/staff/items/{item['id']}/bookings/{booking_id}/cancel"
        ).status_code
        == 200
    )
    r = staff.post(
        f"/api/staff/items/{item['id']}/bookings", json={"claimant_id": b_id}
    )
    assert r.status_code == 201


def test_correction_cancels_booking_and_rebook_selection(staff: Staff):
    item = staff.create_item(DETAILS)
    a_id, a_headers = staff.create_claimant("认领者甲")
    b_id, b_headers = staff.create_claimant("认领者乙")
    make_eligible(staff, a_headers, item)
    make_eligible(staff, b_headers, item)

    first = staff.post(
        f"/api/staff/items/{item['id']}/bookings", json={"claimant_id": a_id}
    )
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
    r = staff.post(
        f"/api/staff/items/{item['id']}/bookings", json={"claimant_id": b_id}
    )
    assert r.status_code == 201, r.text
    assert r.json()["claimant_id"] == b_id


def test_changed_answer_also_cancels_booking(staff: Staff):
    item = staff.create_item(DETAILS)
    a_id, a_headers = staff.create_claimant("认领者改答")
    make_eligible(staff, a_headers, item)
    staff.post(f"/api/staff/items/{item['id']}/bookings", json={"claimant_id": a_id})

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

    # No booking yet.
    r = staff.post(
        f"/api/staff/items/{item['id']}/deliveries", json={"claimant_id": a_id}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_booked"

    staff.post(f"/api/staff/items/{item['id']}/bookings", json={"claimant_id": a_id})

    # Wrong claimant for the booking.
    r = staff.post(
        f"/api/staff/items/{item['id']}/deliveries", json={"claimant_id": b_id}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "booking_mismatch"

    # Correct claimant.
    r = staff.post(
        f"/api/staff/items/{item['id']}/deliveries", json={"claimant_id": a_id}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["claimant_id"] == a_id
    assert body["private_clue_count"] == 2
    assert body["has_later_invalidated_evidence"] is False

    # No second delivery, even to another person.
    r = staff.post(
        f"/api/staff/items/{item['id']}/deliveries", json={"claimant_id": b_id}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "already_delivered"

    public = staff.client.get(f"/api/public/items/{item['public_code']}").json()
    assert public["status"] == "delivered"


def test_evidence_invalid_at_delivery_time_blocks_delivery(staff: Staff):
    """Bypass auto-cancellation via a direct DB tweak to prove the re-check."""
    item = staff.create_item(DETAILS)
    a_id, a_headers = staff.create_claimant("认领者甲")
    make_eligible(staff, a_headers, item)
    staff.post(f"/api/staff/items/{item['id']}/bookings", json={"claimant_id": a_id})

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

    r = staff.post(
        f"/api/staff/items/{item['id']}/deliveries", json={"claimant_id": a_id}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "evidence_invalid"
    bookings = staff.get(f"/api/staff/items/{item['id']}/bookings").json()["bookings"]
    assert bookings[0]["status"] == "cancelled"


def test_delivery_after_correction_requires_new_valid_answers(staff: Staff):
    item = staff.create_item(DETAILS)
    a_id, a_headers = staff.create_claimant("认领者甲")
    make_eligible(staff, a_headers, item)
    staff.post(f"/api/staff/items/{item['id']}/bookings", json={"claimant_id": a_id})

    staff.post(
        f"/api/staff/items/{item['id']}/details/{detail_id(item, '1')}/correct",
        json={"answer": "L.K.2025"},
    )
    # Booking was auto-cancelled; no delivery possible.
    r = staff.post(
        f"/api/staff/items/{item['id']}/deliveries", json={"claimant_id": a_id}
    )
    assert r.status_code == 409
