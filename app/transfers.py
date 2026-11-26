"""Cross-counter transfers: counters, box dispatch, open-box receipts.

Custody model
-------------
Every non-delivered item has an independent physical-custody state:

``at_counter``
    Physically on the shelf of ``custody_counter_id``; that counter can book
    and deliver it.
``in_transit``
    On a dispatched box that has not yet been receipted.
``missing``
    The original box was receipted without the item's label; the shortage is
    recorded against the destination counter.
``quarantined``
    A label of another (still open or un-reconciled) trip was scanned inside a
    different box; the object is physically at the opening counter but isolated.

A wrong-box finding is released only when the original box's unique receipt
records that item as missing.  The reconciliation is symmetric in opening
order:

* foreign scan first  -> ``quarantined``; later missing line matches it;
* missing signed first -> later foreign scan matches immediately.

Every conflict (unknown / old / already-found label, duplicate scan, content
mismatch) aborts the *whole* receipt transaction, so no partial count exists.
Accepted scans and receipts are append-only facts; matching only updates
reconciliation columns.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import string
from typing import Any

from .db import audit, next_seq, now_iso, transaction
from .services import api_error, get_item

# Custody states -------------------------------------------------------------

AT_COUNTER = "at_counter"
IN_TRANSIT = "in_transit"
MISSING = "missing"
QUARANTINED = "quarantined"

CLAIM_BLOCKING_STATES = {IN_TRANSIT, MISSING, QUARANTINED}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def canonical_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_counter(conn: sqlite3.Connection, counter_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM counters WHERE id = ?", (counter_id,)).fetchone()
    if row is None:
        raise api_error(404, "counter_not_found", "柜台不存在")
    return row


def find_counter_by_ref(conn: sqlite3.Connection, ref: str) -> sqlite3.Row | None:
    ref = ref.strip()
    return conn.execute(
        "SELECT * FROM counters WHERE id = ? OR code = ?", (ref, ref)
    ).fetchone()


def get_counter_by_ref(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
    row = find_counter_by_ref(conn, ref)
    if row is None:
        raise api_error(404, "counter_not_found", "柜台不存在", counter_ref=ref)
    return row


def counter_brief(conn: sqlite3.Connection, counter_id: str | None) -> dict[str, Any] | None:
    if counter_id is None:
        return None
    row = conn.execute(
        "SELECT id, code, name, is_main FROM counters WHERE id = ?", (counter_id,)
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "is_main": bool(row["is_main"]),
    }


def staff_brief(conn: sqlite3.Connection, staff_id: str | None) -> dict[str, Any] | None:
    if staff_id is None:
        return None
    row = conn.execute("SELECT id, name FROM staff WHERE id = ?", (staff_id,)).fetchone()
    return None if row is None else {"id": row["id"], "name": row["name"]}


def find_item_by_ref(conn: sqlite3.Connection, ref: str) -> sqlite3.Row | None:
    """Resolve either the internal item id or the public ``LF-XXXXXX`` code."""
    ref = ref.strip()
    return conn.execute(
        "SELECT * FROM items WHERE id = ? OR public_code = ?", (ref, ref)
    ).fetchone()


def get_box(conn: sqlite3.Connection, box_no: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM boxes WHERE box_no = ?", (box_no,)).fetchone()
    if row is None:
        raise api_error(404, "box_not_found", "箱号不存在", box_no=box_no)
    return row


# --------------------------------------------------------------------------
# Counters
# --------------------------------------------------------------------------


def create_counter(
    conn: sqlite3.Connection, staff: sqlite3.Row, name: str, code: str | None
) -> dict[str, Any]:
    with transaction(conn):
        if code is not None:
            code = code.strip()
            existing = conn.execute(
                "SELECT 1 FROM counters WHERE code = ?", (code,)
            ).fetchone()
            if existing:
                raise api_error(409, "counter_code_taken", "柜台编号已存在", counter_code=code)
        else:
            alphabet = string.ascii_uppercase + string.digits
            code = "CTR-" + "".join(secrets.choice(alphabet) for _ in range(6))
            while conn.execute("SELECT 1 FROM counters WHERE code = ?", (code,)).fetchone():
                code = "CTR-" + "".join(secrets.choice(alphabet) for _ in range(6))

        from .services import new_id

        counter_id = new_id()
        at = now_iso()
        conn.execute(
            "INSERT INTO counters(id, code, name, is_main, created_by, created_at)"
            " VALUES (?,?,?,0,?,?)",
            (counter_id, code, name.strip(), staff["id"], at),
        )
        audit(
            conn,
            f"staff:{staff['id']}",
            "counter_created",
            f"counter={counter_id} code={code} name={name}",
        )
        return counter_view(conn, get_counter(conn, counter_id))


def counter_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    member_count = conn.execute(
        "SELECT COUNT(*) AS n FROM staff WHERE counter_id = ?", (row["id"],)
    ).fetchone()["n"]
    held_count = conn.execute(
        "SELECT COUNT(*) AS n FROM items WHERE custody_counter_id = ?"
        " AND custody_state = 'at_counter' AND status = 'held'",
        (row["id"],),
    ).fetchone()["n"]
    return {
        "id": row["id"],
        "code": row["code"],
        "name": row["name"],
        "is_main": bool(row["is_main"]),
        "created_at": row["created_at"],
        "staff_count": int(member_count),
        "held_item_count": int(held_count),
    }


def list_counters(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM counters ORDER BY is_main DESC, created_at, code").fetchall()
    return {"counters": [counter_view(conn, r) for r in rows]}


def counter_inventory(conn: sqlite3.Connection, counter_ref: str) -> dict[str, Any]:
    counter = get_counter_by_ref(conn, counter_ref)
    rows = conn.execute(
        "SELECT * FROM items WHERE custody_counter_id = ?"
        " AND custody_state = 'at_counter' AND status = 'held'"
        " ORDER BY created_at DESC",
        (counter["id"],),
    ).fetchall()
    return {
        "counter": counter_brief(conn, counter["id"]),
        "held_items": [
            {
                "item_id": r["id"],
                "public_code": r["public_code"],
                "title": r["title"],
                "found_location": r["found_location"],
            }
            for r in rows
        ],
        "count": len(rows),
    }


def update_staff_counter(
    conn: sqlite3.Connection, actor: sqlite3.Row, staff_id: str, counter_ref: str
) -> dict[str, Any]:
    with transaction(conn):
        target = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
        if target is None:
            raise api_error(404, "staff_not_found", "工作人员不存在")
        counter = get_counter_by_ref(conn, counter_ref)
        conn.execute("UPDATE staff SET counter_id = ? WHERE id = ?", (counter["id"], staff_id))
        audit(
            conn,
            f"staff:{actor['id']}",
            "staff_counter_assigned",
            f"staff={staff_id} counter={counter['id']}",
        )
        return staff_membership(conn, staff_id)


def staff_membership(conn: sqlite3.Connection, staff_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT id, name, is_admin, counter_id FROM staff WHERE id = ?", (staff_id,)).fetchone()
    if row is None:
        raise api_error(404, "staff_not_found", "工作人员不存在")
    return {
        "staff_id": row["id"],
        "name": row["name"],
        "is_admin": bool(row["is_admin"]),
        "counter_id": row["counter_id"],
        "counter": counter_brief(conn, row["counter_id"]),
    }


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def _dispatch_problem(
    conn: sqlite3.Connection, item: sqlite3.Row, sender_counter_id: str
) -> dict[str, Any] | None:
    base = {
        "item_id": item["id"],
        "public_code": item["public_code"],
        "title": item["title"],
    }
    if item["status"] == "delivered":
        return {**base, "code": "item_already_delivered",
                "message": "该失物已交付，不能发箱"}
    state = item["custody_state"]
    if state == IN_TRANSIT:
        line = conn.execute(
            "SELECT box_no FROM box_items WHERE item_id = ? AND result = 'in_transit'"
            " ORDER BY id DESC LIMIT 1",
            (item["id"],),
        ).fetchone()
        return {**base, "code": "item_in_transit",
                "message": "该失物已在途，不能重复发箱",
                "box_no": None if line is None else line["box_no"]}
    if state == MISSING:
        return {**base, "code": "item_missing",
                "message": "该失物处于缺件状态，不能发箱"}
    if state == QUARANTINED:
        return {**base, "code": "item_quarantined",
                "message": "该失物为错箱隔离状态，对上之前不能发箱"}
    if item["custody_counter_id"] != sender_counter_id:
        holder = counter_brief(conn, item["custody_counter_id"])
        return {**base, "code": "wrong_counter",
                "message": "该失物不在本柜台保管，本柜台不能发走",
                "holding_counter": holder}
    return None


def dispatch_box(
    conn: sqlite3.Connection,
    staff: sqlite3.Row,
    *,
    box_no: str,
    destination_ref: str,
    item_refs: list[str],
) -> dict[str, Any]:
    box_no = box_no.strip()

    # Duplicate manifest rows are rejected outright (not de-duplicated).
    seen: set[str] = set()
    duplicates: list[str] = []
    for raw in item_refs:
        ref = raw.strip()
        if ref in seen and ref not in duplicates:
            duplicates.append(ref)
        seen.add(ref)
    if duplicates:
        raise api_error(
            422, "duplicate_box_item", "失物清单不能重复", duplicates=duplicates
        )

    with transaction(conn):
        sender = get_counter(conn, staff["counter_id"])
        destination = get_counter_by_ref(conn, destination_ref)
        if destination["id"] == sender["id"]:
            raise api_error(
                422,
                "same_counter_transfer",
                "目的柜台与保管柜台相同，不需要跨柜台发箱",
            )

        items: list[sqlite3.Row] = []
        for ref in item_refs:
            item = find_item_by_ref(conn, ref)
            if item is None:
                raise api_error(404, "item_not_found", "失物编号不存在", item_ref=ref)
            items.append(item)

        # The same item may be given once by internal id and once by public
        # code; after resolution that is still a duplicate manifest.
        resolved_ids = [i["id"] for i in items]
        if len(set(resolved_ids)) != len(resolved_ids):
            dup_ids = sorted({i for i in resolved_ids if resolved_ids.count(i) > 1})
            raise api_error(
                422,
                "duplicate_box_item",
                "失物清单不能重复（id 与公开编号指向同一件也算重复）",
                duplicate_item_ids=dup_ids,
            )

        request_hash = canonical_hash(
            {"destination": destination["id"], "items": sorted(resolved_ids)}
        )

        existing = conn.execute(
            "SELECT * FROM boxes WHERE box_no = ?", (box_no,)
        ).fetchone()
        if existing is not None:
            # Retry of the same dispatch: only the original sender may query
            # it through this endpoint, and content can never be overwritten.
            if existing["sender_counter_id"] != sender["id"]:
                raise api_error(
                    403,
                    "wrong_sender_counter",
                    "该箱号由其他柜台发出，本柜台不能重送",
                )
            if existing["request_hash"] != request_hash:
                raise api_error(
                    409,
                    "box_no_content_conflict",
                    "箱号已用于一趟内容不同的发箱，不能覆盖原记录",
                    box_no=box_no,
                )
            view = box_view(conn, box_no)
            view["replayed"] = True
            view["note"] = "相同箱号与业务内容的重送，返回原发箱记录。"
            return view

        # All-or-nothing validation: collect every problem before any write,
        # so one bad item leaves the box uncreated and every booking intact.
        problems = []
        for item in items:
            problem = _dispatch_problem(conn, item, sender["id"])
            if problem is not None:
                problems.append(problem)
        if problems:
            raise api_error(
                409,
                "box_items_not_dispatchable",
                "清单中存在本柜台不能发走的失物，整箱未发出，任何预约均未取消",
                problems=problems,
            )

        at = now_iso()
        seq = next_seq(conn)
        conn.execute(
            "INSERT INTO boxes(box_no, sender_counter_id, destination_counter_id,"
            " dispatched_by, dispatched_at, item_count, request_hash, status, seq)"
            " VALUES (?,?,?,?,?,?,?, 'dispatched', ?)",
            (
                box_no,
                sender["id"],
                destination["id"],
                staff["id"],
                at,
                len(items),
                request_hash,
                seq,
            ),
        )
        for ord_, item in enumerate(items):
            conn.execute(
                "INSERT INTO box_items(box_no, item_id, ord, result)"
                " VALUES (?,?,?, 'in_transit')",
                (box_no, item["id"], ord_),
            )
            conn.execute(
                "UPDATE items SET custody_state = ?, custody_counter_id = NULL,"
                " updated_at = ? WHERE id = ?",
                (IN_TRANSIT, at, item["id"]),
            )
            _cancel_booking_for_dispatch(conn, item["id"], staff["id"], box_no, at)

        audit(
            conn,
            f"staff:{staff['id']}",
            "box_dispatched",
            f"box={box_no} from={sender['code']} to={destination['code']}"
            f" items={len(items)} seq={seq}",
        )
        view = box_view(conn, box_no)
        view["replayed"] = False
        return view


def _cancel_booking_for_dispatch(
    conn: sqlite3.Connection, item_id: str, staff_id: str, box_no: str, at: str
) -> list[int]:
    """Cancel active bookings because the object entered transit.

    Cancellation is one-way, exactly like eligibility cancellations.  An
    existing check-in remains a historical fact and its person's slot seat
    stays occupied until the slot ends; only this item's own booking ends.
    """
    cancelled: list[int] = []
    rows = conn.execute(
        "SELECT id FROM bookings WHERE item_id = ? AND status = 'active'", (item_id,)
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE bookings SET status = 'cancelled', ended_at = ?,"
            " ended_reason = 'item_dispatched' WHERE id = ?",
            (at, row["id"]),
        )
        audit(
            conn,
            f"staff:{staff_id}",
            "booking_cancelled",
            f"booking={row['id']} reason=item_dispatched box={box_no}",
        )
        cancelled.append(row["id"])
    return cancelled


# --------------------------------------------------------------------------
# Receipt + reconciliation
# --------------------------------------------------------------------------


def submit_receipt(
    conn: sqlite3.Connection,
    staff: sqlite3.Row,
    box_no: str,
    labels: list[Any],
) -> dict[str, Any]:
    box_no = box_no.strip()
    with transaction(conn):
        box = get_box(conn, box_no)
        destination_id = box["destination_counter_id"]
        if staff["counter_id"] != destination_id:
            raise api_error(
                403,
                "not_destination_counter",
                "只有该箱的目的柜台能提交开箱签收",
                box_no=box_no,
                destination_counter=counter_brief(conn, destination_id),
                your_counter=counter_brief(conn, staff["counter_id"]),
            )

        # Resolve every label first.  Unknown item identity is an unknown label.
        resolved: list[tuple[str, sqlite3.Row]] = []
        for label in labels:
            item_ref = label.item_id or label.item_code
            item = find_item_by_ref(conn, item_ref) if item_ref else None
            if item is None:
                raise api_error(
                    409,
                    "unknown_label",
                    "签收单包含无法识别的失物标签，整张签收单不生效",
                    label={"box_no": label.box_no, "item_ref": item_ref},
                    reason="item_not_found",
                )
            resolved.append((label.box_no.strip(), item))

        request_hash = canonical_hash(
            {"labels": sorted((lb, item["id"]) for lb, item in resolved)}
        )

        prior_receipt = conn.execute(
            "SELECT * FROM receipts WHERE box_no = ?", (box_no,)
        ).fetchone()
        if prior_receipt is not None:
            # Idempotent retry must short-circuit BEFORE conflict validation:
            # the original labels are now consumed/warehoused by design.
            if prior_receipt["request_hash"] != request_hash:
                raise api_error(
                    409,
                    "receipt_content_conflict",
                    "该箱已签收，重送的标签清单与首次签收不同，不能覆盖原签收单",
                    box_no=box_no,
                    receipt_id=prior_receipt["id"],
                )
            view = receipt_view(conn, prior_receipt["id"])
            view["replayed"] = True
            view["note"] = "相同箱号与签收内容的重送，返回原签收记录。"
            return view

        # Duplicate scans inside this very submission.
        label_keys = [(lb, item["id"]) for lb, item in resolved]
        if len(set(label_keys)) != len(label_keys):
            raise api_error(
                409,
                "duplicate_scan",
                "同一张签收单内标签重复扫描，整张签收单不生效",
            )

        # Validate every label against its own trip manifest and history.
        for lb, item in resolved:
            is_own = lb == box_no
            line = conn.execute(
                "SELECT * FROM box_items WHERE box_no = ? AND item_id = ?",
                (lb, item["id"]),
            ).fetchone()
            if line is None:
                raise api_error(
                    409,
                    "unknown_label",
                    "标签上的箱号/失物不属于任何一趟已登记发箱，不能签收",
                    label={"box_no": lb, "item_id": item["id"],
                           "public_code": item["public_code"]},
                )
            prior_scan = conn.execute(
                "SELECT * FROM receipt_scans WHERE label_box_no = ? AND item_id = ?",
                (lb, item["id"]),
            ).fetchone()
            if prior_scan is not None:
                if prior_scan["scan_kind"] == "own":
                    code = "label_already_warehoused"
                    message = "该标签是已入库的旧标签，不能再次签收"
                elif is_own:
                    code = "label_reported_in_other_box"
                    message = "该标签已在另一箱报到找到，不能再签作本箱实到；" \
                              "如原箱未见到实物，应不扫此标签以登记缺件"
                else:
                    code = "label_already_found_elsewhere"
                    message = "该标签已报告在另一处找到，不能重复报告"
                raise api_error(409, code, message,
                                label={"box_no": lb, "public_code": item["public_code"]})
            if line["result"] == "arrived":
                raise api_error(
                    409,
                    "label_already_warehoused",
                    "该失物已凭本标签入库，旧标签不能再次签收",
                    label={"box_no": lb, "public_code": item["public_code"]},
                )

        at = now_iso()
        seq = next_seq(conn)
        cur = conn.execute(
            "INSERT INTO receipts(box_no, received_by, receiving_counter_id,"
            " received_at, scanned_count, request_hash, seq)"
            " VALUES (?,?,?,?,?,?,?)",
            (box_no, staff["id"], destination_id, at, len(resolved), request_hash, seq),
        )
        receipt_id = cur.lastrowid

        scan_id: dict[tuple[str, str], int] = {}
        for lb, item in resolved:
            is_own = lb == box_no
            cur = conn.execute(
                "INSERT INTO receipt_scans(receipt_id, label_box_no, item_id,"
                " scan_kind, find_state, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    receipt_id,
                    lb,
                    item["id"],
                    "own" if is_own else "wrong_box",
                    "arrived" if is_own else "open",
                    at,
                ),
            )
            scan_id[(lb, item["id"])] = cur.lastrowid

        matches: list[int] = []

        # 1) Process THIS box's manifest: scanned own labels arrive, the rest
        #    are shortages.  A shortage may close an earlier foreign finding.
        lines = conn.execute(
            "SELECT * FROM box_items WHERE box_no = ? ORDER BY id", (box_no,)
        ).fetchall()
        scanned_own = {item["id"] for lb, item in resolved if lb == box_no}

        for line in lines:
            item_id = line["item_id"]
            if item_id in scanned_own:
                conn.execute(
                    "UPDATE box_items SET result = 'arrived', result_receipt_id = ?,"
                    " processed_at = ? WHERE id = ?",
                    (receipt_id, at, line["id"]),
                )
                conn.execute(
                    "UPDATE items SET custody_state = ?, custody_counter_id = ?,"
                    " updated_at = ? WHERE id = ?",
                    (AT_COUNTER, destination_id, at, item_id),
                )
                continue

            conn.execute(
                "UPDATE box_items SET result = 'missing', result_receipt_id = ?,"
                " processed_at = ? WHERE id = ?",
                (receipt_id, at, line["id"]),
            )
            conn.execute(
                "UPDATE items SET custody_state = ?, custody_counter_id = ?,"
                " updated_at = ? WHERE id = ?",
                (MISSING, destination_id, at, item_id),
            )

            open_find = conn.execute(
                "SELECT s.id AS scan_id, r.id AS find_receipt_id,"
                "       r.receiving_counter_id AS finder_counter_id"
                "  FROM receipt_scans s JOIN receipts r ON r.id = s.receipt_id"
                " WHERE s.label_box_no = ? AND s.item_id = ?"
                "   AND s.scan_kind = 'wrong_box' AND s.find_state = 'open'",
                (box_no, item_id),
            ).fetchone()
            if open_find is not None:
                matches.append(
                    _match_find(
                        conn,
                        at=at,
                        item_id=item_id,
                        missing_box_no=box_no,
                        missing_receipt_id=receipt_id,
                        find_scan_id=open_find["scan_id"],
                        find_receipt_id=open_find["find_receipt_id"],
                        finder_counter_id=open_find["finder_counter_id"],
                    )
                )

        # 2) Foreign labels scanned here: either match an already-signed
        #    shortage immediately, or quarantine at the opening counter.
        for lb, item in resolved:
            if lb == box_no:
                continue
            key = (lb, item["id"])
            this_scan_id = scan_id[key]
            original_receipt = conn.execute(
                "SELECT * FROM receipts WHERE box_no = ?", (lb,)
            ).fetchone()
            original_line = conn.execute(
                "SELECT * FROM box_items WHERE box_no = ? AND item_id = ?",
                (lb, item["id"]),
            ).fetchone()
            if (
                original_receipt is not None
                and original_line is not None
                and original_line["result"] == "missing"
            ):
                matches.append(
                    _match_find(
                        conn,
                        at=at,
                        item_id=item["id"],
                        missing_box_no=lb,
                        missing_receipt_id=original_receipt["id"],
                        find_scan_id=this_scan_id,
                        find_receipt_id=receipt_id,
                        finder_counter_id=destination_id,
                    )
                )
            else:
                conn.execute(
                    "UPDATE items SET custody_state = ?, custody_counter_id = ?,"
                    " updated_at = ? WHERE id = ?",
                    (QUARANTINED, destination_id, at, item["id"]),
                )

        conn.execute(
            "UPDATE boxes SET status = 'received', receipt_id = ? WHERE box_no = ?",
            (receipt_id, box_no),
        )
        audit(
            conn,
            f"staff:{staff['id']}",
            "box_received",
            f"box={box_no} receipt={receipt_id} scanned={len(resolved)}"
            f" matches={len(matches)} seq={seq}",
        )
        view = receipt_view(conn, receipt_id)
        view["replayed"] = False
        return view


def _match_find(
    conn: sqlite3.Connection,
    *,
    at: str,
    item_id: str,
    missing_box_no: str,
    missing_receipt_id: int,
    find_scan_id: int,
    find_receipt_id: int,
    finder_counter_id: str,
) -> int:
    """Pair a signed shortage with a physical wrong-box finding.

    The item is henceforth kept by the counter that actually found it.
    """
    cur = conn.execute(
        "INSERT INTO mismatch_matches(item_id, missing_box_no, missing_receipt_id,"
        " find_scan_id, find_receipt_id, finder_counter_id, matched_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (
            item_id,
            missing_box_no,
            missing_receipt_id,
            find_scan_id,
            find_receipt_id,
            finder_counter_id,
            at,
        ),
    )
    match_id = cur.lastrowid
    conn.execute(
        "UPDATE receipt_scans SET find_state = 'matched', matched_at = ?,"
        " matched_missing_receipt_id = ? WHERE id = ?",
        (at, missing_receipt_id, find_scan_id),
    )
    conn.execute(
        "UPDATE items SET custody_state = ?, custody_counter_id = ?, updated_at = ?"
        " WHERE id = ?",
        (AT_COUNTER, finder_counter_id, at, item_id),
    )
    audit(
        conn,
        "system:reconciliation",
        "mismatch_matched",
        f"item={item_id} missing_box={missing_box_no}"
        f" missing_receipt={missing_receipt_id} find_scan={find_scan_id}"
        f" finder_counter={finder_counter_id}",
    )
    return match_id


# --------------------------------------------------------------------------
# Views
# --------------------------------------------------------------------------


def receipt_view(conn: sqlite3.Connection, receipt_id: int) -> dict[str, Any]:
    receipt = conn.execute(
        "SELECT * FROM receipts WHERE id = ?", (receipt_id,)
    ).fetchone()
    if receipt is None:
        raise api_error(404, "receipt_not_found", "签收记录不存在")

    scans = conn.execute(
        """
        SELECT s.*, i.public_code, i.title,
               m.id AS match_id, m.missing_box_no, m.matched_at AS match_at,
               mc.code AS finder_counter_code, mc.name AS finder_counter_name
          FROM receipt_scans s
          JOIN items i ON i.id = s.item_id
          LEFT JOIN mismatch_matches m ON m.find_scan_id = s.id
          LEFT JOIN counters mc ON mc.id = m.finder_counter_id
         WHERE s.receipt_id = ?
         ORDER BY s.id
        """,
        (receipt_id,),
    ).fetchall()

    label_rows = []
    arrived_items: list[dict[str, Any]] = []
    wrong_box_findings: list[dict[str, Any]] = []
    for s in scans:
        row = {
            "scan_id": s["id"],
            "label_box_no": s["label_box_no"],
            "item_id": s["item_id"],
            "public_code": s["public_code"],
            "title": s["title"],
            "scan_kind": s["scan_kind"],
            "find_state": s["find_state"],
            "matched": s["find_state"] == "matched",
            "matched_at": s["matched_at"],
            "matched_missing_receipt_id": s["matched_missing_receipt_id"],
            "match": None
            if s["match_id"] is None
            else {
                "match_id": s["match_id"],
                "missing_box_no": s["missing_box_no"],
                "matched_at": s["match_at"],
                "keeper_counter": {
                    "code": s["finder_counter_code"],
                    "name": s["finder_counter_name"],
                },
            },
        }
        label_rows.append(row)
        brief = {"item_id": s["item_id"], "public_code": s["public_code"], "title": s["title"]}
        if s["scan_kind"] == "own":
            arrived_items.append(brief)
        else:
            wrong_box_findings.append({**row})

    box_no = receipt["box_no"]
    manifest = conn.execute(
        """
        SELECT bi.*, i.public_code, i.title
          FROM box_items bi JOIN items i ON i.id = bi.item_id
         WHERE bi.box_no = ? ORDER BY bi.id
        """,
        (box_no,),
    ).fetchall()
    missing_items = [
        {"item_id": m["item_id"], "public_code": m["public_code"], "title": m["title"]}
        for m in manifest
        if m["result"] == "missing"
    ]
    manifest_rows = [
        {
            "item_id": m["item_id"],
            "public_code": m["public_code"],
            "title": m["title"],
            "result": m["result"],
            "processed_at": m["processed_at"],
        }
        for m in manifest
    ]

    matches = conn.execute(
        "SELECT * FROM mismatch_matches WHERE missing_receipt_id = ?"
        " OR find_receipt_id = ? ORDER BY id",
        (receipt_id, receipt_id),
    ).fetchall()

    return {
        "receipt_id": receipt["id"],
        "box_no": box_no,
        "received_at": receipt["received_at"],
        "received_by": staff_brief(conn, receipt["received_by"]),
        "receiving_counter": counter_brief(conn, receipt["receiving_counter_id"]),
        "scanned_count": receipt["scanned_count"],
        "counts": {
            "manifest": len(manifest),
            "scanned": receipt["scanned_count"],
            "arrived": len(arrived_items),
            "missing": len(missing_items),
            "wrong_box_found": len(wrong_box_findings),
            "wrong_box_matched": sum(1 for w in wrong_box_findings if w["matched"]),
            "quarantined_open": sum(
                1 for w in wrong_box_findings if w["find_state"] == "open"
            ),
        },
        "scanned_labels": label_rows,
        "manifest": manifest_rows,
        "arrived_items": arrived_items,
        "missing_items": missing_items,
        "wrong_box_findings": wrong_box_findings,
        "match_ids": [m["id"] for m in matches],
    }


def box_view(conn: sqlite3.Connection, box_no: str) -> dict[str, Any]:
    box = get_box(conn, box_no)
    lines = conn.execute(
        """
        SELECT bi.*, i.public_code, i.title
          FROM box_items bi JOIN items i ON i.id = bi.item_id
         WHERE bi.box_no = ? ORDER BY bi.id
        """,
        (box_no,),
    ).fetchall()

    manifest = []
    for line in lines:
        match = conn.execute(
            "SELECT * FROM mismatch_matches WHERE missing_box_no = ? AND item_id = ?",
            (box_no, line["item_id"]),
        ).fetchone()
        manifest.append(
            {
                "item_id": line["item_id"],
                "public_code": line["public_code"],
                "title": line["title"],
                "result": line["result"],
                "processed_at": line["processed_at"],
                "reconciled": match is not None,
                "match_id": None if match is None else match["id"],
            }
        )

    receipt = None
    if box["receipt_id"] is not None:
        receipt = receipt_view(conn, box["receipt_id"])

    return {
        "box_no": box["box_no"],
        "status": box["status"],
        "dispatched_at": box["dispatched_at"],
        "sender_counter": counter_brief(conn, box["sender_counter_id"]),
        "destination_counter": counter_brief(conn, box["destination_counter_id"]),
        "dispatched_by": staff_brief(conn, box["dispatched_by"]),
        "item_count": box["item_count"],
        "manifest": manifest,
        "receipt": receipt,
    }


def list_boxes(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT box_no FROM boxes ORDER BY dispatched_at DESC, rowid DESC").fetchall()
    return {"boxes": [box_view(conn, r["box_no"]) for r in rows]}


def item_location(conn: sqlite3.Connection, item_id: str) -> dict[str, Any]:
    item = get_item(conn, item_id)
    delivery = conn.execute(
        "SELECT id, delivered_at, claimant_id FROM deliveries WHERE item_id = ?",
        (item_id,),
    ).fetchone()

    result: dict[str, Any] = {
        "item_id": item["id"],
        "public_code": item["public_code"],
        "title": item["title"],
        "item_status": item["status"],
        "custody_state": item["custody_state"],
        "holding_counter": counter_brief(conn, item["custody_counter_id"]),
        "claim_processing_available": False,
        "claimable_here": False,
        "reason_code": None,
        "reason_message": None,
        "active_trip": None,
        "open_finding": None,
    }

    if item["status"] == "delivered" or delivery is not None:
        result["custody_state"] = "delivered"
        result["holding_counter"] = None
        result["reason_code"] = "already_delivered"
        result["reason_message"] = "该失物已交付，流程结束。"
        if delivery is not None:
            result["delivery_id"] = delivery["id"]
            result["delivered_at"] = delivery["delivered_at"]
        return result

    state = item["custody_state"]
    if state == AT_COUNTER:
        result["claim_processing_available"] = True
        result["claimable_here"] = True
        result["reason_message"] = "实物在本柜台，可按既有资格规则办理预约与交付。"
        return result

    if state == IN_TRANSIT:
        line = conn.execute(
            "SELECT box_no FROM box_items WHERE item_id = ? AND result = 'in_transit'"
            " ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        trip = None
        if line is not None:
            box = get_box(conn, line["box_no"])
            trip = {
                "box_no": box["box_no"],
                "sender_counter": counter_brief(conn, box["sender_counter_id"]),
                "destination_counter": counter_brief(conn, box["destination_counter_id"]),
                "dispatched_at": box["dispatched_at"],
            }
        result["active_trip"] = trip
        result["reason_code"] = IN_TRANSIT
        result["reason_message"] = "失物正在调拨在途，任何柜台都不能办理领取。"
        return result

    if state == MISSING:
        line = conn.execute(
            "SELECT bi.box_no, bi.result_receipt_id, b.destination_counter_id"
            "  FROM box_items bi JOIN boxes b ON b.box_no = bi.box_no"
            " WHERE bi.item_id = ? AND bi.result = 'missing'"
            " ORDER BY bi.id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        # No counter physically holds a signed-shortage item; the destination
        # is only where it was expected.
        result["holding_counter"] = None
        result["reason_code"] = MISSING
        result["reason_message"] = (
            "目的柜台开箱时未扫到该标签，已登记缺件；在另一箱实际找到并对上之前不可领取。"
        )
        if line is not None:
            expected = counter_brief(conn, line["destination_counter_id"])
            result["expected_at_counter"] = expected
            result["active_trip"] = {
                "box_no": line["box_no"],
                "missing_receipt_id": line["result_receipt_id"],
                "signed_as": "missing",
                "expecting_counter": expected,
            }
        return result

    if state == QUARANTINED:
        finding = conn.execute(
            """
            SELECT s.id AS scan_id, s.label_box_no, s.receipt_id,
                   r.box_no AS found_in_box_no, r.receiving_counter_id
              FROM receipt_scans s JOIN receipts r ON r.id = s.receipt_id
             WHERE s.item_id = ? AND s.scan_kind = 'wrong_box'
               AND s.find_state = 'open'
             ORDER BY s.id DESC LIMIT 1
            """,
            (item_id,),
        ).fetchone()
        result["reason_code"] = QUARANTINED
        result["reason_message"] = (
            "实物在开箱柜台但属于另一趟标签，已隔离；须等原箱签收登记缺件并对上后，"
            "才由实际找到它的柜台保管和办理领取。"
        )
        if finding is not None:
            result["open_finding"] = {
                "scan_id": finding["scan_id"],
                "label_box_no": finding["label_box_no"],
                "found_in_receipt_id": finding["receipt_id"],
                "found_in_opening_of_box": finding["found_in_box_no"],
                "finder_counter": counter_brief(conn, finding["receiving_counter_id"]),
            }
            result["holding_counter"] = counter_brief(
                conn, finding["receiving_counter_id"]
            )
        return result

    # Defensive fallback for any future state.
    result["reason_code"] = state
    result["reason_message"] = "该失物当前状态不可办理领取。"
    return result
