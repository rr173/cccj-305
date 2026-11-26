"""Cross-counter transfer + wrong-box reconciliation demo.

Run from the repository root:

    python3 demo/transfer_demo.py

Uses a throw-away SQLite database and entirely fictional data.  The story:

* 总台 sends cup X to 东门台 in box A and umbrella Y to 西门台 in box B;
* X was physically mis-packed into B;
* 西门台 opens B first: Y is normal arrival, label A/X is a wrong-box find,
  so X is isolated (quarantined) at 西门台;
* 东门台 opens A and scans nothing for X -> signed missing; the two records
  match and X is deemed kept by the counter that found it (西门台);
* the eligible claimant is then booked and served at 西门台.

Reversing the opening order gives the same final result; see
tests/test_counters_transfers.py.
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

BOOTSTRAP = "transfer-demo-token"


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    os.environ["LOF_BOOTSTRAP_TOKEN"] = BOOTSTRAP
    tmpdir = tempfile.TemporaryDirectory(prefix="lof-transfer-demo-")
    app = create_app(Path(tmpdir.name) / "transfer-demo.db")

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

        # 1. Counters and their staff --------------------------------------
        east = post("/api/staff/counters", ah, {"code": "EAST", "name": "东门服务台"})
        west = post("/api/staff/counters", ah, {"code": "WEST", "name": "西门服务台"})
        east_op = post(
            "/api/staff/operators", ah, {"name": "东员（化名）", "counter_id": "EAST"}
        )
        west_op = post(
            "/api/staff/operators", ah, {"name": "西员（化名）", "counter_id": "WEST"}
        )
        eh = {"Authorization": f"Bearer {east_op['token']}"}
        wh = {"Authorization": f"Bearer {west_op['token']}"}
        show("1. 新建两个柜台并为其指定工作人员", {
            "east": east["code"], "west": west["code"],
            "east_staff": east_op["name"], "west_staff": west_op["name"],
        })

        # 2. Two items registered at the main desk --------------------------
        cup = post("/api/staff/items", ah, {
            "title": "深灰色保温杯（演示）",
            "public_description": "二层咖啡区拾获。",
            "found_at": "2026-11-20",
            "found_location": "二层咖啡区",
            "details": [
                {"code": "1", "label": "杯底刻字", "answer": "L.K. 2024"},
                {"code": "2", "label": "贴纸形状", "answer": "星星"},
            ],
        })
        umbrella = post("/api/staff/items", ah, {
            "title": "藏青长柄伞（演示）",
            "public_description": "一层入口拾获。",
            "found_at": "2026-11-20",
            "found_location": "一层入口",
            "details": [],
        })

        claimant = post("/api/staff/claimants", ah, {"display_name": "认领者甲（化名）"})
        ch = {"Authorization": f"Bearer {claimant['token']}"}
        for code, text in [("1", "L.K. 2024"), ("2", "星星")]:
            client.post(
                "/api/claimant/answers", headers=ch,
                json={"public_code": cup["public_code"], "detail_code": code, "text": text},
            )
        now = datetime.now(timezone.utc)
        slot = now.replace(
            minute=(now.minute // 30) * 30, second=0, microsecond=0
        ).isoformat()
        main_duty = post("/api/staff/operators", ah,
                         {"name": "总台值班员（化名）", "counter_id": "MAIN"})
        east_duty = post("/api/staff/operators", ah,
                         {"name": "东门值班员（化名）", "counter_id": "EAST"})
        west_duty = post("/api/staff/operators", ah,
                         {"name": "西门值班员（化名）", "counter_id": "WEST"})
        client.put(
            f"/api/staff/slots/{slot}/roster", headers=ah,
            json={"entries": [
                {"staff_id": main_duty["staff_id"], "counter_ids": ["MAIN"]},
                {"staff_id": east_duty["staff_id"], "counter_ids": ["EAST"]},
                {"staff_id": west_duty["staff_id"], "counter_ids": ["WEST"]},
            ]},
        )
        post(f"/api/staff/items/{cup['id']}/bookings", ah,
             {"claimant_id": claimant["claimant_id"], "slot_start": slot})

        # 3. Dispatch A -> EAST with cup, B -> WEST with umbrella -----------
        box_a = post("/api/staff/boxes", ah, {
            "box_no": "BOX-A", "destination_counter_id": "EAST",
            "item_ids": [cup["id"]],
        })
        box_b = post("/api/staff/boxes", ah, {
            "box_no": "BOX-B", "destination_counter_id": "WEST",
            "item_ids": [umbrella["id"]],
        })
        show("2. 总台发出两箱；杯子的既有预约被取消且不恢复", {
            "BOX-A": box_a["destination_counter"]["name"],
            "BOX-B": box_b["destination_counter"]["name"],
            "cup_location": client.get(
                f"/api/staff/items/{cup['id']}/location", headers=ah
            ).json()["reason_code"],
        })

        # Identical retry (even different order) returns the original record.
        retry = client.post("/api/staff/boxes", headers=ah, json={
            "box_no": "BOX-A", "destination_counter_id": east["id"],
            "item_ids": [cup["id"]],
        }).json()
        assert retry["replayed"] is True

        # 4. WEST opens B and scans A/cup (mis-packed) ----------------------
        receipt_b = post("/api/staff/boxes/BOX-B/receipts", wh, {
            "scanned_labels": [
                {"box_no": "BOX-B", "item_code": umbrella["public_code"]},
                {"box_no": "BOX-A", "item_code": cup["public_code"]},
            ]
        })
        show("3. 西门台先开 B 箱：伞入库，A 箱标签的杯子被隔离", {
            "counts": receipt_b["counts"],
            "cup": client.get(f"/api/staff/items/{cup['id']}/location", headers=ah).json(),
        })

        # 5. EAST opens A with nothing -> cup signed missing -> match --------
        receipt_a = post("/api/staff/boxes/BOX-A/receipts", eh, {"scanned_labels": []})
        show("4. 东门台开 A 箱未见到杯子，登记缺件；两箱记录对上", {
            "A_counts": receipt_a["counts"],
            "cup_final": client.get(
                f"/api/staff/items/{cup['id']}/location", headers=ah
            ).json(),
        })

        # 6. Claim is handled by the finder counter (WEST) -------------------
        booking = post(f"/api/staff/items/{cup['id']}/bookings", wh,
                       {"claimant_id": claimant["claimant_id"], "slot_start": slot})
        checkin = post(
            f"/api/staff/items/{cup['id']}/bookings/{booking['booking_id']}/check-in",
            wh, {"reception_staff_id": west_duty["staff_id"]},
        )
        delivery = post(f"/api/staff/items/{cup['id']}/deliveries", wh,
                        {"claimant_id": claimant["claimant_id"]})
        show("5. 资格规则不变；由实际找到并保管杯子的西门台办理交付", {
            "booking_id": booking["booking_id"],
            "reception_staff": checkin["reception_staff"]["name"],
            "slot": delivery["slot"],
            "delivery_id": delivery["delivery_id"],
            "private_clue_count": delivery["private_clue_count"],
        })

        # 7. Box audit view --------------------------------------------------
        view = client.get("/api/staff/boxes/BOX-B", headers=ah).json()
        show("6. 箱子查询：发件、实际签收人与清点结果（原始错箱记录保留）", {
            "box_no": view["box_no"],
            "sent_by": view["dispatched_by"]["name"],
            "sender_counter": view["sender_counter"]["name"],
            "received_by": view["receipt"]["received_by"]["name"],
            "received_at_counter": view["receipt"]["receiving_counter"]["name"],
            "counts": view["receipt"]["counts"],
            "wrong_box_findings": [
                {
                    "label_box_no": f["label_box_no"],
                    "public_code": f["public_code"],
                    "find_state": f["find_state"],
                }
                for f in view["receipt"]["wrong_box_findings"]
            ],
        })

    print("\n演示完成：数据库为临时文件，已随进程清理。")


if __name__ == "__main__":
    main()
