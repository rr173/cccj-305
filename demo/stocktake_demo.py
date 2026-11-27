"""Counter stocktake + discrepancy review demo.

Run from the repository root:

    python3 demo/stocktake_demo.py

Uses a throw-away SQLite database and entirely fictional data.  The story,
played at 东门服务台 while the gallery stays open:

* the shelf holds cup A, umbrella B and a newly registered bag C;
* the counter opens a round and scans A (seen) and a code of 总台's hat
  (off-counter, does not move the hat);
* meanwhile umbrella B is dispatched to 西门台: counting never locks it;
* on close, A is "seen", B is no longer here (so not a shortage) and C is
  pending review;
* 西门台's staff cannot read the round; another 东门 staff member reviews C as
  an inventory loss, which cancels its remaining booking and blocks handling;
* the counter later registers a find: the block is lifted, a fresh on-shelf
  stay starts, and the old booking is not restored.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

BOOTSTRAP = "stocktake-demo-token"


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    os.environ["LOF_BOOTSTRAP_TOKEN"] = BOOTSTRAP
    tmpdir = tempfile.TemporaryDirectory(prefix="lof-stocktake-demo-")
    app = create_app(Path(tmpdir.name) / "stocktake-demo.db")

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

        def get(path, headers):
            r = client.get(path, headers=headers)
            assert r.status_code < 400, (path, r.status_code, r.text)
            return r.json()

        def put(path, headers, body=None):
            r = client.put(path, headers=headers, json=body or {})
            assert r.status_code < 400, (path, r.status_code, r.text)
            return r.json()

        post("/api/staff/counters", ah, {"code": "EAST", "name": "东门服务台"})
        post("/api/staff/counters", ah, {"code": "WEST", "name": "西门服务台"})
        east1 = post("/api/staff/operators", ah, {"name": "东员甲", "counter_id": "EAST"})
        east2 = post("/api/staff/operators", ah, {"name": "东员乙", "counter_id": "EAST"})
        west = post("/api/staff/operators", ah, {"name": "西员（化名）", "counter_id": "WEST"})
        e1 = {"Authorization": f"Bearer {east1['token']}"}
        e2 = {"Authorization": f"Bearer {east2['token']}"}
        wh = {"Authorization": f"Bearer {west['token']}"}

        details = [
            {"code": "1", "label": "杯底刻字", "answer": "L.K. 2024"},
            {"code": "2", "label": "贴纸形状", "answer": "星星"},
        ]

        def new_item(person_headers, title):
            return post(
                "/api/staff/items",
                person_headers,
                {
                    "title": title,
                    "public_description": "演示用虚构失物。",
                    "found_at": "2026-11-20",
                    "found_location": "东门大厅",
                    "details": details,
                },
            )

        cup = new_item(e1, "深灰色保温杯")
        umbrella = new_item(e1, "黑色长柄伞")
        bag = new_item(e1, "帆布单肩包")
        hat = new_item(ah, "总台的渔夫帽")

        # A claimant becomes eligible for the bag and books it.
        claimant = post("/api/staff/claimants", e1, {"display_name": "认领者甲（化名）"})
        ch = {"Authorization": f"Bearer {claimant['token']}"}
        for code, text in (("1", "L.K. 2024"), ("2", "星星")):
            post(
                "/api/claimant/answers",
                ch,
                {"public_code": bag["public_code"], "detail_code": code, "text": text},
            )
        now = datetime.now(timezone.utc)
        slot = now.replace(minute=(now.minute // 30) * 30, second=0, microsecond=0)
        slot_iso = slot.isoformat()
        put(
            f"/api/staff/slots/{slot_iso}/roster",
            ah,
            {"entries": [{"staff_id": east1["staff_id"], "counter_ids": ["EAST"]}]},
        )
        booking = post(
            f"/api/staff/items/{bag['id']}/bookings",
            e1,
            {"claimant_id": claimant["claimant_id"], "slot_start": slot_iso},
        )
        show("预约：东门台接待时段", {"booking_id": booking["booking_id"], "slot": booking["slot"]})

        # Open a round; a second round at the same counter would be refused.
        rnd = post("/api/staff/stocktakes", e1)
        rid = rnd["round_id"]
        show("开轮", {"round_id": rid, "counter": rnd["counter"], "status": rnd["status"]})

        cup_scan = post(
            f"/api/staff/stocktakes/{rid}/scans",
            e1,
            {"request_no": "scan-cup-1", "item_ref": cup["public_code"]},
        )
        hat_scan = post(
            f"/api/staff/stocktakes/{rid}/scans",
            e1,
            {"request_no": "scan-hat-1", "item_ref": hat["public_code"]},
        )
        replay = post(
            f"/api/staff/stocktakes/{rid}/scans",
            e1,
            {"request_no": "scan-cup-1", "item_ref": cup["public_code"]},
        )
        show(
            "扫描：在柜杯=见到，他柜帽=盘外（不移动保管），同号重送返原结果",
            {
                "cup": {"classification": cup_scan["classification"],
                        "reason": cup_scan["reason"]},
                "hat": {"classification": hat_scan["classification"],
                        "reason": hat_scan["reason"]},
                "replayed": replay["replayed"],
                "hat_holder_after": get(
                    f"/api/staff/items/{hat['id']}/location", ah
                )["holding_counter"]["code"],
            },
        )

        # Counting does not lock operations: dispatch the umbrella away.
        post(
            "/api/staff/boxes",
            e1,
            {"box_no": "DEMO-BOX-1", "destination_counter_id": "WEST",
             "item_ids": [umbrella["id"]]},
        )
        post(
            "/api/staff/boxes/DEMO-BOX-1/receipts",
            wh,
            {"scanned_labels": [
                {"box_no": "DEMO-BOX-1", "item_code": umbrella["public_code"]}
            ]},
        )

        closed = post(f"/api/staff/stocktakes/{rid}/close", e1)
        show(
            "关轮：封存结算清单",
            {
                "counts": closed["counts"],
                "seen": [i["public_code"] for i in closed["seen_items"]],
                "pending_review": [
                    i["public_code"] for i in closed["pending_review_items"]
                ],
                "off_counter_scans": [s["request_no"] for s in closed["off_counter_scans"]],
                "note": "调走的伞不报短缺；关轮前新登记且未扫到的包进入待复核",
            },
        )

        # New scans are refused after close; originals are still retrievable.
        late = client.post(
            f"/api/staff/stocktakes/{rid}/scans",
            headers=e1,
            json={"request_no": "scan-late", "item_ref": bag["public_code"]},
        )
        original = get(f"/api/staff/stocktakes/{rid}/scans/scan-cup-1", e1)
        show(
            "关轮后",
            {
                "new_scan_http": late.status_code,
                "new_scan_code": late.json()["detail"]["code"],
                "original_result": {
                    "classification": original["classification"],
                    "round_closed": original["round_closed"],
                },
            },
        )

        # Another counter cannot read the discrepancy.
        outsider = client.get(f"/api/staff/stocktakes/{rid}", headers=wh)
        show(
            "可见性",
            {"west_counter_http": outsider.status_code,
             "west_counter_code": outsider.json()["detail"]["code"]},
        )

        # The closer may not review; a second EAST staff member confirms loss.
        closer_attempt = client.post(
            f"/api/staff/stocktakes/{rid}/items/{bag['id']}/review",
            headers=e1,
            json={"outcome": "loss_confirmed"},
        )
        loss = post(
            f"/api/staff/stocktakes/{rid}/items/{bag['id']}/review",
            e2,
            {"outcome": "loss_confirmed"},
        )
        status = get(f"/api/claimant/items/{bag['public_code']}/status", ch)
        blocked = client.post(
            f"/api/staff/items/{bag['id']}/bookings",
            headers=e1,
            json={"claimant_id": claimant["claimant_id"], "slot_start": slot_iso},
        )
        show(
            "确认盘亏（由另一位本柜人员复核）",
            {
                "closer_http": closer_attempt.status_code,
                "cancelled_booking_ids": loss["cancelled_booking_ids"],
                "claimant_sees": status["booking"]["ended_reason_message"],
                "rebook_http": blocked.status_code,
                "rebook_code": blocked.json()["detail"]["code"],
            },
        )

        # The counter registers a find; old booking is not restored.
        found = post(
            f"/api/staff/stocktakes/{rid}/items/{bag['id']}/find",
            e2,
            {"note": "在库房角落木箱内找到"},
        )
        after_find = get(f"/api/staff/items/{bag['id']}/location", e1)
        show(
            "登记找回",
            {
                "find_id": found["find_id"],
                "old_bookings_restored": found["old_bookings_restored"],
                "claim_processing_available": after_find["claim_processing_available"],
                "note": "按原规则重新预约办理；旧预约不自动恢复",
            },
        )

    tmpdir.cleanup()


if __name__ == "__main__":
    main()
