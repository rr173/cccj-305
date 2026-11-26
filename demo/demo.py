"""End-to-end demonstration with two claimants and no real personal data.

Run from the repository root:

    python3 demo/demo.py

The script uses a throw-away SQLite database under ``data/`` and prints each
step and the relevant API response.  It covers:

* two claimants who knew answers *before* a detail was published;
* publication of a detail without invalidating earlier correct answers;
* staff booking one of two eligible claimants;
* a correction that invalidates earlier answers and auto-cancels the booking;
* re-selection of the other claimant after a fresh, valid submission;
* delivery, then a post-delivery correction that leaves history intact.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

BOOTSTRAP = "demo-bootstrap-token"


def show(title: str, payload) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    os.environ["LOF_BOOTSTRAP_TOKEN"] = BOOTSTRAP
    tmpdir = tempfile.TemporaryDirectory(prefix="lof-demo-")
    db = Path(tmpdir.name) / "demo.db"
    app = create_app(db)

    with TestClient(app) as client:
        staff = client.post(
            "/api/staff/bootstrap",
            json={"name": "值班员（演示）"},
            headers={"X-Bootstrap-Token": BOOTSTRAP},
        ).json()
        sh = {"Authorization": f"Bearer {staff['token']}"}

        def make_claimant(name: str):
            data = client.post(
                "/api/staff/claimants", json={"display_name": name}, headers=sh
            ).json()
            return data["claimant_id"], {"Authorization": f"Bearer {data['token']}"}

        def claimant_answer(headers, public_code: str, code: str, text: str):
            r = client.post(
                "/api/claimant/answers",
                json={"public_code": public_code, "detail_code": code, "text": text},
                headers=headers,
            )
            return r.status_code, r.json()

        def claimant_status(headers, public_code: str):
            return client.get(
                f"/api/claimant/items/{public_code}/status", headers=headers
            ).json()

        # -- 1. Staff registers the item and three private details ----------
        item = client.post(
            "/api/staff/items",
            headers=sh,
            json={
                "title": "深灰色保温杯（演示）",
                "public_description": "二层咖啡区拾获，杯身有使用痕迹。",
                "found_at": "2026-11-20",
                "found_location": "二层咖啡区",
                "details": [
                    {"code": "1", "label": "杯底刻字", "answer": "L.K. 2024"},
                    {"code": "2", "label": "贴纸形状", "answer": "星星"},
                    {"code": "3", "label": "内胆颜色", "answer": "奶油白"},
                ],
            },
        ).json()
        code = item["public_code"]
        detail_ids = {d["code"]: d["id"] for d in item["details"]}
        show(
            "1. 工作人员登记失物（细节全部未公开）",
            {
                "public_code": code,
                "details": [
                    {
                        "code": d["code"],
                        "label": d["label"],
                        "is_public": d["is_public"],
                        "answer_hidden": "***",
                    }
                    for d in item["details"]
                ],
            },
        )

        a_id, a_h = make_claimant("认领者甲（化名）")
        b_id, b_h = make_claimant("认领者乙（化名）")

        # -- 2. Both claimants know answers BEFORE publication --------------
        for headers in (a_h, b_h):
            claimant_answer(headers, code, "1", "L.K. 2024")
            claimant_answer(headers, code, "2", "星星")
        show(
            "2. 公开前，甲乙分别提交两条正确答案",
            {
                "甲": claimant_status(a_h, code)["eligible_for_delivery"],
                "乙": claimant_status(b_h, code)["eligible_for_delivery"],
                "说明": "两条不同的有效私有线索，均具备到场交付资格。",
            },
        )

        # -- 3. Publish one detail -----------------------------------------
        client.post(
            f"/api/staff/items/{item['id']}/details/{detail_ids['2']}/publish",
            headers=sh,
        )
        public = client.get(f"/api/public/items/{code}").json()
        show(
            "3. 公开“贴纸形状”用于寻找主人",
            {
                "公网可见": public["public_details"],
                "甲仍达标（公开前回答仍有效）": claimant_status(a_h, code)[
                    "eligible_for_delivery"
                ],
                "乙仍达标（公开前回答仍有效）": claimant_status(b_h, code)[
                    "eligible_for_delivery"
                ],
            },
        )

        # -- 4. Staff books claimant A -------------------------------------
        booking_a = client.post(
            f"/api/staff/items/{item['id']}/bookings",
            json={"claimant_id": a_id},
            headers=sh,
        ).json()
        show(
            "4. 两人都达标，工作人员选择甲安排到场（实物仍由馆方持有）",
            {
                "booking": booking_a,
                "item_status": client.get(f"/api/public/items/{code}").json()["status"],
            },
        )

        # -- 5. Staff corrects detail 1 ------------------------------------
        correction = client.post(
            f"/api/staff/items/{item['id']}/details/{detail_ids['1']}/correct",
            json={"answer": "L.K. 2025"},
            headers=sh,
        ).json()
        status_a = claimant_status(a_h, code)
        show(
            "5. 工作人员更正“杯底刻字”，甲此前回答全部失效，预约自动取消",
            {
                "correction": correction,
                "甲的状态": {
                    "eligible": status_a["eligible_for_delivery"],
                    "booking": status_a["booking"],
                },
                "乙的状态": {
                    "eligible": claimant_status(b_h, code)["eligible_for_delivery"],
                },
            },
        )

        # Delivery attempt fails: booking is gone and evidence invalid.
        delivery_attempt = client.post(
            f"/api/staff/items/{item['id']}/deliveries",
            json={"claimant_id": a_id},
            headers=sh,
        )
        show(
            "6. 尝试向甲交付：被拒绝",
            {
                "status_code": delivery_attempt.status_code,
                "error": delivery_attempt.json()["detail"],
            },
        )

        # A resends the same old text: still the invalidated answer row.
        rc, replay = claimant_answer(a_h, code, "1", "L.K. 2024")
        show(
            "7. 甲原样重送旧答案（不刷新提交时点，仍无效）",
            {
                "status_code": rc,
                "replayed_identical_answer": replay["replayed_identical_answer"],
                "eligible": replay["eligible"],
            },
        )

        # B changes the answer -> new submission, judged at the new time.
        claimant_answer(b_h, code, "1", "L.K. 2025")
        show(
            "8. 乙改答更正后的刻字内容：按新提交时点判断，重新达标",
            {"乙": claimant_status(b_h, code)["eligible_for_delivery"]},
        )

        # -- 9. Staff reselects B ------------------------------------------
        booking_b = client.post(
            f"/api/staff/items/{item['id']}/bookings",
            json={"claimant_id": b_id},
            headers=sh,
        ).json()
        show("9. 工作人员重新选择乙安排到场", booking_b)

        delivery = client.post(
            f"/api/staff/items/{item['id']}/deliveries",
            json={"claimant_id": b_id},
            headers=sh,
        ).json()
        show(
            "10. 交付给乙（交付时复核预约人、当前资格与线索快照）",
            {
                "claimant_display_name": delivery["claimant_display_name"],
                "private_clue_count": delivery["private_clue_count"],
                "evidence": [
                    {
                        "detail_code": e["detail_code"],
                        "submitted_text": e["submitted_text"],
                        "answer_at_delivery": e["answer_at_delivery"],
                        "was_public_at_delivery": e["was_public_at_delivery"],
                    }
                    for e in delivery["evidence"]
                ],
            },
        )

        # Second delivery to A is impossible.
        second = client.post(
            f"/api/staff/items/{item['id']}/deliveries",
            json={"claimant_id": a_id},
            headers=sh,
        )
        show(
            "11. 不能再向甲（或任何第二人）交付",
            {"status_code": second.status_code, "error": second.json()["detail"]},
        )

        # -- 12. Post-delivery correction keeps history --------------------
        client.post(
            f"/api/staff/items/{item['id']}/details/{detail_ids['2']}/correct",
            json={"answer": "五角星"},
            headers=sh,
        )
        history = client.get(
            f"/api/staff/items/{item['id']}/delivery", headers=sh
        ).json()
        show(
            "12. 交付后更正“贴纸形状”：历史交付不变，仅标注后来失效的依据",
            {
                "claimant_id": history["claimant_id"],
                "delivered_at": history["delivered_at"],
                "has_later_invalidated_evidence": history[
                    "has_later_invalidated_evidence"
                ],
                "evidence": [
                    {
                        "detail_code": e["detail_code"],
                        "answer_at_delivery": e["answer_at_delivery"],
                        "current_answer": e["current_answer"],
                        "later_invalidated": e["later_invalidated"],
                    }
                    for e in history["evidence"]
                ],
            },
        )

    print("\n演示完成：数据库为临时文件，已随进程清理。")


if __name__ == "__main__":
    main()
