"""Counter stocktakes and discrepancy review.

Covers:

* one open round per counter, own-counter staff only, admin visibility;
* counting never locks registration / dispatch / receipt / claims;
* scans are classified by the book state at adoption time (seen vs.
  off-counter incl. unknown codes) and never move custody;
* idempotent scans by ``request_no``; content conflict; scans refused after
  close while original results remain retrievable;
* close freezes a sealed settlement from the objects claimable at that
  moment; a seen scan only counts for the same continuous on-shelf stay;
* confirmed losses cancel still-usable bookings and block claim handling,
  keeping check-ins; recovery lifts the block without restoring old bookings;
* review only by another staff member of the counter and only while the same
  stay is still open; reviews/finds are appended and visible to staff only;
* confirmed losses are not re-reported as a new shortage in a later round;
* no contradictory results when settlement, loss, recovery and claims race.
"""

from __future__ import annotations

import sqlite3

import pytest

from .conftest import Staff, current_slot_start, setUpReception

DETAILS = [
    {"code": "1", "label": "杯底刻字", "answer": "L.K. 2024"},
    {"code": "2", "label": "贴纸形状", "answer": "星星"},
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def make_counter(staff: Staff, code: str, name: str) -> dict:
    r = staff.post("/api/staff/counters", json={"code": code, "name": name})
    assert r.status_code == 201, r.text
    return r.json()


def counter_staff(staff: Staff, counter_ref: str, name: str) -> Staff:
    r = staff.post(
        "/api/staff/operators", json={"name": name, "counter_id": counter_ref}
    )
    assert r.status_code == 201, r.text
    return Staff(staff.client, r.json()["token"], r.json()["staff_id"])


def make_item(desk: Staff, title: str = "深灰色保温杯") -> dict:
    return desk.create_item(DETAILS, title=title)


def make_eligible(desk: Staff, item: dict, name: str = "认领者甲") -> tuple[str, dict]:
    cid, headers = desk.create_claimant(name)
    for code, text in (("1", "L.K. 2024"), ("2", "星星")):
        r = desk.client.post(
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


def open_round(desk: Staff) -> dict:
    r = desk.post("/api/staff/stocktakes")
    assert r.status_code == 201, r.text
    return r.json()


def scan(desk: Staff, round_id: str, request_no: str, item_ref: str):
    return desk.post(
        f"/api/staff/stocktakes/{round_id}/scans",
        json={"request_no": request_no, "item_ref": item_ref},
    )


def close(desk: Staff, round_id: str):
    return desk.post(f"/api/staff/stocktakes/{round_id}/close")


def review(desk: Staff, round_id: str, item_ref: str, outcome: str):
    return desk.post(
        f"/api/staff/stocktakes/{round_id}/items/{item_ref}/review",
        json={"outcome": outcome},
    )


def find(desk: Staff, round_id: str, item_ref: str):
    return desk.post(
        f"/api/staff/stocktakes/{round_id}/items/{item_ref}/find", json={}
    )


def settlement(desk: Staff, round_id: str) -> dict:
    r = desk.get(f"/api/staff/stocktakes/{round_id}/settlement")
    assert r.status_code == 200, r.text
    return r.json()


def dispatch(desk: Staff, box_no: str, dest: str, items: list[str]):
    return desk.post(
        "/api/staff/boxes",
        json={
            "box_no": box_no,
            "destination_counter_id": dest,
            "item_ids": items,
        },
    )


def receive(desk: Staff, box_no: str, labels: list[dict]):
    return desk.post(
        f"/api/staff/boxes/{box_no}/receipts", json={"scanned_labels": labels}
    )


def label(box_no: str, item: dict) -> dict:
    return {"box_no": box_no, "item_code": item["public_code"]}


def active_booking(desk: Staff, item_id: str) -> list[dict]:
    rows = desk.get(f"/api/staff/items/{item_id}/bookings").json()["bookings"]
    return [b for b in rows if b["status"] == "active"]


def item_ids(rows: list[dict]) -> list[str]:
    return [r["item_id"] for r in rows]


def setup_reception(desk: Staff, person: Staff, counter_code: str = "MAIN") -> str:
    slot = current_slot_start()
    setUpReception(desk, slot, [(person, counter_code)])
    return slot


# --------------------------------------------------------------------------
# Round lifecycle and access
# --------------------------------------------------------------------------


def test_one_open_round_per_counter_and_scoped_visibility(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    east1 = counter_staff(staff, "EAST", "东员甲")
    east2 = counter_staff(staff, "EAST", "东员乙")
    west = counter_staff(staff, "WEST", "西员")

    rnd = open_round(east1)
    assert rnd["counter"]["code"] == "EAST"
    assert rnd["status"] == "open"
    assert rnd["opened_by"]["id"] == east1.staff_id

    # A second open round is refused; no silent second row.
    r = east2.post("/api/staff/stocktakes")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "open_round_exists"

    # MAIN can hold its own open round independently.
    main_round = open_round(staff)
    assert main_round["counter"]["code"] == "MAIN"

    # Same-counter staff view each other's rounds; outsiders and claimants do not.
    assert east2.get(f"/api/staff/stocktakes/{rnd['round_id']}").status_code == 200
    r = west.get(f"/api/staff/stocktakes/{rnd['round_id']}")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "wrong_counter"
    # Admins may inspect every counter's round (management audit).
    assert staff.get(f"/api/staff/stocktakes/{rnd['round_id']}").status_code == 200

    r = east1.get("/api/staff/stocktakes?counter=WEST")
    assert r.status_code == 403

    # Claimants never reach these routes.
    _, claimant_headers = east1.create_claimant("认领者甲")
    r = east1.client.get(
        f"/api/staff/stocktakes/{rnd['round_id']}", headers=claimant_headers
    )
    assert r.status_code == 401


# --------------------------------------------------------------------------
# Scan classification + idempotency
# --------------------------------------------------------------------------


def test_scan_classification_by_book_state_and_idempotency(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    east = counter_staff(staff, "EAST", "东员")
    west = counter_staff(staff, "WEST", "西员")

    here = make_item(east, "东门在柜的杯")
    elsewhere = make_item(staff, "总台的伞")
    rnd = open_round(east)
    rid = rnd["round_id"]

    # Claimable here -> seen.
    r = scan(east, rid, "req-1", here["public_code"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["classification"] == "seen"
    assert body["reason"] == "claimable_at_counter"
    assert body["item_id"] == here["id"]
    assert body["replayed"] is False

    # Same request_no, same content -> original result, 200.
    r = scan(east, rid, "req-1", here["id"])
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert r.json()["scan_id"] == body["scan_id"]
    assert r.json()["classification"] == "seen"

    # Same request_no, different content -> rejected, nothing overwritten.
    r = scan(east, rid, "req-1", elsewhere["public_code"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "scan_content_conflict"

    # Unknown code is accepted and recorded as an off-counter scan.
    r = scan(east, rid, "req-unknown", "LF-NOSUCH")
    assert r.status_code == 201, r.text
    assert r.json()["classification"] == "off_counter"
    assert r.json()["reason"] == "unknown_code"
    assert r.json()["item_id"] is None

    # An object held by another counter is off-counter here, and its custody
    # does not move.
    r = scan(east, rid, "req-other", elsewhere["public_code"])
    assert r.json()["classification"] == "off_counter"
    assert r.json()["reason"] == "at_other_counter"
    loc = staff.get(f"/api/staff/items/{elsewhere['id']}/location").json()
    assert loc["custody_state"] == "at_counter"
    assert loc["holding_counter"]["code"] == "MAIN"

    # In transit / missing / delivered are all off-counter.
    in_transit = make_item(staff, "在途件")
    assert dispatch(staff, "B1", "WEST", [in_transit["id"]]).status_code == 201
    r = scan(east, rid, "req-transit", in_transit["public_code"])
    assert r.json()["reason"] == "in_transit"

    short = make_item(staff, "缺件件")
    assert dispatch(staff, "B2", "WEST", [short["id"]]).status_code == 201
    receive(west, "B2", [])  # signs everything missing
    r = scan(east, rid, "req-short", short["public_code"])
    assert r.json()["reason"] == "missing"

    delivered = make_item(staff, "已交付件")
    from app.db import connect

    conn = connect(staff.client.app.state.db_path)
    try:
        conn.execute(
            "UPDATE items SET status = 'delivered' WHERE id = ?", (delivered["id"],)
        )
    finally:
        conn.close()
    r = scan(east, rid, "req-delivered", delivered["public_code"])
    assert r.json()["reason"] == "already_delivered"

    view = east.get(f"/api/staff/stocktakes/{rid}").json()
    assert view["scan_counts"] == {
        "total": 6,
        "seen": 1,
        "off_counter": 5,
    }


# --------------------------------------------------------------------------
# Closing and the sealed settlement
# --------------------------------------------------------------------------


def test_close_settles_at_close_time_and_seals(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    east = counter_staff(staff, "EAST", "东员")

    seen_item = make_item(east, "扫到的杯")
    missed_item = make_item(east, "漏扫的伞")
    later_item = make_item(east, "关轮前新登记的包")
    other_counter_item = make_item(staff, "总台的帽")

    rid = open_round(east)["round_id"]
    assert scan(east, rid, "r-seen", seen_item["public_code"]).status_code == 201
    # Off-counter scans are preserved in the settlement.
    assert scan(east, rid, "r-other", other_counter_item["public_code"]).status_code == 201
    assert scan(east, rid, "r-ghost", "LF-GHOST1").status_code == 201

    r = close(east, rid)
    assert r.status_code == 200, r.text
    closed = r.json()
    assert closed["already_closed"] is False
    assert closed["sealed"] is True
    assert closed["counts"] == {
        "claimable": 3,
        "seen": 1,
        "pending_review": 2,
        "off_counter_scans": 2,
    }
    assert item_ids(closed["seen_items"]) == [seen_item["id"]]
    assert set(item_ids(closed["pending_review_items"])) == {
        missed_item["id"],
        later_item["id"],
    }
    assert {s["request_no"] for s in closed["off_counter_scans"]} == {
        "r-other",
        "r-ghost",
    }

    # Closing again is idempotent and does not rewrite the list.
    again = close(east, rid).json()
    assert again["already_closed"] is True
    assert item_ids(again["items"]) == item_ids(closed["items"])

    # New scans are refused after close...
    r = scan(east, rid, "r-after", seen_item["public_code"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "round_closed"
    # ...but the original result is still retrievable by request number,
    # including a replay submission.
    r = east.get(f"/api/staff/stocktakes/{rid}/scans/r-seen")
    assert r.status_code == 200
    assert r.json()["classification"] == "seen"
    assert r.json()["round_closed"] is True
    r = scan(east, rid, "r-seen", seen_item["public_code"])
    assert r.status_code == 200
    assert r.json()["replayed"] is True

    # Later operational changes do not rewrite the sealed settlement.
    assert dispatch(east, "SEAL-B", "MAIN", [missed_item["id"]]).status_code == 201
    after = settlement(east, rid)
    assert item_ids(after["pending_review_items"]) == item_ids(
        closed["pending_review_items"]
    )

    # The settlement table itself is append-only at the database level.
    from app.db import connect

    conn = connect(staff.client.app.state.db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE stocktake_settlement_items SET classification = 'seen'"
                " WHERE round_id = ?",
                (rid,),
            )
    finally:
        conn.close()


def test_settlement_requires_closed_round(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    east = counter_staff(staff, "EAST", "东员")
    rid = open_round(east)["round_id"]
    r = east.get(f"/api/staff/stocktakes/{rid}/settlement")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "round_not_closed"


# --------------------------------------------------------------------------
# Continuous stay rules
# --------------------------------------------------------------------------


def test_seen_requires_the_current_continuous_stay(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    east = counter_staff(staff, "EAST", "东员")
    west = counter_staff(staff, "WEST", "西员")

    travelled = make_item(east, "离柜又回来的杯")
    rid = open_round(east)["round_id"]
    # Scanned while on its first stay at EAST.
    assert scan(east, rid, "r1", travelled["public_code"]).json()["classification"] == "seen"

    # Leaves EAST -> WEST, comes back to EAST: a new continuous stay begins.
    assert dispatch(east, "T1", "WEST", [travelled["id"]]).status_code == 201
    assert receive(west, "T1", [label("T1", travelled)]).status_code == 201
    assert dispatch(west, "T2", "EAST", [travelled["id"]]).status_code == 201
    assert receive(east, "T2", [label("T2", travelled)]).status_code == 201

    # Newly registered after the round opened, never scanned.
    new_item = make_item(east, "关轮前新登记的包")

    closed = close(east, rid).json()
    # The old scan belongs to the previous stay and cannot mark the return.
    assert item_ids(closed["seen_items"]) == []
    assert set(item_ids(closed["pending_review_items"])) == {
        travelled["id"],
        new_item["id"],
    }

    # A fresh round with a fresh scan does count.
    rid2 = open_round(east)["round_id"]
    assert scan(east, rid2, "r2", travelled["public_code"]).json()["classification"] == "seen"
    closed2 = close(east, rid2).json()
    assert travelled["id"] in item_ids(closed2["seen_items"])


def test_seen_then_leaving_is_not_a_shortage(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    east = counter_staff(staff, "EAST", "东员")

    item = make_item(east, "扫到后正常调走的杯")
    rid = open_round(east)["round_id"]
    assert scan(east, rid, "r1", item["public_code"]).json()["classification"] == "seen"
    # Dispatched away while the round is still open; counting must not lock it.
    assert dispatch(east, "MOVE", "WEST", [item["id"]]).status_code == 201

    closed = close(east, rid).json()
    # No longer claimable here at close -> neither seen nor a new shortage.
    assert closed["counts"]["claimable"] == 0
    assert item_ids(closed["items"]) == []
    assert closed["counts"]["seen"] == 0
    assert closed["counts"]["pending_review"] == 0


def test_counting_does_not_lock_registration_dispatch_and_claims(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    east = counter_staff(staff, "EAST", "东员")
    west = counter_staff(staff, "WEST", "西员")

    rid = open_round(east)["round_id"]

    # Registration works during the round.
    fresh = make_item(east, "盘点中登记的包")
    # Dispatch works during the round.
    moving = make_item(east, "盘点中调走的伞")
    assert dispatch(east, "D1", "WEST", [moving["id"]]).status_code == 201
    assert receive(west, "D1", [label("D1", moving)]).status_code == 201

    # Booking and delivery also work; use an item that stays at EAST.
    claimable = make_item(east, "盘点中可领取的杯")
    cid, headers = make_eligible(east, claimable)
    slot = current_slot_start()
    setUpReception(staff, slot, [(east, "EAST")])
    r = east.book(claimable["id"], cid, slot)
    assert r.status_code == 201, r.text
    r = east.checkin(claimable["id"], r.json()["booking_id"], east.staff_id)
    assert r.status_code == 201, r.text
    assert east.deliver(claimable["id"], cid).status_code == 201

    closed = close(east, rid).json()
    # Only the freshly-registered item remains; it was never scanned.
    assert item_ids(closed["items"]) == [fresh["id"]]
    assert closed["counts"]["pending_review"] == 1


# --------------------------------------------------------------------------
# Review authority and loss confirmation
# --------------------------------------------------------------------------


def test_review_must_be_another_staff_member_and_only_pending(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    closer = counter_staff(staff, "EAST", "关轮员")
    reviewer = counter_staff(staff, "EAST", "复核员")

    seen_item = make_item(closer, "扫到的杯")
    lost_item = make_item(closer, "待复核的伞")

    rid = open_round(closer)["round_id"]
    scan(closer, rid, "r1", seen_item["public_code"])
    close(closer, rid)

    # The closer cannot review their own round.
    r = review(closer, rid, lost_item["id"], "loss_confirmed")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "reviewer_is_closer"

    # A seen item is not a discrepancy to review.
    r = review(reviewer, rid, seen_item["id"], "loss_confirmed")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "item_already_seen"

    # A second, valid review records the outcome and cannot repeat.
    r = review(reviewer, rid, lost_item["id"], "confirmed_at_counter")
    assert r.status_code == 200, r.text
    assert r.json()["outcome"] == "confirmed_at_counter"
    assert r.json()["settlement_item"]["review"]["outcome"] == "confirmed_at_counter"
    r = review(reviewer, rid, lost_item["id"], "confirmed_at_counter")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "already_reviewed"

    # Staff of another counter cannot act on this round at all.
    make_counter(staff, "WEST", "西门台")
    west = counter_staff(staff, "WEST", "西员")
    other_lost = make_item(closer, "另一件待复核")
    rid3 = open_round(closer)["round_id"]
    close(closer, rid3)
    r = review(west, rid3, other_lost["id"], "loss_confirmed")
    assert r.status_code == 403


def test_loss_confirmation_cancels_bookings_and_blocks_claim_handling(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    closer = counter_staff(staff, "EAST", "关轮员")
    reviewer = counter_staff(staff, "EAST", "复核员")

    item = make_item(closer, "盘亏的杯")
    cid, claimant_headers = make_eligible(closer, item)
    slot = current_slot_start()
    setUpReception(staff, slot, [(closer, "EAST")])
    booking = closer.book(item["id"], cid, slot)
    assert booking.status_code == 201, booking.text
    booking_id = booking.json()["booking_id"]
    # The claimant reports on time (check-in already happened before the loss).
    assert closer.checkin(item["id"], booking_id, closer.staff_id).status_code == 201

    rid = open_round(closer)["round_id"]
    close(closer, rid)  # item was not scanned -> pending_review
    r = review(reviewer, rid, item["id"], "loss_confirmed")
    assert r.status_code == 200, r.text
    assert r.json()["cancelled_booking_ids"] == [booking_id]

    # The still-usable booking was cancelled with the loss reason; the earlier
    # check-in remains on record.
    rows = closer.get(f"/api/staff/items/{item['id']}/bookings").json()["bookings"]
    row = next(b for b in rows if b["booking_id"] == booking_id)
    assert row["status"] == "cancelled"
    assert row["ended_reason"] == "inventory_loss"
    assert row["checkin"] is not None

    # The claimant sees only the cancellation reason for their own booking.
    status = closer.client.get(
        f"/api/claimant/items/{item['public_code']}/status", headers=claimant_headers
    ).json()
    assert status["booking"]["status"] == "cancelled"
    assert status["booking"]["ended_reason"] == "inventory_loss"
    assert status["booking"]["ended_reason_message"]

    # Every further claim step is blocked until a find is registered.
    r = closer.book(item["id"], cid, slot)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "item_confirmed_lost"
    assert closer.checkin(item["id"], booking_id, closer.staff_id).status_code == 409
    assert closer.deliver(item["id"], cid).status_code == 409
    r = dispatch(closer, "LOSS-B", "WEST", [item["id"]])
    assert r.status_code == 409
    assert r.json()["detail"]["problems"][0]["code"] == "item_confirmed_lost"
    loc = closer.get(f"/api/staff/items/{item['id']}/location").json()
    assert loc["reason_code"] == "confirmed_lost"
    assert loc["claim_processing_available"] is False
    # It is also not claimable, so a new round must not report it as a shortage.
    rid2 = open_round(closer)["round_id"]
    closed2 = close(closer, rid2).json()
    assert item_ids(closed2["items"]) == []


def test_loss_block_can_be_reviewed_into_place_then_recovered(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    closer = counter_staff(staff, "EAST", "关轮员")
    reviewer = counter_staff(staff, "EAST", "复核员")

    item = make_item(closer, "先盘亏后找回的伞")
    cid, headers = make_eligible(closer, item)

    rid = open_round(closer)["round_id"]
    close(closer, rid)
    assert review(reviewer, rid, item["id"], "loss_confirmed").status_code == 200

    # Find before any review was possible is rejected; find is an explicit
    # counter action that clears the block.
    r = find(reviewer, rid, item["id"])
    assert r.status_code == 201, r.text
    assert r.json()["old_bookings_restored"] is False
    assert find(reviewer, rid, item["id"]).status_code == 409

    # Once cleared, ordinary claim handling works again with the SAME rules,
    # but the old (nonexistent/cancelled) bookings are not restored.
    loc = closer.get(f"/api/staff/items/{item['id']}/location").json()
    assert loc["claim_processing_available"] is True
    r = closer.book(item["id"], cid, current_slot_start())
    assert r.status_code == 409  # no roster yet, not because of the loss
    assert r.json()["detail"]["code"] != "item_confirmed_lost"

    # Recovery is a new continuous stay: a later round must scan it again.
    rid2 = open_round(closer)["round_id"]
    closed2 = close(closer, rid2).json()
    assert item_ids(closed2["items"]) == [item["id"]]
    assert closed2["items"][0]["classification"] == "pending_review"


# --------------------------------------------------------------------------
# Review is only valid while the same stay is open
# --------------------------------------------------------------------------


def test_review_after_stay_ended_is_refused(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    closer = counter_staff(staff, "EAST", "关轮员")
    reviewer = counter_staff(staff, "EAST", "复核员")
    west = counter_staff(staff, "WEST", "西员")

    item = make_item(closer, "结算后离柜的杯")
    rid = open_round(closer)["round_id"]
    close(closer, rid)  # pending review

    # The item leaves EAST after the settlement is sealed.
    assert dispatch(closer, "AFTER", "WEST", [item["id"]]).status_code == 201
    assert receive(west, "AFTER", [label("AFTER", item)]).status_code == 201

    for outcome in ("loss_confirmed", "confirmed_at_counter"):
        r = review(reviewer, rid, item["id"], outcome)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "stay_ended_after_settlement"

    # The sealed list still shows it as pending; historical facts are intact.
    body = settlement(reviewer, rid)
    row = next(i for i in body["items"] if i["item_id"] == item["id"])
    assert row["classification"] == "pending_review"
    assert row["review"] is None
    assert row["review_permitted"]["confirm_loss"] is False


# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------


def test_discrepancies_are_counter_private(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    closer = counter_staff(staff, "EAST", "关轮员")
    reviewer = counter_staff(staff, "EAST", "复核员")
    west = counter_staff(staff, "WEST", "西员")

    item = make_item(closer, "盘亏的私密杯子")
    cid, claimant_headers = make_eligible(closer, item)
    rid = open_round(closer)["round_id"]
    close(closer, rid)
    assert review(reviewer, rid, item["id"], "loss_confirmed").status_code == 200
    assert find(reviewer, rid, item["id"]).status_code == 201

    # Staff of the counter can see review and find records.
    body = settlement(closer, rid)
    row = next(i for i in body["items"] if i["item_id"] == item["id"])
    assert row["review"]["outcome"] == "loss_confirmed"
    assert row["review"]["reviewer"]["id"] == reviewer.staff_id
    assert row["find"]["find_id"]
    assert row["find"]["found_by"]["id"] == reviewer.staff_id

    # Another counter's staff cannot read it at all.
    for path in (
        f"/api/staff/stocktakes/{rid}",
        f"/api/staff/stocktakes/{rid}/settlement",
        f"/api/staff/stocktakes/{rid}/scans",
    ):
        assert west.get(path).status_code == 403

    # The claimant's own status shows only a cancellation reason, never the
    # stocktake record, settlement, review or find internals.
    status = closer.client.get(
        f"/api/claimant/items/{item['public_code']}/status", headers=claimant_headers
    )
    assert status.status_code == 200
    text = status.text
    for leaked in (rid, "stocktake", "settlement", "pending_review",
                   "loss_confirmed", "review", "find"):
        assert leaked not in text
    # Public notice board does not expose stocktake information either.
    public = closer.client.get(f"/api/public/items/{item['public_code']}")
    assert rid not in public.text
    assert "stocktake" not in public.text


# --------------------------------------------------------------------------
# Migration of stays for pre-existing items
# --------------------------------------------------------------------------


def test_existing_on_shelf_items_get_a_stay_on_migration(tmp_path):
    from app.db import connect, init_db

    db = connect(tmp_path / "legacy.db")
    init_db(db)
    db.execute(
        "INSERT INTO staff(id, name, token_hash, is_admin, counter_id, created_at)"
        " VALUES ('op','老员工','h',1,'cnt_main','2026-01-01T00:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO items(id, public_code, title, public_description, found_at,"
        " found_location, status, custody_state, custody_counter_id, created_by,"
        " created_at, updated_at)"
        " VALUES ('i-old','LF-OLD01','旧物','d','2026-01-01','大厅','held',"
        "'at_counter','cnt_main','op','2026-01-01T00:00:00+00:00',"
        "'2026-01-01T00:00:00+00:00')"
    )
    # Re-run migration/init as a real upgrade would.
    init_db(db)
    stays = db.execute(
        "SELECT counter_id, ended_at FROM item_counter_stays WHERE item_id='i-old'"
    ).fetchall()
    assert len(stays) == 1
    assert stays[0]["counter_id"] == "cnt_main"
    assert stays[0]["ended_at"] is None
    db.close()


# --------------------------------------------------------------------------
# Concurrency-ish consistency: no contradictory states
# --------------------------------------------------------------------------


def test_find_and_review_do_not_leave_contradictions(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    closer = counter_staff(staff, "EAST", "关轮员")
    reviewer = counter_staff(staff, "EAST", "复核员")

    item = make_item(closer, "并发复核的杯")
    rid = open_round(closer)["round_id"]
    close(closer, rid)

    # Find before a loss review is meaningless.
    r = find(reviewer, rid, item["id"])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_confirmed_loss"

    assert review(reviewer, rid, item["id"], "loss_confirmed").status_code == 200
    # A second reviewer cannot flip the outcome once recorded.
    r = review(closer, rid, item["id"], "confirmed_at_counter")
    assert r.status_code in (403, 409)

    # Recovery clears the block; a subsequent review is refused as reviewed.
    assert find(reviewer, rid, item["id"]).status_code == 201
    r = review(reviewer, rid, item["id"], "loss_confirmed")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "already_reviewed"

    # Item is claimable and has a live stay; no residual loss block.
    conn = __import__("app.db", fromlist=["connect"]).connect(
        staff.client.app.state.db_path
    )
    try:
        row = conn.execute(
            "SELECT inventory_loss_review_id FROM items WHERE id = ?", (item["id"],)
        ).fetchone()
        assert row["inventory_loss_review_id"] is None
        open_stays = conn.execute(
            "SELECT COUNT(*) AS n FROM item_counter_stays"
            " WHERE item_id = ? AND ended_at IS NULL",
            (item["id"],),
        ).fetchone()["n"]
        assert open_stays == 1
    finally:
        conn.close()
