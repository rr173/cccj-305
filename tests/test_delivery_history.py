"""Post-delivery corrections keep the historical delivery intact."""

from __future__ import annotations

from .conftest import Staff

DETAILS = [
    {"code": "1", "label": "杯底刻字", "answer": "L.K. 2024"},
    {"code": "2", "label": "贴纸形状", "answer": "星星"},
    {"code": "3", "label": "内胆颜色", "answer": "奶油白"},
]


def answer(staff, headers, item, code, text):
    return staff.client.post(
        "/api/claimant/answers",
        json={"public_code": item["public_code"], "detail_code": code, "text": text},
        headers=headers,
    )


def detail_id(item, code):
    return next(d["id"] for d in item["details"] if d["code"] == code)


def delivered(staff: Staff):
    item = staff.create_item(DETAILS)
    a_id, a_headers = staff.create_claimant("认领者甲")
    answer(staff, a_headers, item, "1", "L.K. 2024")
    answer(staff, a_headers, item, "2", "星星")
    staff.post(f"/api/staff/items/{item['id']}/bookings", json={"claimant_id": a_id})
    r = staff.post(
        f"/api/staff/items/{item['id']}/deliveries", json={"claimant_id": a_id}
    )
    assert r.status_code == 201, r.text
    return item, a_id, a_headers, r.json()


def test_post_delivery_correction_keeps_history(staff: Staff):
    item, a_id, a_headers, delivery = delivered(staff)
    before = {e["detail_code"]: e for e in delivery["evidence"]}

    r = staff.post(
        f"/api/staff/items/{item['id']}/details/{detail_id(item, '1')}/correct",
        json={"answer": "L.K. 2025"},
    )
    assert r.status_code == 200
    # No active booking is affected by a post-delivery correction.
    assert r.json()["invalidated_bookings"] == []

    after = staff.get(f"/api/staff/items/{item['id']}/delivery").json()
    assert after["delivery_id"] == delivery["delivery_id"]
    assert after["claimant_id"] == a_id
    assert after["delivered_at"] == delivery["delivered_at"]
    assert after["private_clue_count"] == 2
    assert after["has_later_invalidated_evidence"] is True

    rows = {e["detail_code"]: e for e in after["evidence"]}
    assert rows["1"]["answer_at_delivery"] == "L.K. 2024"
    assert rows["1"]["submitted_text"] == "L.K. 2024"
    assert rows["1"]["later_invalidated"] is True
    assert rows["1"]["current_answer"] == "L.K. 2025"
    assert rows["2"]["later_invalidated"] is False
    # Historical values are unchanged.
    assert rows["1"]["answer_at_delivery"] == before["1"]["answer_at_delivery"]
    assert rows["2"]["answer_at_delivery"] == before["2"]["answer_at_delivery"]

    # Claimant sees the same frozen delivery record.
    own = staff.client.get(
        f"/api/claimant/items/{item['public_code']}/status", headers=a_headers
    ).json()
    assert own["delivered_to_you"] is True


def test_second_delivery_attempt_after_correction_rejected(staff: Staff):
    item, a_id, _, delivery = delivered(staff)
    staff.post(
        f"/api/staff/items/{item['id']}/details/{detail_id(item, '1')}/correct",
        json={"answer": "L.K. 2025"},
    )
    staff.post(
        f"/api/staff/items/{item['id']}/details/{detail_id(item, '2')}/correct",
        json={"answer": "六角星"},
    )
    r = staff.post(
        f"/api/staff/items/{item['id']}/deliveries", json={"claimant_id": a_id}
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "already_delivered"
    # Still exactly one delivery, unchanged.
    view = staff.get(f"/api/staff/items/{item['id']}/delivery").json()
    assert view["delivery_id"] == delivery["delivery_id"]
    assert view["delivered_at"] == delivery["delivered_at"]


def test_public_detail_shows_corrected_answer_after_publication(staff: Staff):
    item = staff.create_item(DETAILS)
    d1 = detail_id(item, "1")
    staff.post(f"/api/staff/items/{item['id']}/details/{d1}/publish")
    staff.post(
        f"/api/staff/items/{item['id']}/details/{d1}/correct",
        json={"answer": "L.K. 2025"},
    )
    public = staff.client.get(f"/api/public/items/{item['public_code']}").json()
    row = next(d for d in public["public_details"] if d["code"] == "1")
    assert row["answer"] == "L.K. 2025"

    # Publishing twice is idempotent and does not make it secret again.
    again = staff.post(f"/api/staff/items/{item['id']}/details/{d1}/publish")
    assert again.status_code == 200
    assert again.json()["already_public"] is True
