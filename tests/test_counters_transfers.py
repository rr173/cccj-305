"""Cross-counter dispatch and open-box receipt rules.

Covers:

* counters and staff membership (upgrade default = 总台/main desk);
* registrar-counter custody for new items;
* dispatch validation, atomic all-or-nothing failure, booking cancellation;
* idempotent redelivery / content conflicts (order-insensitive);
* in-transit / missing / quarantined items cannot be booked or delivered;
* destination-only, exactly-once receipt with idempotent retry;
* unknown / old / duplicate / elsewhere-reported labels reject the WHOLE form;
* symmetric two-box reconciliation, whichever box is opened first;
* staff location view and privacy boundaries;
* legacy database migration onto the main desk.
"""

from __future__ import annotations

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


def counter_staff(staff: Staff, counter_ref: str, name: str = "驻台员") -> Staff:
    r = staff.post(
        "/api/staff/operators",
        json={"name": name, "counter_id": counter_ref},
    )
    assert r.status_code == 201, r.text
    return Staff(staff.client, r.json()["token"])


def answer(client, headers: dict, public_code: str, code: str, text: str):
    return client.post(
        "/api/claimant/answers",
        json={"public_code": public_code, "detail_code": code, "text": text},
        headers=headers,
    )


def make_eligible(client, headers: dict, public_code: str):
    answer(client, headers, public_code, "1", "L.K. 2024")
    answer(client, headers, public_code, "2", "星星")


def create_simple_item(staff: Staff, title: str = "深灰色保温杯") -> dict:
    return staff.create_item(
        DETAILS,
        title=title,
    )


def dispatch(staff: Staff, box_no: str, dest: str, item_refs: list[str]):
    return staff.post(
        "/api/staff/boxes",
        json={
            "box_no": box_no,
            "destination_counter_id": dest,
            "item_ids": item_refs,
        },
    )


def receive(staff: Staff, box_no: str, labels: list[dict] | None):
    return staff.post(
        f"/api/staff/boxes/{box_no}/receipts",
        json={"scanned_labels": labels or []},
    )


def label(box_no: str, code: str) -> dict:
    return {"box_no": box_no, "item_code": code}


def active_booking(staff: Staff, item_id: str):
    rows = staff.get(f"/api/staff/items/{item_id}/bookings").json()["bookings"]
    return [b for b in rows if b["status"] == "active"]


# --------------------------------------------------------------------------
# Counters / membership / upgrade default
# --------------------------------------------------------------------------


