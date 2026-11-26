#!/usr/bin/env python3
"""Convenience launcher: ``python3 run.py`` (equivalent to uvicorn app.main:app)."""

from __future__ import annotations

import os
import secrets
import sys

import uvicorn

if __name__ == "__main__":
    if not os.environ.get("LOF_BOOTSTRAP_TOKEN"):
        token = secrets.token_urlsafe(24)
        os.environ["LOF_BOOTSTRAP_TOKEN"] = token
        print(
            "未设置 LOF_BOOTSTRAP_TOKEN，本次使用临时引导令牌：\n"
            f"  {token}\n"
            "一次性管理员引导：POST /api/staff/bootstrap，"
            "请求头 X-Bootstrap-Token 使用上面的值。",
            file=sys.stderr,
        )
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("LOF_RELOAD")),
    )
