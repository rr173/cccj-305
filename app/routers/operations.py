"""Staff endpoints for counters, box dispatch/receipt and item location."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from ..schemas import CounterCreate, DispatchCreate, ReceiptCreate
from ..security import current_staff, get_conn
from .. import transfers

router = APIRouter(prefix="/api/staff", tags=["staff-transfers"])

Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
Staff = Annotated[sqlite3.Row, Depends(current_staff)]


# --------------------------------------------------------------------------
# Counters
# --------------------------------------------------------------------------


@router.post("/counters", status_code=201)
def create_counter(payload: CounterCreate, conn: Conn, staff: Staff) -> dict:
    if not staff["is_admin"]:
        from ..services import api_error

        raise api_error(403, "forbidden", "仅管理员可创建柜台")
    return transfers.create_counter(conn, staff, payload.name, payload.code)


@router.get("/counters")
def list_counters(conn: Conn, staff: Staff) -> dict:
    return transfers.list_counters(conn)


@router.get("/counters/{counter_ref}")
def get_counter(counter_ref: str, conn: Conn, staff: Staff) -> dict:
    counter = transfers.get_counter_by_ref(conn, counter_ref)
    return transfers.counter_view(conn, counter)


@router.get("/counters/{counter_ref}/inventory")
def counter_inventory(counter_ref: str, conn: Conn, staff: Staff) -> dict:
    return transfers.counter_inventory(conn, counter_ref)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


@router.post("/boxes")
def dispatch_box(
    payload: DispatchCreate, conn: Conn, staff: Staff, response: Response
) -> dict:
    result = transfers.dispatch_box(
        conn,
        staff,
        box_no=payload.box_no,
        destination_ref=payload.destination_counter_id,
        item_refs=payload.item_ids,
    )
    response.status_code = 200 if result.get("replayed") else 201
    return result


@router.get("/boxes")
def list_boxes(conn: Conn, staff: Staff) -> dict:
    return transfers.list_boxes(conn)


@router.get("/boxes/{box_no}")
def get_box(box_no: str, conn: Conn, staff: Staff) -> dict:
    return transfers.box_view(conn, box_no)


# --------------------------------------------------------------------------
# Receipt (destination counter, exactly once; retries are idempotent)
# --------------------------------------------------------------------------


@router.post("/boxes/{box_no}/receipts")
@router.post("/boxes/{box_no}/receipt", include_in_schema=False)
def submit_receipt(
    box_no: str,
    payload: ReceiptCreate,
    conn: Conn,
    staff: Staff,
    response: Response,
) -> dict:
    result = transfers.submit_receipt(conn, staff, box_no, payload.scanned_labels)
    response.status_code = 200 if result.get("replayed") else 201
    return result


@router.get("/boxes/{box_no}/receipt")
def get_receipt(box_no: str, conn: Conn, staff: Staff) -> dict:
    box = transfers.get_box(conn, box_no)
    if box["receipt_id"] is None:
        from ..services import api_error

        raise api_error(404, "receipt_not_found", "该箱尚未开箱签收", box_no=box_no)
    return transfers.receipt_view(conn, box["receipt_id"])


# --------------------------------------------------------------------------
# Single-item location / claimability
# --------------------------------------------------------------------------


@router.get("/items/{item_id}/location")
def item_location(item_id: str, conn: Conn, staff: Staff) -> dict:
    return transfers.item_location(conn, item_id)
