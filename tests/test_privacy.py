"""Public exposure and access control."""

from __future__ import annotations

from .conftest import Staff


def test_public_view_only_shows_public_details(staff: Staff):
    item = staff.create_item(
        [
            {"code": "1", "label": "杯盖贴纸形状", "answer": "圆形"},
            {"code": "2", "label": "杯底刻字", "answer": "L.K. 2024"},
            {"code": "3", "label": "内胆颜色", "answer": "奶油白"},
        ]
    )
    public = staff.client.get(f"/api/public/items/{item['public_code']}")
    assert public.status_code == 200
    body = public.json()
    assert body["public_details"] == []
    assert "圆形" not in public.text
    assert "L.K. 2024" not in public.text
    assert "奶油白" not in public.text
    assert "details" not in body

    detail_id = next(d["id"] for d in item["details"] if d["code"] == "1")
    r = staff.post(f"/api/staff/items/{item['id']}/details/{detail_id}/publish")
    assert r.status_code == 200

    body = staff.client.get(f"/api/public/items/{item['public_code']}").json()
    assert body["public_details"] == [
        {
            "code": "1",
            "label": "杯盖贴纸形状",
            "answer": "圆形",
            "published_at": body["public_details"][0]["published_at"],
        }
    ]
    assert "L.K. 2024" not in str(body)


def test_claimant_cannot_see_internal_data(staff: Staff):
    item = staff.create_item(
        [
            {"code": "1", "label": "刻字", "answer": "MZ-07"},
            {"code": "2", "label": "贴纸", "answer": "星形"},
        ]
    )
    other_id, other = staff.create_claimant("认领者甲")
    viewer_id, viewer = staff.create_claimant("认领者乙")

    staff.client.post(
        "/api/claimant/answers",
        json={"public_code": item["public_code"], "detail_code": "1", "text": "MZ-07"},
        headers=other,
    )
    staff.client.post(
        "/api/claimant/answers",
        json={"public_code": item["public_code"], "detail_code": "2", "text": "星形"},
        headers=other,
    )
    r = staff.client.post(
        "/api/claimant/answers",
        json={"public_code": item["public_code"], "detail_code": "1", "text": "错误猜测"},
        headers=viewer,
    )
    assert r.status_code == 200
    # Response must not reveal correctness of individual answers, nor the
    # internal prompt of an unpublished detail.
    assert set(r.json()) == {
        "public_code",
        "detail_code",
        "detail_label",
        "detail_is_public",
        "submitted_text",
        "submitted_at",
        "replayed_identical_answer",
        "eligible",
    }
    assert r.json()["detail_label"] is None
    assert r.json()["detail_is_public"] is False

    status = staff.client.get(
        f"/api/claimant/items/{item['public_code']}/status", headers=viewer
    )
    assert status.status_code == 200
    body = status.json()
    assert body["eligible_for_delivery"] is False
    assert "claimants" not in body
    # Cannot see the other claimant's submission.
    assert "MZ-07" not in status.text
    assert "星形" not in status.text

    # Claimant routes reject staff-only views.
    assert (
        staff.client.get(f"/api/staff/items/{item['id']}/claimants", headers=viewer).status_code
        == 401
    )
    assert (
        staff.client.get(f"/api/staff/items/{item['id']}/claimants").status_code == 401
    )


def test_staff_routes_require_staff_token(staff: Staff):
    item = staff.create_item([])
    _, claimant_headers = staff.create_claimant("认领者丙")
    assert (
        staff.client.get(
            f"/api/staff/items/{item['id']}", headers=claimant_headers
        ).status_code
        == 401
    )
    assert staff.client.post("/api/staff/items", json={}).status_code == 401


def test_input_rejects_unknown_fields_and_blank_text(staff: Staff):
    item = staff.create_item([{"code": "1", "label": "刻字", "answer": "AB"}])
    _, headers = staff.create_claimant("认领者丁")
    r = staff.client.post(
        "/api/claimant/answers",
        json={
            "public_code": item["public_code"],
            "detail_code": "1",
            "text": "AB",
            "is_correct": True,
        },
        headers=headers,
    )
    assert r.status_code == 422
    r = staff.client.post(
        "/api/claimant/answers",
        json={"public_code": item["public_code"], "detail_code": "1", "text": "   "},
        headers=headers,
    )
    assert r.status_code == 422


def test_bootstrap_is_one_time_and_admins_can_add_operators(staff: Staff):
    from .conftest import BOOTSTRAP

    again = staff.client.post(
        "/api/staff/bootstrap",
        json={"name": "第二管理员"},
        headers={"X-Bootstrap-Token": BOOTSTRAP},
    )
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "staff_exists"

    r = staff.post("/api/staff/operators", json={"name": "同事（化名）"})
    assert r.status_code == 201
    colleague = {"Authorization": f"Bearer {r.json()['token']}"}
    assert staff.client.get("/api/staff/items", headers=colleague).status_code == 200


def test_duplicate_detail_codes_rejected(staff: Staff):
    r = staff.post(
        "/api/staff/items",
        json={
            "title": "雨伞",
            "public_description": "展厅门口拾获。",
            "found_at": "2026-11-21",
            "found_location": "一层入口",
            "details": [
                {"code": "1", "label": "伞面颜色", "answer": "藏青"},
                {"code": "1", "label": "伞柄", "answer": "木纹"},
            ],
        },
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "duplicate_detail_code"

    item = staff.create_item([{"code": "1", "label": "伞面颜色", "answer": "藏青"}])
    r = staff.post(
        f"/api/staff/items/{item['id']}/details",
        json={"code": "1", "label": "重复", "answer": "x"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "detail_code_taken"