def test_admin_creates_counter_and_assigns_staff(staff: Staff):
    me = staff.get("/api/staff/me").json()
    assert me["counter"]["code"] == "MAIN"
    assert me["counter"]["is_main"] is True

    east = make_counter(staff, "EAST", "东门服务台")
    assert east["held_item_count"] == 0

    r = staff.post(
        "/api/staff/operators",
        json={"name": "小东", "counter_id": "EAST"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["counter_id"] == east["id"]
    assert r.json()["counter"]["code"] == "EAST"
    east_staff = Staff(staff.client, r.json()["token"])
    assert east_staff.get("/api/staff/me").json()["counter"]["code"] == "EAST"

    # counter_id also accepts the opaque id.
    r = staff.post(
        "/api/staff/operators",
        json={"name": "小西", "counter_id": east["id"]},
    )
    assert r.status_code == 201

    # Omitted counter -> main desk.
    r = staff.post("/api/staff/operators", json={"name": "默认总台同事"})
    assert r.status_code == 201
    assert r.json()["counter"]["code"] == "MAIN"


def test_counter_code_unique_and_admin_can_reassign_staff(staff: Staff):
    east = make_counter(staff, "EAST", "东门台")
    r = staff.post("/api/staff/counters", json={"code": "EAST", "name": "重复台"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "counter_code_taken"

    # Operator starts at MAIN.
    r = staff.post("/api/staff/operators", json={"name": "调动员"})
    assert r.status_code == 201
    op_id = r.json()["staff_id"]
    op = Staff(staff.client, r.json()["token"])
    assert op.get("/api/staff/me").json()["counter"]["code"] == "MAIN"

    # Admin reassigns to EAST (ref accepts code).
    r = staff.post(f"/api/staff/operators/{op_id}/counter", json={"counter_id": "EAST"})
    assert r.status_code == 200, r.text
    assert r.json()["counter_id"] == east["id"]
    assert op.get("/api/staff/me").json()["counter"]["code"] == "EAST"

    # Operator list exposes membership.
    listing = staff.get("/api/staff/operators").json()["operators"]
    me = next(o for o in listing if o["staff_id"] == op_id)
    assert me["counter"]["code"] == "EAST"

    # A plain operator cannot reassign anyone.
    r = staff.post("/api/staff/operators", json={"name": "普通员"})
    plain = Staff(staff.client, r.json()["token"])
    r = plain.post(
        f"/api/staff/operators/{op_id}/counter", json={"counter_id": "MAIN"}
    )
    assert r.status_code == 403


def test_non_admin_cannot_create_counter_or_assign(staff: Staff):
    r = staff.post("/api/staff/operators", json={"name": "普通员"})
    assert r.status_code == 201
    plain = Staff(staff.client, r.json()["token"])

    r = plain.post("/api/staff/counters", json={"code": "X", "name": "私台"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "forbidden"

    # A plain operator also cannot create further operators.
    r = plain.post("/api/staff/operators", json={"name": "二号"})
    assert r.status_code == 403


def test_new_items_kept_by_registrar_counter(staff: Staff):
    east = make_counter(staff, "EAST", "东门台")
    east_staff = counter_staff(staff, "EAST", "东员")

    main_item = create_simple_item(staff, "总台登记的伞")
    east_item = east_staff.create_item([], title="东门登记的杯")

    assert main_item["holding_counter"]["code"] == "MAIN"
    assert main_item["custody_state"] == "at_counter"
    assert main_item["claim_processing_available"] is True
    assert east_item["holding_counter"]["id"] == east["id"]

    inv = staff.get(f"/api/staff/counters/EAST/inventory").json()
    assert [i["item_id"] for i in inv["held_items"]] == [east_item["id"]]
    assert inv["count"] == 1


def test_legacy_database_migrates_everyone_to_main_desk(tmp_path):
    """An old-schema database is upgraded: old staff/items -> 总台."""
    from app.db import connect, init_db

    db_path = tmp_path / "legacy.db"
    conn = connect(db_path)
    conn.executescript(
        """
        CREATE TABLE staff (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
            is_admin INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE items (
            id TEXT PRIMARY KEY, public_code TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
            public_description TEXT NOT NULL, found_at TEXT NOT NULL,
            found_location TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'held',
            created_by TEXT NOT NULL REFERENCES staff(id),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO staff(id,name,token_hash,is_admin,created_at)"
        " VALUES ('s1','老值班','h',1,'2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO items(id,public_code,title,public_description,found_at,"
        "found_location,status,created_by,created_at,updated_at)"
        " VALUES ('i1','LF-OLD01','旧物','d','2026-01-01','大厅','held','s1',?,'x')",
        ("2026-01-01T00:00:00+00:00",),
    )
    init_db(conn)

    s = conn.execute("SELECT counter_id FROM staff WHERE id='s1'").fetchone()
    assert s["counter_id"] == "cnt_main"
    i = conn.execute(
        "SELECT custody_state, custody_counter_id FROM items WHERE id='i1'"
    ).fetchone()
    assert i["custody_state"] == "at_counter"
    assert i["custody_counter_id"] == "cnt_main"
    main = conn.execute("SELECT code,name FROM counters WHERE id='cnt_main'").fetchone()
    assert main["code"] == "MAIN"
    assert main["name"] == "总台"
    conn.close()

    # The upgraded database boots the API and the legacy staff token hash works
    # only if it matches; instead just confirm the app initializes cleanly.
    from app.main import create_app
    from fastapi.testclient import TestClient

    app = create_app(db_path)
    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200


# --------------------------------------------------------------------------
# Dispatch validation + atomicity
# --------------------------------------------------------------------------


def test_dispatch_happy_path_cancels_booking_and_enters_transit(staff: Staff):
    east = make_counter(staff, "EAST", "东门台")
    duty = staff.create_operator("MAIN", "总台值班")
    setUpReception(staff, current_slot_start(), [(duty, "MAIN")])
    item = create_simple_item(staff)
    claimant_id, headers = staff.create_claimant("认领者甲")
    make_eligible(staff.client, headers, item["public_code"])
    r = staff.book(item["id"], claimant_id, current_slot_start())
    assert r.status_code == 201
    booking_id = r.json()["booking_id"]

    r = dispatch(staff, "BOX-1", "EAST", [item["id"]])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["box_no"] == "BOX-1"
    assert body["status"] == "dispatched"
    assert body["sender_counter"]["code"] == "MAIN"
    assert body["destination_counter"]["code"] == "EAST"
    assert body["dispatched_by"]["name"] == "当班工作人员"
    assert body["manifest"][0]["result"] == "in_transit"
    assert body["replayed"] is False

    # Booking cancelled one-way with the transfer reason.
    bookings = staff.get(f"/api/staff/items/{item['id']}/bookings").json()["bookings"]
    b = next(b for b in bookings if b["id"] == booking_id)
    assert b["status"] == "cancelled"
    assert b["ended_reason"] == "item_dispatched"
    assert active_booking(staff, item["id"]) == []

    loc = staff.get(f"/api/staff/items/{item['id']}/location").json()
    assert loc["custody_state"] == "in_transit"
    assert loc["claim_processing_available"] is False
    assert loc["reason_code"] == "in_transit"
    assert loc["active_trip"]["box_no"] == "BOX-1"
    assert loc["active_trip"]["destination_counter"]["id"] == east["id"]

    # Item view reflects the same state.
    detail = staff.get(f"/api/staff/items/{item['id']}").json()
    assert detail["custody_state"] == "in_transit"
    assert detail["claim_processing_available"] is False


def test_dispatch_manifest_must_be_nonempty_and_unique(staff: Staff):
    item = create_simple_item(staff)
    # Empty list rejected by schema.
    r = staff.post(
        "/api/staff/boxes",
        json={"box_no": "B-EMPTY", "destination_counter_id": "EAST", "item_ids": []},
    )
    assert r.status_code == 422

    # Unknown destination.
    r = dispatch(staff, "B-X", "NOPE", [item["id"]])
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "counter_not_found"

    make_counter(staff, "EAST", "东门台")
    # Duplicate rows rejected.
    r = dispatch(staff, "B-DUP", "EAST", [item["id"], item["id"]])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "duplicate_box_item"

    # id + public code aliasing the same item is also a duplicate.
    r = dispatch(staff, "B-DUP2", "EAST", [item["id"], item["public_code"]])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "duplicate_box_item"

    # Same counter transfer rejected.
    r = dispatch(staff, "B-SELF", "MAIN", [item["id"]])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "same_counter_transfer"


def test_dispatch_is_atomic_when_one_item_cannot_go(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    east_staff = counter_staff(staff, "EAST", "东员")
    duty = staff.create_operator("MAIN", "总台值班")
    setUpReception(staff, current_slot_start(), [(duty, "MAIN")])

    good = create_simple_item(staff, "总台可发的杯")
    bad = east_staff.create_item([], title="东门的伞")  # not held by MAIN

    # Give the good item an active booking; it must survive the failed batch.
    cid, headers = staff.create_claimant("认领者甲")
    make_eligible(staff.client, headers, good["public_code"])
    r = staff.book(good["id"], cid, current_slot_start())
    assert r.status_code == 201

    r = dispatch(staff, "BOX-BAD", "EAST", [good["id"], bad["id"]])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "box_items_not_dispatchable"
    codes = {p["item_id"]: p["code"] for p in detail["problems"]}
    assert codes[bad["id"]] == "wrong_counter"

    # Box was never created.
    assert staff.get("/api/staff/boxes/BOX-BAD").status_code == 404
    # Nothing moved and no appointment was cancelled.
    assert active_booking(staff, good["id"]) != []
    loc = staff.get(f"/api/staff/items/{good['id']}/location").json()
    assert loc["custody_state"] == "at_counter"


def test_dispatch_idempotent_replay_and_content_conflict(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    i1 = create_simple_item(staff, "杯")
    i2 = staff.create_item([], title="伞")

    payload = lambda items: {  # noqa: E731
        "box_no": "BOX-IDEM",
        "destination_counter_id": "EAST",
        "item_ids": items,
    }
    r1 = staff.post("/api/staff/boxes", json=payload([i1["id"], i2["id"]]))
    assert r1.status_code == 201
    first = r1.json()

    # Same business content, reversed order -> original record, HTTP 200.
    r2 = staff.post("/api/staff/boxes", json=payload([i2["id"], i1["id"]]))
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert [m["item_id"] for m in r2.json()["manifest"]] == [
        m["item_id"] for m in first["manifest"]
    ]

    # Changed content cannot overwrite the box number.
    r3 = staff.post("/api/staff/boxes", json=payload([i1["id"]]))
    assert r3.status_code == 409
    assert r3.json()["detail"]["code"] == "box_no_content_conflict"

    # Exactly one box row with two items.
    boxes = staff.get("/api/staff/boxes").json()["boxes"]
    assert [b["box_no"] for b in boxes].count("BOX-IDEM") == 1
    view = staff.get("/api/staff/boxes/BOX-IDEM").json()
    assert view["item_count"] == 2


def test_item_cannot_be_put_in_two_boxes(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    item = create_simple_item(staff)

    assert dispatch(staff, "B1", "EAST", [item["id"]]).status_code == 201
    r = dispatch(staff, "B2", "WEST", [item["id"]])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "box_items_not_dispatchable"
    assert r.json()["detail"]["problems"][0]["code"] == "item_in_transit"


def test_in_transit_blocks_booking_and_delivery(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    duty = staff.create_operator("MAIN", "总台值班")
    setUpReception(staff, current_slot_start(), [(duty, "MAIN")])
    item = create_simple_item(staff)
    cid, headers = staff.create_claimant("认领者甲")
    make_eligible(staff.client, headers, item["public_code"])
    assert staff.book(item["id"], cid, current_slot_start()).status_code == 201
    dispatch(staff, "B", "EAST", [item["id"]])

    r = staff.book(item["id"], cid, current_slot_start())
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "item_in_transit"

    r = staff.deliver(item["id"], cid)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "item_in_transit"


# --------------------------------------------------------------------------
# Receipts: authority, once-only, replay, counts
# --------------------------------------------------------------------------


def test_only_destination_counter_receipts_once_with_replay(staff: Staff):
    east = make_counter(staff, "EAST", "东门台")
    east_staff = counter_staff(staff, "EAST", "东员")
    other_staff = counter_staff(staff, "MAIN", "总台另一人")
    item = create_simple_item(staff)
    dispatch(staff, "B", "EAST", [item["id"]])

    # Sender/main counter is not the destination.
    r = receive(other_staff, "B", [label("B", item["public_code"])])
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "not_destination_counter"

    # Destination receives.
    r = receive(east_staff, "B", [label("B", item["public_code"])])
    assert r.status_code == 201, r.text
    receipt = r.json()
    assert receipt["received_by"]["name"] == "东员"
    assert receipt["receiving_counter"]["id"] == east["id"]
    assert receipt["counts"]["arrived"] == 1
    assert receipt["counts"]["missing"] == 0

    # Same content retried (using internal id alias, reversed irrelevant here)
    # returns the original receipt with 200.
    r2 = receive(east_staff, "B", [{"box_no": "B", "item_id": item["id"]}])
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["receipt_id"] == receipt["receipt_id"]

    # Different content cannot overwrite the single receipt.
    r3 = receive(east_staff, "B", [])
    assert r3.status_code == 409
    assert r3.json()["detail"]["code"] == "receipt_content_conflict"

    # Box query exposes sender, actual receiver and count result.
    box = staff.get("/api/staff/boxes/B").json()
    assert box["status"] == "received"
    assert box["receipt"]["receipt_id"] == receipt["receipt_id"]
    assert box["receipt"]["counts"]["scanned"] == 1


def test_partial_scan_marks_arrivals_and_missing(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    east_staff = counter_staff(staff, "EAST")
    i1 = create_simple_item(staff, "杯")
    i2 = staff.create_item([], title="伞")
    dispatch(staff, "BOX", "EAST", [i1["id"], i2["id"]])

    r = receive(east_staff, "BOX", [label("BOX", i1["public_code"])])
    assert r.status_code == 201
    counts = r.json()["counts"]
    assert counts["manifest"] == 2
    assert counts["arrived"] == 1
    assert counts["missing"] == 1
    missing_ids = {m["item_id"] for m in r.json()["missing_items"]}
    assert missing_ids == {i2["id"]}

    loc1 = staff.get(f"/api/staff/items/{i1['id']}/location").json()
    loc2 = staff.get(f"/api/staff/items/{i2['id']}/location").json()
    assert loc1["custody_state"] == "at_counter"
    assert loc1["holding_counter"]["code"] == "EAST"
    assert loc2["custody_state"] == "missing"
    assert loc2["reason_code"] == "missing"

    # Arrived item can be booked at EAST; missing item cannot.
    cid, _ = staff.create_claimant("认领者")
    r = east_staff.post(
        f"/api/staff/items/{i2['id']}/bookings",
        json={"claimant_id": cid, "slot_start": current_slot_start()},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "item_missing"


def test_empty_receipt_marks_everything_missing(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    east_staff = counter_staff(staff, "EAST")
    i1 = create_simple_item(staff)
    dispatch(staff, "EMPTY-BOX", "EAST", [i1["id"]])

    r = receive(east_staff, "EMPTY-BOX", [])
    assert r.status_code == 201
    assert r.json()["scanned_count"] == 0
    assert r.json()["counts"]["missing"] == 1
    loc = staff.get(f"/api/staff/items/{i1['id']}/location").json()
    assert loc["custody_state"] == "missing"


# --------------------------------------------------------------------------
# Receipt conflicts: whole form fails
# --------------------------------------------------------------------------


def test_unknown_label_rejects_whole_receipt(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    east_staff = counter_staff(staff, "EAST")
    item = create_simple_item(staff)
    dispatch(staff, "B", "EAST", [item["id"]])

    # Unknown item code.
    r = receive(east_staff, "B", [label("B", "LF-NOPE00")])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "unknown_label"
    assert staff.get("/api/staff/boxes/B").json()["status"] == "dispatched"

    # Label for a box that exists but does not contain the item.
    other = staff.create_item([], title="另一件")
    r = receive(east_staff, "B", [label("B", other["public_code"])])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "unknown_label"

    # Unknown printed box number.
    r = receive(east_staff, "B", [label("GHOST", item["public_code"])])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "unknown_label"

    # Nothing was saved: a valid receipt can still be submitted once.
    r = receive(east_staff, "B", [label("B", item["public_code"])])
    assert r.status_code == 201


def test_duplicate_scan_in_form_rejects_everything(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    east_staff = counter_staff(staff, "EAST")
    item = create_simple_item(staff)
    dispatch(staff, "B", "EAST", [item["id"]])

    r = receive(
        east_staff,
        "B",
        [label("B", item["public_code"]), label("B", item["id"])],  # same label
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "duplicate_scan"
    assert staff.get("/api/staff/boxes/B").json()["status"] == "dispatched"


def test_old_warehoused_label_cannot_be_scanned_again(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    east_staff = counter_staff(staff, "EAST")
    west_staff = counter_staff(staff, "WEST")
    x = create_simple_item(staff, "X")
    z = staff.create_item([], title="Z")
    dispatch(staff, "A", "EAST", [x["id"]])
    dispatch(staff, "C", "WEST", [z["id"]])

    # X arrives normally at EAST.
    assert receive(east_staff, "A", [label("A", x["public_code"])]).status_code == 201

    # WEST tries to scan the old A/X label together with its own Z.
    r = receive(
        west_staff,
        "C",
        [label("C", z["public_code"]), label("A", x["public_code"])],
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "label_already_warehoused"

    # Whole C receipt failed: Z did not arrive and C has no receipt.
    assert staff.get("/api/staff/boxes/C").json()["status"] == "dispatched"
    loc_z = staff.get(f"/api/staff/items/{z['id']}/location").json()
    assert loc_z["custody_state"] == "in_transit"


# --------------------------------------------------------------------------
# Two-box mismatch reconciliation, symmetric in open order
# --------------------------------------------------------------------------


def _two_box_setup(staff: Staff):
    """X travels in A (->EAST) but physically lands in B (->WEST) with Y."""
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    east_staff = counter_staff(staff, "EAST", "东员")
    west_staff = counter_staff(staff, "WEST", "西员")
    x = create_simple_item(staff, "灰色保温杯")
    y = staff.create_item([], title="黑色长柄伞")
    dispatch(staff, "A", "EAST", [x["id"]])
    dispatch(staff, "B", "WEST", [y["id"]])
    return east_staff, west_staff, x, y


def _assert_final_with_finder_west(staff: Staff, x: dict):
    loc = staff.get(f"/api/staff/items/{x['id']}/location").json()
    assert loc["custody_state"] == "at_counter"
    assert loc["holding_counter"]["code"] == "WEST"
    assert loc["claim_processing_available"] is True

    box_a = staff.get("/api/staff/boxes/A").json()
    line_x = next(m for m in box_a["manifest"] if m["item_id"] == x["id"])
    assert line_x["result"] == "missing"
    assert line_x["reconciled"] is True

    # Original shortage receipt and wrong-box scan records are both retained.
    assert box_a["receipt"] is not None
    assert box_a["receipt"]["missing_items"][0]["item_id"] == x["id"]

    box_b = staff.get("/api/staff/boxes/B").json()
    finding = box_b["receipt"]["wrong_box_findings"][0]
    assert finding["matched"] is True
    assert finding["find_state"] == "matched"
    assert finding["match"]["missing_box_no"] == "A"


def test_wrong_box_found_first_then_original_signs_missing(staff: Staff):
    east_staff, west_staff, x, y = _two_box_setup(staff)

    # WEST opens B first: own Y arrives, foreign A/X is quarantined at WEST.
    r = receive(
        west_staff,
        "B",
        [label("B", y["public_code"]), label("A", x["public_code"])],
    )
    assert r.status_code == 201, r.text
    counts = r.json()["counts"]
    assert counts["arrived"] == 1
    assert counts["wrong_box_found"] == 1
    assert counts["quarantined_open"] == 1

    loc = staff.get(f"/api/staff/items/{x['id']}/location").json()
    assert loc["custody_state"] == "quarantined"
    assert loc["holding_counter"]["code"] == "WEST"
    assert loc["reason_code"] == "quarantined"
    assert loc["open_finding"]["label_box_no"] == "A"
    assert loc["claim_processing_available"] is False

    # A not receipted yet: EAST cannot book X.
    cid, _ = staff.create_claimant("认领者甲")
    r = east_staff.post(
        f"/api/staff/items/{x['id']}/bookings",
        json={"claimant_id": cid, "slot_start": current_slot_start()},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "item_quarantined"

    # Re-reporting the same label in yet another receipt is forbidden.
    z = staff.create_item([], title="第三件")
    make_counter(staff, "SOUTH", "南门台")
    south_staff = counter_staff(staff, "SOUTH")
    dispatch(staff, "C", "SOUTH", [z["id"]])
    r = receive(
        south_staff,
        "C",
        [label("C", z["public_code"]), label("A", x["public_code"])],
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "label_already_found_elsewhere"
    assert staff.get("/api/staff/boxes/C").json()["status"] == "dispatched"

    # EAST trying to scan X as own arrival conflicts with the open report.
    r = receive(east_staff, "A", [label("A", x["public_code"])])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "label_reported_in_other_box"
    assert staff.get("/api/staff/boxes/A").json()["status"] == "dispatched"

    # Correct action: leave X unscanned -> signed missing -> match released.
    r = receive(east_staff, "A", [])
    assert r.status_code == 201
    _assert_final_with_finder_west(staff, x)


def test_original_signs_missing_first_then_wrong_box_appears(staff: Staff):
    east_staff, west_staff, x, y = _two_box_setup(staff)

    # EAST opens A first with nothing scanned: X is missing.
    r = receive(east_staff, "A", [])
    assert r.status_code == 201
    assert r.json()["counts"]["missing"] == 1
    loc = staff.get(f"/api/staff/items/{x['id']}/location").json()
    assert loc["custody_state"] == "missing"

    # WEST opens B and scans the foreign A/X label: immediate reconciliation.
    r = receive(
        west_staff,
        "B",
        [label("B", y["public_code"]), label("A", x["public_code"])],
    )
    assert r.status_code == 201, r.text
    finding = r.json()["wrong_box_findings"][0]
    assert finding["find_state"] == "matched"
    assert r.json()["counts"]["quarantined_open"] == 0

    _assert_final_with_finder_west(staff, x)


def test_finder_counter_completes_booking_and_delivery(staff: Staff):
    east_staff, west_staff, x, _ = _two_box_setup(staff)

    claimant_id, headers = staff.create_claimant("认领者甲")
    make_eligible(staff.client, headers, x["public_code"])

    receive(east_staff, "A", [])
    receive(west_staff, "B", [label("A", x["public_code"])])

    # The cancelled pre-transfer booking does not come back.
    assert active_booking(staff, x["id"]) == []

    # WEST (finder) can serve; it needs a reception slot/roster of its own.
    slot = current_slot_start()
    west_duty = staff.create_operator("WEST", "西员二")
    west_duty.staff_id = west_duty.staff_id
    r = staff.set_roster(
        slot, [{"staff_id": west_duty.staff_id, "counter_ids": ["WEST"]}]
    )
    assert r.status_code == 200, r.text

    # EAST (original destination) has no authority.
    r = east_staff.post(
        f"/api/staff/items/{x['id']}/bookings",
        json={"claimant_id": claimant_id, "slot_start": slot},
    )
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "wrong_counter"

    r = west_staff.post(
        f"/api/staff/items/{x['id']}/bookings",
        json={"claimant_id": claimant_id, "slot_start": slot},
    )
    assert r.status_code == 201, r.text
    booking_id = r.json()["booking_id"]
    r = west_staff.post(
        f"/api/staff/items/{x['id']}/bookings/{booking_id}/check-in",
        json={"reception_staff_id": west_duty.staff_id},
    )
    assert r.status_code == 201, r.text
    r = west_staff.post(
        f"/api/staff/items/{x['id']}/deliveries", json={"claimant_id": claimant_id}
    )
    assert r.status_code == 201, r.text
    assert r.json()["claimant_id"] == claimant_id

    loc = staff.get(f"/api/staff/items/{x['id']}/location").json()
    assert loc["reason_code"] == "already_delivered"


# --------------------------------------------------------------------------
# Privacy: no internal counts / mismatch detail on public or claimant APIs
# --------------------------------------------------------------------------


def test_public_and_claimant_views_hide_internal_counting(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    west_staff = counter_staff(staff, "WEST", "西员")
    x = create_simple_item(staff, "灰色保温杯")
    y = staff.create_item([], title="黑色长柄伞")

    claimant_id, ch = staff.create_claimant("认领者隐私")
    make_eligible(staff.client, ch, x["public_code"])
    duty = staff.create_operator("MAIN", "总台值班")
    setUpReception(staff, current_slot_start(), [(duty, "MAIN")])
    r = staff.book(x["id"], claimant_id, current_slot_start())
    assert r.status_code == 201, r.text

    dispatch(staff, "SECRET-A", "EAST", [x["id"]])
    dispatch(staff, "SECRET-B", "WEST", [y["id"]])

    # WEST opens B and finds the foreign SECRET-A/X label (quarantine).
    r = receive(
        west_staff,
        "SECRET-B",
        [label("SECRET-A", x["public_code"])],
    )
    assert r.status_code == 201, r.text

    public_txt = staff.client.get(f"/api/public/items/{x['public_code']}").text
    status = staff.client.get(
        f"/api/claimant/items/{x['public_code']}/status", headers=ch
    )
    assert status.status_code == 200
    claimant_txt = status.text

    # Internal box numbers, quarantine/missing mechanics never leak.
    assert "SECRET-A" not in public_txt
    assert "SECRET-A" not in claimant_txt
    assert "SECRET-B" not in claimant_txt
    for secret in ["quarantined", "in_transit", "missing", "隔离", "缺件", "错箱"]:
        assert secret not in public_txt
        assert secret not in claimant_txt
    for internal_key in [
        "custody_state",
        "holding_counter",
        "reason_code",
        "active_trip",
        "open_finding",
        "manifest",
        "receipt",
    ]:
        assert internal_key not in status.json()

    # Existing appointment was cancelled because of dispatch.
    assert status.json()["booking"]["status"] == "cancelled"
    assert status.json()["booking"]["ended_reason"] == "item_dispatched"

    # Claimant routes still reject staff-only box/location views.
    assert staff.client.get("/api/staff/boxes/SECRET-A", headers=ch).status_code == 401
    assert staff.client.get(
        f"/api/staff/items/{x['id']}/location", headers=ch
    ).status_code == 401


# --------------------------------------------------------------------------
# Extra guarantees: replay ordering, corrections in transit, re-forwarding
# --------------------------------------------------------------------------


def test_receipt_replay_is_order_insensitive(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    east_staff = counter_staff(staff, "EAST")
    i1 = create_simple_item(staff, "杯")
    i2 = staff.create_item([], title="伞")
    dispatch(staff, "MULTI", "EAST", [i1["id"], i2["id"]])

    payload_order_1 = [
        label("MULTI", i1["public_code"]),
        label("MULTI", i2["public_code"]),
    ]
    r1 = receive(east_staff, "MULTI", payload_order_1)
    assert r1.status_code == 201, r1.text

    # Retry with reversed scan order -> same original receipt.
    r2 = receive(east_staff, "MULTI", list(reversed(payload_order_1)))
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["receipt_id"] == r1.json()["receipt_id"]
    assert r2.json()["counts"]["arrived"] == 2


def test_clue_correction_during_transfer_and_claim_at_finder(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    east_staff = counter_staff(staff, "EAST")
    west_staff = counter_staff(staff, "WEST")
    x = create_simple_item(staff, "灰色保温杯")
    y = staff.create_item([], title="黑色长柄伞")

    claimant_id, ch = staff.create_claimant("认领者甲")
    make_eligible(staff.client, ch, x["public_code"])
    duty = staff.create_operator("MAIN", "总台值班")
    setUpReception(staff, current_slot_start(), [(duty, "MAIN")])
    r = staff.book(x["id"], claimant_id, current_slot_start())
    assert r.status_code == 201

    dispatch(staff, "A", "EAST", [x["id"]])
    dispatch(staff, "B", "WEST", [y["id"]])

    # A staff member may keep correcting clues while the object is in transit:
    # this must not move the item or resurrect the cancelled booking.
    detail_id = next(d["id"] for d in x["details"] if d["code"] == "2")
    r = staff.post(
        f"/api/staff/items/{x['id']}/details/{detail_id}/correct",
        json={"answer": "五角星"},
    )
    assert r.status_code == 200
    assert active_booking(staff, x["id"]) == []

    # Reconcile the wrong-box trip in favour of WEST.
    receive(east_staff, "A", [])
    receive(west_staff, "B", [label("A", x["public_code"])])

    # Old answer for detail 2 is invalidated by the correction; the claimant
    # answers again with the corrected clue and re-qualifies.
    answer(staff.client, ch, x["public_code"], "2", "五角星")
    elig = staff.client.get(
        f"/api/claimant/items/{x['public_code']}/status", headers=ch
    ).json()
    assert elig["eligible_for_delivery"] is True

    slot = current_slot_start()
    west_duty = staff.create_operator("WEST", "西员二")
    assert staff.set_roster(
        slot, [{"staff_id": west_duty.staff_id, "counter_ids": ["WEST"]}]
    ).status_code == 200
    r = west_staff.post(
        f"/api/staff/items/{x['id']}/bookings",
        json={"claimant_id": claimant_id, "slot_start": slot},
    )
    assert r.status_code == 201, r.text
    booking_id = r.json()["booking_id"]
    assert west_staff.post(
        f"/api/staff/items/{x['id']}/bookings/{booking_id}/check-in",
        json={"reception_staff_id": west_duty.staff_id},
    ).status_code == 201
    r = west_staff.post(
        f"/api/staff/items/{x['id']}/deliveries", json={"claimant_id": claimant_id}
    )
    assert r.status_code == 201, r.text


def test_item_can_be_forwarded_again_after_arrival(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "SOUTH", "南门台")
    east_staff = counter_staff(staff, "EAST")
    south_staff = counter_staff(staff, "SOUTH")
    item = create_simple_item(staff)

    dispatch(staff, "LEG-1", "EAST", [item["id"]])
    receive(east_staff, "LEG-1", [label("LEG-1", item["public_code"])])

    # EAST is now the holder and may forward the same item onward.
    r = dispatch(east_staff, "LEG-2", "SOUTH", [item["id"]])
    assert r.status_code == 201, r.text
    loc = staff.get(f"/api/staff/items/{item['id']}/location").json()
    assert loc["custody_state"] == "in_transit"
    assert loc["active_trip"]["box_no"] == "LEG-2"

    r = receive(south_staff, "LEG-2", [label("LEG-2", item["public_code"])])
    assert r.status_code == 201
    loc = staff.get(f"/api/staff/items/{item['id']}/location").json()
    assert loc["custody_state"] == "at_counter"
    assert loc["holding_counter"]["code"] == "SOUTH"

    # Both legs and their labels remain queryable.
    assert staff.get("/api/staff/boxes/LEG-1").json()["receipt"] is not None
    assert staff.get("/api/staff/boxes/LEG-2").json()["receipt"] is not None


def test_mixed_arrival_missing_and_foreign_in_one_receipt(staff: Staff):
    make_counter(staff, "EAST", "东门台")
    make_counter(staff, "WEST", "西门台")
    east_staff = counter_staff(staff, "EAST")
    west_staff = counter_staff(staff, "WEST")

    own_arrive = staff.create_item([], title="正常到的")
    own_missing = staff.create_item([], title="缺件的")
    foreign = create_simple_item(staff, "错箱的")

    # Manifest may use public codes too.
    r = dispatch(
        staff,
        "MX",
        "EAST",
        [own_arrive["public_code"], own_missing["id"]],
    )
    assert r.status_code == 201, r.text
    dispatch(staff, "ORIG", "WEST", [foreign["id"]])

    # EAST receipt: one own arrival, one own shortage, plus a foreign label
    # whose original box ORIG has not been opened yet -> quarantine.
    r = receive(
        east_staff,
        "MX",
        [
            label("MX", own_arrive["public_code"]),
            label("ORIG", foreign["public_code"]),
        ],
    )
    assert r.status_code == 201, r.text
    counts = r.json()["counts"]
    assert counts["arrived"] == 1
    assert counts["missing"] == 1
    assert counts["wrong_box_found"] == 1
    assert counts["quarantined_open"] == 1

    assert staff.get(f"/api/staff/items/{own_arrive['id']}/location").json()[
        "custody_state"
    ] == "at_counter"
    assert staff.get(f"/api/staff/items/{own_missing['id']}/location").json()[
        "reason_code"
    ] == "missing"
    assert staff.get(f"/api/staff/items/{foreign['id']}/location").json()[
        "custody_state"
    ] == "quarantined"

    # Singular /receipt alias behaves identically for a replay.
    r2 = east_staff.post(
        "/api/staff/boxes/MX/receipt",
        json={
            "scanned_labels": [
                label("ORIG", foreign["public_code"]),
                label("MX", own_arrive["public_code"]),
            ]
        },
    )
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True


def test_box_list_and_404(staff: Staff):
    r = staff.get("/api/staff/boxes/NO-SUCH-BOX")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "box_not_found"

    r = staff.get("/api/staff/boxes/NO-SUCH-BOX/receipt")
    assert r.status_code == 404

    boxes = staff.get("/api/staff/boxes").json()["boxes"]
    assert boxes == []
