"""Counter stocktake endpoints (staff-only; discrepancies are counter-private)."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from .. import stocktakes
from ..schemas import (
    StocktakeFindCreate,
    StocktakeReviewCreate,
    StocktakeScanCreate,
)
from ..security import current_staff, get_conn

router = APIRouter(prefix="/api/staff/stocktakes", tags=["staff-stocktakes"])

Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
Staff = Annotated[sqlite3.Row, Depends(current_staff)]


@router.post("", status_code=201)
def open_round(conn: Conn, staff: Staff, response: Response) -> dict:
    result = stocktakes.open_round(conn, staff)
    response.status_code = 201
    return result


@router.get("")
def list_rounds(
    conn: Conn,
    staff: Staff,
    counter: Annotated[str | None, Query(max_length=64)] = None,
) -> dict:
    return stocktakes.list_rounds(conn, staff, counter)


@router.get("/{round_id}")
def get_round(round_id: str, conn: Conn, staff: Staff) -> dict:
    round_row = stocktakes.get_round(conn, round_id)
    stocktakes.assert_can_view(conn, staff, round_row)
    view = stocktakes.round_view(conn, round_row)
    return view


@router.post("/{round_id}/scans")
def submit_scan(
    round_id: str,
    payload: StocktakeScanCreate,
    conn: Conn,
    staff: Staff,
    response: Response,
) -> dict:
    result = stocktakes.submit_scan(
        conn, staff, round_id, payload.request_no, payload.item_ref
    )
    # Replays return the original result with 200; new adoptions are 201.
    response.status_code = 200 if result.get("replayed") else 201
    return result


@router.get("/{round_id}/scans")
def list_scans(round_id: str, conn: Conn, staff: Staff) -> dict:
    return stocktakes.list_scans(conn, staff, round_id)


@router.get("/{round_id}/scans/{request_no}")
def get_scan(
    round_id: str, request_no: str, conn: Conn, staff: Staff
) -> dict:
    return stocktakes.get_scan_result(conn, staff, round_id, request_no)


@router.post("/{round_id}/close")
def close_round(round_id: str, conn: Conn, staff: Staff) -> dict:
    return stocktakes.close_round(conn, staff, round_id)


@router.get("/{round_id}/settlement")
def get_settlement(round_id: str, conn: Conn, staff: Staff) -> dict:
    return stocktakes.get_settlement(conn, staff, round_id)


@router.post("/{round_id}/items/{item_ref}/review")
def review_item(
    round_id: str,
    item_ref: str,
    payload: StocktakeReviewCreate,
    conn: Conn,
    staff: Staff,
) -> dict:
    return stocktakes.review_settlement_item(
        conn, staff, round_id, item_ref, payload.outcome, payload.note
    )


@router.post("/{round_id}/items/{item_ref}/find", status_code=201)
def register_find(
    round_id: str,
    item_ref: str,
    payload: StocktakeFindCreate,
    conn: Conn,
    staff: Staff,
) -> dict:
    return stocktakes.register_find(conn, staff, round_id, item_ref, payload.note)
