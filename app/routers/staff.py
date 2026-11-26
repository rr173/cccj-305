"""Staff endpoints.

Staff manage tokens through a one-time bootstrap token configured via
``LOF_BOOTSTRAP_TOKEN``.  Everything else requires a staff bearer token.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Header

from ..schemas import (
    BookingCreate,
    ClaimantCreate,
    CorrectionIn,
    DeliveryCreate,
    DetailAdd,
    ItemCreate,
    StaffCreate,
)
from ..security import current_staff, get_conn, hash_token, new_token
from ..db import audit, now_iso, next_seq, transaction
from .. import services

router = APIRouter(prefix="/api/staff", tags=["staff"])

Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
Staff = Annotated[sqlite3.Row, Depends(current_staff)]


@router.post("/bootstrap", status_code=201)
def bootstrap_staff(
    payload: StaffCreate,
    conn: Conn,
    x_bootstrap_token: Annotated[str, Header(alias="X-Bootstrap-Token")],
) -> dict:
    expected = os.environ.get("LOF_BOOTSTRAP_TOKEN")
    if not expected or not secrets.compare_digest(x_bootstrap_token, expected):
        raise services.api_error(401, "invalid_bootstrap_token", "引导令牌无效")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM staff LIMIT 1").fetchone():
            raise services.api_error(409, "staff_exists", "已存在工作人员账户，引导接口已关闭")
        staff_id = services.new_id()
        token = new_token()
        conn.execute(
            "INSERT INTO staff(id, name, token_hash, is_admin, created_at)"
            " VALUES (?,?,?,1,?)",
            (staff_id, payload.name, hash_token(token), now_iso()),
        )
        audit(conn, f"staff:{staff_id}", "staff_created", "bootstrap")
    return {"staff_id": staff_id, "name": payload.name, "token": token}


@router.post("/operators", status_code=201)
def create_staff(
    payload: StaffCreate, conn: Conn, staff: Staff
) -> dict:
    if not staff["is_admin"]:
        raise services.api_error(403, "forbidden", "仅管理员可创建工作人员")
    with transaction(conn):
        staff_id = services.new_id()
        token = new_token()
        conn.execute(
            "INSERT INTO staff(id, name, token_hash, created_at) VALUES (?,?,?,?)",
            (staff_id, payload.name, hash_token(token), now_iso()),
        )
        audit(conn, f"staff:{staff['id']}", "staff_created", f"staff_id={staff_id}")
    return {"staff_id": staff_id, "name": payload.name, "token": token}


@router.post("/claimants", status_code=201)
def create_claimant(payload: ClaimantCreate, conn: Conn, staff: Staff) -> dict:
    """Staff hand a one-time token to a claimant.

    Claimants may use any display name; real personal data is not required.
    """
    with transaction(conn):
        claimant_id = services.new_id()
        token = new_token()
        conn.execute(
            "INSERT INTO claimants(id, display_name, token_hash, created_at)"
            " VALUES (?,?,?,?)",
            (claimant_id, payload.display_name, hash_token(token), now_iso()),
        )
        audit(
            conn,
            f"staff:{staff['id']}",
            "claimant_created",
            f"claimant_id={claimant_id}",
        )
    return {
        "claimant_id": claimant_id,
        "display_name": payload.display_name,
        "token": token,
        "note": "请将此令牌交给认领者本人；令牌只显示一次。",
    }


@router.post("/items", status_code=201)
def create_item(payload: ItemCreate, conn: Conn, staff: Staff) -> dict:
    with transaction(conn):
        codes = payload.detail_codes()
        if len(set(codes)) != len(codes):
            raise services.api_error(422, "duplicate_detail_code", "细节编号不能重复")
        next_seq(conn)
        item_id = services.new_id()
        public_code = services.new_public_code(conn)
        now = now_iso()
        conn.execute(
            "INSERT INTO items(id, public_code, title, public_description, found_at,"
            " found_location, status, created_by, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,'held',?,?,?)",
            (
                item_id,
                public_code,
                payload.title,
                payload.public_description,
                payload.found_at.isoformat(),
                payload.found_location,
                staff["id"],
                now,
                now,
            ),
        )
        for detail in payload.details:
            conn.execute(
                "INSERT INTO details(id, item_id, code, label, answer, is_public,"
                " published_seq, published_at, correction_epoch, created_at, updated_at)"
                " VALUES (?,?,?,?,?,0,NULL,NULL,0,?,?)",
                (
                    services.new_id(),
                    item_id,
                    detail.code,
                    detail.label,
                    detail.answer.strip(),
                    now,
                    now,
                ),
            )
        audit(
            conn,
            f"staff:{staff['id']}",
            "item_created",
            f"item={public_code} details={len(payload.details)}",
        )
        item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return services.staff_item(conn, item)


@router.get("/items")
def list_items(conn: Conn, staff: Staff) -> dict:
    items = conn.execute("SELECT * FROM items ORDER BY created_at DESC").fetchall()
    return {"items": [services.staff_item(conn, i) for i in items]}


@router.get("/items/{item_id}")
def get_item(item_id: str, conn: Conn, staff: Staff) -> dict:
    return services.staff_item(conn, services.get_item(conn, item_id))


@router.post("/items/{item_id}/details", status_code=201)
def add_detail(item_id: str, payload: DetailAdd, conn: Conn, staff: Staff) -> dict:
    detail = services.add_detail(
        conn, item_id, staff["id"], payload.code, payload.label, payload.answer
    )
    return {
        "detail_id": detail["id"],
        "code": detail["code"],
        "label": detail["label"],
        "answer": detail["answer"],
        "is_public": False,
    }


@router.post("/items/{item_id}/details/{detail_id}/publish")
def publish_detail(item_id: str, detail_id: str, conn: Conn, staff: Staff) -> dict:
    return services.publish_detail(conn, item_id, detail_id, staff["id"])


@router.post("/items/{item_id}/details/{detail_id}/correct")
def correct_detail(
    item_id: str, detail_id: str, payload: CorrectionIn, conn: Conn, staff: Staff
) -> dict:
    return services.correct_detail(conn, item_id, detail_id, staff["id"], payload.answer)


@router.get("/items/{item_id}/claimants")
def item_claimants(item_id: str, conn: Conn, staff: Staff) -> dict:
    """Internal comparison view: never exposed on claimant/public routes."""
    item = services.get_item(conn, item_id)
    return {
        "item_id": item_id,
        "public_code": item["public_code"],
        "required_private_clues": services.MIN_PRIVATE_CLUES,
        "claimants": services.item_claimants(conn, item),
    }


@router.post("/items/{item_id}/bookings", status_code=201)
def create_booking(
    item_id: str, payload: BookingCreate, conn: Conn, staff: Staff
) -> dict:
    return services.create_booking(conn, item_id, payload.claimant_id, staff["id"])


@router.get("/items/{item_id}/bookings")
def list_bookings(item_id: str, conn: Conn, staff: Staff) -> dict:
    services.get_item(conn, item_id)
    rows = conn.execute(
        "SELECT b.*, c.display_name FROM bookings b JOIN claimants c ON c.id = b.claimant_id"
        " WHERE b.item_id = ? ORDER BY b.id",
        (item_id,),
    ).fetchall()
    return {"bookings": [dict(r) for r in rows]}


@router.post("/items/{item_id}/bookings/{booking_id}/cancel")
def cancel_booking(item_id: str, booking_id: int, conn: Conn, staff: Staff) -> dict:
    return services.cancel_booking(conn, item_id, booking_id, staff["id"])


@router.post("/items/{item_id}/deliveries", status_code=201)
def deliver(item_id: str, payload: DeliveryCreate, conn: Conn, staff: Staff) -> dict:
    return services.deliver(conn, item_id, payload.claimant_id, staff["id"])


@router.get("/items/{item_id}/delivery")
def get_delivery(item_id: str, conn: Conn, staff: Staff) -> dict:
    services.get_item(conn, item_id)
    return services.delivery_view(conn, item_id)
