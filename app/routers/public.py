"""Public (no authentication) and claimant-facing endpoints."""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ..schemas import AnswerSubmit
from ..security import current_claimant, get_conn
from .. import services

public_router = APIRouter(prefix="/api/public", tags=["public"])
claimant_router = APIRouter(prefix="/api/claimant", tags=["claimant"])

Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
Claimant = Annotated[sqlite3.Row, Depends(current_claimant)]


@public_router.get("/items")
def list_public_items(
    conn: Conn,
    q: Annotated[str | None, Query(max_length=100)] = None,
) -> dict:
    """Search the public notice board.  Only public content is ever returned."""
    sql = "SELECT * FROM items WHERE 1=1"
    params: list = []
    if q:
        needle = f"%{q.strip()}%"
        sql += " AND (title LIKE ? OR public_description LIKE ? OR found_location LIKE ?)"
        params += [needle, needle, needle]
    sql += " ORDER BY found_at DESC, public_code"
    items = conn.execute(sql, params).fetchall()
    return {"items": [services.public_item(conn, item) for item in items]}


@public_router.get("/items/{public_code}")
def get_public_item(public_code: str, conn: Conn) -> dict:
    item = services.get_item_by_public_code(conn, public_code)
    return services.public_item(conn, item)


@claimant_router.post("/answers")
def submit_answer(payload: AnswerSubmit, conn: Conn, claimant: Claimant) -> dict:
    """Submit or update one answer.  Only aggregate eligibility is returned."""
    result = services.submit_answer(
        conn, claimant["id"], payload.public_code, payload.detail_code, payload.text
    )
    return result


@claimant_router.get("/items/{public_code}/status")
def my_status(public_code: str, conn: Conn, claimant: Claimant) -> dict:
    """Own processing status only: no other claimants, no per-detail verdicts."""
    return services.claimant_item_status(conn, claimant["id"], public_code)
