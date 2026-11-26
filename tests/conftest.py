from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402

BOOTSTRAP = "test-bootstrap-token-123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOF_BOOTSTRAP_TOKEN", BOOTSTRAP)
    app = create_app(tmp_path / "test.db")
    with TestClient(app) as c:
        yield c


class Staff:
    def __init__(self, client: TestClient, token: str):
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}"}

    def post(self, path: str, **kwargs):
        return self.client.post(path, headers=self.headers, **kwargs)

    def get(self, path: str, **kwargs):
        return self.client.get(path, headers=self.headers, **kwargs)

    def create_claimant(self, display_name: str) -> tuple[str, dict]:
        r = self.post("/api/staff/claimants", json={"display_name": display_name})
        assert r.status_code == 201, r.text
        data = r.json()
        return data["claimant_id"], {"Authorization": f"Bearer {data['token']}"}

    def create_item(self, details, **overrides) -> dict:
        payload = {
            "title": "深灰色保温杯",
            "public_description": "馆内咖啡区拾获，杯身有使用痕迹。",
            "found_at": "2026-11-20",
            "found_location": "二层咖啡区",
            "details": details,
        }
        payload.update(overrides)
        r = self.post("/api/staff/items", json=payload)
        assert r.status_code == 201, r.text
        return r.json()


@pytest.fixture()
def staff(client):
    r = client.post(
        "/api/staff/bootstrap",
        json={"name": "当班工作人员"},
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )
    assert r.status_code == 201, r.text
    return Staff(client, r.json()["token"])
