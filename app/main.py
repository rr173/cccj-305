"""FastAPI application entry point."""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import connect, db_path_from_env, init_db
from .routers.public import claimant_router, public_router
from .routers.staff import router as staff_router


def create_app(db_path: str | Path | None = None) -> FastAPI:
    resolved = Path(db_path) if db_path is not None else db_path_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = connect(resolved)
        try:
            init_db(conn)
        finally:
            conn.close()
        yield

    app = FastAPI(
        title="展馆失物招领台",
        version="1.0.0",
        description=(
            "工作人员登记失物与私有细节；认领者按编号提交答案；"
            "两条不同的有效私有线索才具备到场交付资格。"
        ),
        lifespan=lifespan,
    )
    app.state.db_path = resolved

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request: Request, exc: sqlite3.IntegrityError):
        code = "conflict"
        if "one_active_booking_per_item" in str(exc):
            code = "item_already_booked"
        return JSONResponse(
            status_code=409,
            content={"detail": {"code": code, "message": "数据冲突，操作未生效"}},
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"detail": {"code": "internal_error", "message": "服务器内部错误"}},
        )

    @app.get("/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok"}

    app.include_router(public_router)
    app.include_router(claimant_router)
    app.include_router(staff_router)
    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )
