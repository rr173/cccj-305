from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
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


def _aligned(dt: datetime) -> datetime:
    return dt.replace(minute=(dt.minute // 30) * 30, second=0, microsecond=0)


def current_slot_start() -> str:
    """ISO start of the UTC half-hour that is running right now."""
    return _aligned(datetime.now(timezone.utc)).isoformat()


def next_slot_start(offset_minutes: int = 30) -> str:
    """ISO start of a later UTC half-hour boundary."""
    return (
        _aligned(datetime.now(timezone.utc)) + timedelta(minutes=offset_minutes)
    ).isoformat()


def past_slot_start(offset_minutes: int = 30) -> str:
    """ISO start of an already-ended UTC half-hour slot."""
    return (
        _aligned(datetime.now(timezone.utc)) - timedelta(minutes=offset_minutes)
    ).isoformat()


class Staff:
    def __init__(self, client: TestClient, token: str, staff_id: str | None = None):
        self.client = client
        self.staff_id = staff_id
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

    def create_counter(self, code: str, name: str) -> dict:
        r = self.post("/api/staff/counters", json={"code": code, "name": name})
        assert r.status_code == 201, r.text
        return r.json()

    def create_operator(self, counter_ref: str | None = None, name: str = "驻台员") -> "Staff":
        payload = {"name": name}
        if counter_ref is not None:
            payload["counter_id"] = counter_ref
        r = self.post("/api/staff/operators", json=payload)
        assert r.status_code == 201, r.text
        data = r.json()
        return Staff(self.client, data["token"], data["staff_id"])

    def set_roster(self, slot_ref: str, entries: list[dict]):
        """entries: [{"staff_id": ..., "counter_ids": [...]}]."""
        return self.put(f"/api/staff/slots/{slot_ref}/roster", json={"entries": entries })

    def put(self, path: str, **kwargs):
        return self.client.put(path, headers=self.headers, **kwargs)

    # ---- bookings ------------------------------------------------------
    def book(self, item_id: str, claimant_id: str, slot_ref: str | None = None):
        payload = {"claimant_id": claimant_id}
        if slot_ref is not None:
            payload["slot_start"] = slot_ref
        return self.post(f"/api/staff/items/{item_id}/bookings", json=payload)

    def checkin(self, item_id: str, booking_id: int, reception_staff_id: str):
        return self.post(
            f"/api/staff/items/{item_id}/bookings/{booking_id}/check-in",
            json={"reception_staff_id": reception_staff_id},
        )

    def deliver(self, item_id: str, claimant_id: str):
        return self.post(
            f"/api/staff/items/{item_id}/deliveries", json={"claimant_id": claimant_id}
        )


@pytest.fixture()
def staff(client):
    r = client.post(
        "/api/staff/bootstrap",
        json={"name": "当班工作人员"},
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )
    assert r.status_code == 201, r.text
    return Staff(client, r.json()["token"], r.json()["staff_id"])


def eligible_claimant(staff: Staff, item: dict, name: str = "认领者") -> tuple[str, dict]:
    claimant_id, headers = staff.create_claimant(name)
    for code, value in (("1", "L.K. 2024"), ("2", "星星")):
        r = staff.client.post(
            "/api/claimant/answers",
            json={
                "public_code": item["public_code"],
                "detail_code": code,
                "text": value,
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
    return claimant_id, headers


def setUpReception(
    staff: Staff,
    slot_ref: str,
    people: list[tuple[Staff, str | list[str]]],
) -> None:
    """Roster one or more staff on one or more counters for a whole slot."""
    entries = []
    for person, counters in people:
        counter_ids = [counters] if isinstance(counters, str) else list(counters)
        entries.append({"staff_id": person.staff_id, "counter_ids": counter_ids})
    r = staff.set_roster(slot_ref, entries)
    assert r.status_code == 200, r.text
