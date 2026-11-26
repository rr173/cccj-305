"""Bearer tokens and request authentication dependencies."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)


def hash_token(token: str) -> str:
    """Tokens are high-entropy random strings, so a plain SHA-256 is enough."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _unauthorized(message: str = "缺少或无效的访问令牌") -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "unauthorized", "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


def _credentials(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise _unauthorized()
    return creds.credentials


def get_conn(request: Request):
    from .db import connect

    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def current_staff(
    creds: Annotated[str, Depends(_credentials)],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM staff WHERE token_hash = ?", (hash_token(creds),)
    ).fetchone()
    if row is None:
        raise _unauthorized("无效的工作人员令牌")
    return row


def current_claimant(
    creds: Annotated[str, Depends(_credentials)],
    conn: Annotated[sqlite3.Connection, Depends(get_conn)],
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM claimants WHERE token_hash = ?", (hash_token(creds),)
    ).fetchone()
    if row is None:
        raise _unauthorized("无效的认领者令牌")
    return row
