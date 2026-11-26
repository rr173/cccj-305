"""Reception staffing demo: shared duty people, slots, check-in, shrink.

Run from the repository root:

    python3 demo/reception_demo.py

Uses a throw-away SQLite database and entirely fictional data.  The story:

* three counters (总台 / 东门台 / 西门台) share two duty people;
* one duty person is rostered at two counters in the same slot, but is only
  one seat, so the second booking that would need them is refused (and no
  earlier booking is cancelled);
* a second duty person is added to the roster, and the booking is accepted;
* both claimants arrive; the holding counter registers the actual person,
  and the second claimant cannot borrow the first person's used seat;
* the roster temporarily shrinks; the check-in stays, the earliest remaining
  appointment is kept, the later one is cancelled with reason
  ``roster_changed`` and never comes back when the roster is restored;
* the claimant sees only their own counter, slot and cancellation reason.

Reversing several of these steps is covered by
tests/test_reception_slots.py.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

BOOTSTRAP = "reception-demo-token"


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    os.environ["LOF_BOOTSTRAP_TOKEN"] = BOOTSTRAP
    tmpdir = tempfile.TemporaryDirectory(prefix="lof-reception-demo-")
    app = create_app(Path(tmpdir.name) / "reception-demo.db")

    with TestClient(app) as client:
        admin = client.post(
            "/api/staff/bootstrap",
            json={"name": "总台管理员（演示）"},
            headers={"X-Bootstrap-Token": BOOTSTRAP},
        ).json()
        ah = {"Authorization": f"Bearer {admin['token']}"}

        def post(path, headers, body=None):
            r = client.post(path, headers=headers, json=body or {})
            assert r.status_code < 400, (path, r.status_code, r.text)
            return r.json()

        def claimant(name: str):
            data = post("/api/staff/claimants", ah, {"display_name": name})
            return data["claimant_id"], {"Authorization": f"Bearer {data['token']}"}

        def make_eligible(item, headers):
            for code, text in (("1", "L.K. 2024"), ("2", "星星")):
                client.post(
                    "/api/claimant/answers",
                    headers=headers,
                    json={
                        "public_code": item["public_code"],
                        "detail_code": code,
                        "text": text,
                    },
                )

        # 1. Counters and two shared duty people ----------------------------
        post("/api/staff/counters", ah, {"code": "EAST", "name": "东门服务台"})
        post("/api/staff/counters", ah, {"code": "WEST", "name": "西门服务台"})
        duty_1 = post(
            "/api/staff/operators", ah,
            {"name": "值班员甲（化名）", "counter_id": "MAIN"},
        )
        duty_2 = post(
            "/api/staff/operators", ah,
            {"name": "值班员乙（化名）", "counter_id": "EAST"},
        )
        show("1. 三个柜台共用两名值班人员（各自仍在原来的所属柜台）", {
            "duty_1": {"name": duty_1["name"], "home": duty_1["counter"]["code"]},
            "duty_2": {"name": duty_2["name"], "home": duty_2["counter"]["code"]},
        })

        now = datetime.now(timezone.utc)
        slot = now.replace(
            minute=(now.minute // 30) * 30, second=0, microsecond=0
        ).isoformat()

        # 2. One person on two counters is still ONE seat -------------------
        view = client.put(
            f"/api/staff/slots/{slot}/roster", headers=ah,
            json={"entries": [
                {"staff_id": duty_1["staff_id"], "counter_ids": ["MAIN", "EAST"]},
            ]},
        ).json()
        show("2. 名单写入：同一人本时段可在总台和东门台接待，但只有一个名额", {
            "slot": view["slot"],
            "entries": view["entries"],
            "seat_count": view["seat_count"],
        })

        item_main = post("/api/staff/items", ah, {
            "title": "深灰色保温杯（演示）",
            "public_description": "二层咖啡区拾获。",
            "found_at": "2026-11-20",
            "found_location": "二层咖啡区",
            "details": [
                {"code": "1", "label": "杯底刻字", "answer": "L.K. 2024"},
                {"code": "2", "label": "贴纸形状", "answer": "星星"},
            ],
        })
        east_desk = post("/api/staff/operators", ah,
                         {"name": "东门登记员（化名）", "counter_id": "EAST"})
        eh = {"Authorization": f"Bearer {east_desk['token']}"}
        item_east = post("/api/staff/items", eh, {
            "title": "藏青长柄伞（演示）",
            "public_description": "一层入口拾获。",
            "found_at": "2026-11-20",
            "found_location": "一层入口",
            "details": [
                {"code": "1", "label": "伞柄刻字", "answer": "L.K. 2024"},
                {"code": "2", "label": "伞面图案", "answer": "星星"},
            ],
        })
        c1, h1 = claimant("认领者甲（化名）")
        c2, h2 = claimant("认领者乙（化名）")
        make_eligible(item_main, h1)
        make_eligible(item_east, h2)

        booking_1 = post(
            f"/api/staff/items/{item_main['id']}/bookings", ah,
            {"claimant_id": c1, "slot_start": slot},
        )
        show("3. 总台确认甲的预约：该时段只有一个名额可用", {
            "booking_id": booking_1["booking_id"],
            "slot": booking_1["slot"],
            "counter": booking_1["counter"],
            "available_reception_people": booking_1["available_reception_people"],
        })

        r = client.post(
            f"/api/staff/items/{item_east['id']}/bookings", headers=eh,
            json={"claimant_id": c2, "slot_start": slot},
        )
        show("4. 东门台尝试为乙预约：同一个值班人已被甲占用，拒绝且不取消甲的预约", {
            "status_code": r.status_code,
            "error": r.json()["detail"],
            "甲仍有效": [
                b["status"]
                for b in client.get(
                    f"/api/staff/items/{item_main['id']}/bookings", headers=ah
                ).json()["bookings"]
            ],
        })

        # 3. Add a second person to the roster -----------------------------
        client.put(
            f"/api/staff/slots/{slot}/roster", headers=ah,
            json={"entries": [
                {"staff_id": duty_1["staff_id"], "counter_ids": ["MAIN", "EAST"]},
                {"staff_id": duty_2["staff_id"], "counter_ids": ["EAST"]},
            ]},
        )
        booking_2 = post(
            f"/api/staff/items/{item_east['id']}/bookings", eh,
            {"claimant_id": c2, "slot_start": slot},
        )
        show("5. 增加第二名值班人后，乙的预约可以确认，旧预约全部保留", {
            "booking_id": booking_2["booking_id"],
            "available_reception_people": booking_2["available_reception_people"],
        })

        # 4. Arrivals and actual reception people ---------------------------
        checkin_1 = post(
            f"/api/staff/items/{item_main['id']}/bookings/"
            f"{booking_1['booking_id']}/check-in",
            ah, {"reception_staff_id": duty_1["staff_id"]},
        )
        r = client.post(
            f"/api/staff/items/{item_east['id']}/bookings/"
            f"{booking_2['booking_id']}/check-in",
            headers=eh, json={"reception_staff_id": duty_1["staff_id"]},
        )
        show("6. 甲在总台报到，值班员甲登记；乙若也想用同一个人，被拒绝", {
            "甲的接待人": checkin_1["reception_staff"]["name"],
            "乙用同一个人": {"status_code": r.status_code, "error": r.json()["detail"]},
        })
        checkin_2 = post(
            f"/api/staff/items/{item_east['id']}/bookings/"
            f"{booking_2['booking_id']}/check-in",
            eh, {"reception_staff_id": duty_2["staff_id"]},
        )
        show("7. 乙改由值班员乙接待，登记成功", {
            "乙的接待人": checkin_2["reception_staff"]["name"],
        })

        # 5. Shrink the roster ---------------------------------------------
        # Give a third future-ready item so the story can show a booking that
        # must be cancelled on shrink.  Here we simply add a third booking to
        # the same slot, then remove a person.
        duty_3 = post(
            "/api/staff/operators", ah,
            {"name": "值班员丙（化名）", "counter_id": "MAIN"},
        )
        client.put(
            f"/api/staff/slots/{slot}/roster", headers=ah,
            json={"entries": [
                {"staff_id": duty_1["staff_id"], "counter_ids": ["MAIN", "EAST"]},
                {"staff_id": duty_2["staff_id"], "counter_ids": ["EAST"]},
                {"staff_id": duty_3["staff_id"], "counter_ids": ["MAIN"]},
            ]},
        )
        item_extra = post("/api/staff/items", ah, {
            "title": "银色钥匙扣（演示）",
            "public_description": "服务台拾获。",
            "found_at": "2026-11-20",
            "found_location": "服务台",
            "details": [
                {"code": "1", "label": "刻字", "answer": "L.K. 2024"},
                {"code": "2", "label": "图案", "answer": "星星"},
            ],
        })
        c3, h3 = claimant("认领者丙（化名）")
        make_eligible(item_extra, h3)
        booking_3 = post(
            f"/api/staff/items/{item_extra['id']}/bookings", ah,
            {"claimant_id": c3, "slot_start": slot},
        )

        # Remove the third (still free) person: the two checked-in seats stay
        # occupied, so the un-checked-in booking has no free seat left.
        shrink = client.put(
            f"/api/staff/slots/{slot}/roster", headers=ah,
            json={"entries": [
                {"staff_id": duty_1["staff_id"], "counter_ids": ["MAIN", "EAST"]},
                {"staff_id": duty_2["staff_id"], "counter_ids": ["EAST"]},
            ]},
        ).json()
        show("8. 名单临时缩减：已报到占用不动，未报到的较晚预约被取消（不自动恢复）", {
            "cancelled_bookings": shrink["cancelled_bookings"],
            "booking_3": booking_3["booking_id"],
        })

        # Restoring the roster does not bring the cancelled booking back.
        client.put(
            f"/api/staff/slots/{slot}/roster", headers=ah,
            json={"entries": [
                {"staff_id": duty_1["staff_id"], "counter_ids": ["MAIN", "EAST"]},
                {"staff_id": duty_2["staff_id"], "counter_ids": ["EAST"]},
                {"staff_id": duty_3["staff_id"], "counter_ids": ["MAIN"]},
            ]},
        )
        restored = client.get(
            f"/api/staff/items/{item_extra['id']}/bookings", headers=ah
        ).json()["bookings"]
        show("8b. 名单恢复后，已取消的预约仍保持取消", {
            "booking_3_status": restored[0]["status"],
            "ended_reason": restored[0]["ended_reason"],
        })

        status_3 = client.get(
            f"/api/claimant/items/{item_extra['public_code']}/status", headers=h3
        ).json()
        show("9. 认领者丙只看到自己的柜台、时段与取消原因，看不到名单或他人预约", {
            "booking": status_3["booking"],
        })

        # 6. Deliver the two checked-in bookings ---------------------------
        delivery_1 = post(
            f"/api/staff/items/{item_main['id']}/deliveries", ah,
            {"claimant_id": c1},
        )
        delivery_2 = post(
            f"/api/staff/items/{item_east['id']}/deliveries", eh,
            {"claimant_id": c2},
        )
        show("10. 两笔已报到预约在时段内交付；接待人与时段写入交付历史", {
            "delivery_1": {
                "reception_staff": delivery_1["reception_staff"]["name"],
                "slot": delivery_1["slot"],
            },
            "delivery_2": {
                "reception_staff": delivery_2["reception_staff"]["name"],
                "slot": delivery_2["slot"],
            },
        })

    print("\n演示完成：数据库为临时文件，已随进程清理。")


if __name__ == "__main__":
    main()
