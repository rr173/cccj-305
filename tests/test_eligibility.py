"""Eligibility rules: stripping, publication timing, resubmission, corrections."""

from __future__ import annotations

from .conftest import Staff

DETAILS = [
    {"code": "1", "label": "杯底刻字", "answer": "L.K. 2024"},
    {"code": "2", "label": "贴纸形状", "answer": "星星"},
    {"code": "3", "label": "内胆颜色", "answer": "奶油白"},
]


def answer(staff: Staff, headers: dict, item: dict, code: str, text: str):
    return staff.client.post(
        "/api/claimant/answers",
        json={"public_code": item["public_code"], "detail_code": code, "text": text},
        headers=headers,
    )


def status(staff: Staff, headers: dict, item: dict) -> dict:
    r = staff.client.get(
        f"/api/claimant/items/{item['public_code']}/status", headers=headers
    )
    assert r.status_code == 200
    return r.json()


def detail_id(item: dict, code: str) -> str:
    return next(d["id"] for d in item["details"] if d["code"] == code)


def test_two_distinct_private_clues_required(staff: Staff):
    item = staff.create_item(DETAILS)
    _, headers = staff.create_claimant("认领者一")

    assert status(staff, headers, item)["eligible_for_delivery"] is False
    answer(staff, headers, item, "1", "L.K. 2024")
    assert status(staff, headers, item)["eligible_for_delivery"] is False
    # Replaying the same detail (identical text) cannot count as a second clue.
    answer(staff, headers, item, "1", "  L.K. 2024  ")
    assert status(staff, headers, item)["eligible_for_delivery"] is False
    answer(staff, headers, item, "2", "星星")
    body = status(staff, headers, item)
    assert body["eligible_for_delivery"] is True
    assert body["booking"] is None


def test_comparison_strips_surrounding_whitespace_only(staff: Staff):
    item = staff.create_item(DETAILS)
    _, headers = staff.create_claimant("认领者空白")

    answer(staff, headers, item, "1", "   L.K. 2024\t")
    body = status(staff, headers, item)
    assert body["eligible_for_delivery"] is False  # only one clue so far
    answer(staff, headers, item, "2", "  星星\n")
    assert status(staff, headers, item)["eligible_for_delivery"] is True

    _, other = staff.create_claimant("认领者大小写")
    answer(staff, other, item, "1", "l.k. 2024")  # case differs -> not equal
    answer(staff, other, item, "2", "星 星")  # inner space differs -> not equal
    assert status(staff, other, item)["eligible_for_delivery"] is False


def test_replay_does_not_refresh_submission_time(staff: Staff):
    item = staff.create_item(DETAILS)
    _, headers = staff.create_claimant("认领者重送")

    first = answer(staff, headers, item, "1", " L.K. 2024 ").json()
    assert first["replayed_identical_answer"] is False
    second = answer(staff, headers, item, "1", "L.K. 2024").json()
    assert second["replayed_identical_answer"] is True
    assert second["submitted_at"] == first["submitted_at"]

    detail = detail_id(item, "1")
    staff.post(f"/api/staff/items/{item['id']}/details/{detail}/publish")

    # Replaying the original answer after publication must NOT create a valid
    # private clue, because the submission time stays before publication.
    answer(staff, headers, item, "1", "L.K. 2024")
    answer(staff, headers, item, "2", "星星")
    assert status(staff, headers, item)["eligible_for_delivery"] is True


def test_answer_after_publication_is_not_private_clue(staff: Staff):
    item = staff.create_item(DETAILS)
    _, headers = staff.create_claimant("认领者公开后")

    staff.post(
        f"/api/staff/items/{item['id']}/details/{detail_id(item, '1')}/publish"
    )
    answer(staff, headers, item, "1", "L.K. 2024")  # correct but public
    answer(staff, headers, item, "2", "星星")
    assert status(staff, headers, item)["eligible_for_delivery"] is False

    view = staff.get(f"/api/staff/items/{item['id']}/claimants").json()
    rows = {a["detail_code"]: a for a in view["claimants"][0]["answers"]}
    assert rows["1"]["clue_status"] == "correct_but_public"
    assert rows["1"]["counts_as_private_clue"] is False
    assert rows["2"]["clue_status"] == "valid_private"


def test_changing_answer_uses_new_submission_time(staff: Staff):
    item = staff.create_item(DETAILS)
    _, headers = staff.create_claimant("认领者改答")

    answer(staff, headers, item, "1", "错的")
    staff.post(
        f"/api/staff/items/{item['id']}/details/{detail_id(item, '1')}/publish"
    )
    answer(staff, headers, item, "1", "L.K. 2024")  # now correct, after publish
    answer(staff, headers, item, "2", "星星")
    assert status(staff, headers, item)["eligible_for_delivery"] is False


def test_correction_permanently_invalidates_earlier_answers(staff: Staff):
    item = staff.create_item(DETAILS)
    claimant_id, headers = staff.create_claimant("认领者更正")
    d1 = detail_id(item, "1")

    answer(staff, headers, item, "1", "L.K. 2024")
    answer(staff, headers, item, "2", "星星")
    assert status(staff, headers, item)["eligible_for_delivery"] is True

    r = staff.post(
        f"/api/staff/items/{item['id']}/details/{d1}/correct",
        json={"answer": "L.K.2025"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True
    assert status(staff, headers, item)["eligible_for_delivery"] is False

    # Changing back to the original answer must not restore the old answer.
    staff.post(
        f"/api/staff/items/{item['id']}/details/{d1}/correct",
        json={"answer": "L.K. 2024"},
    )
    # Re-sending the same old text is a replay of the permanently invalid row.
    answer(staff, headers, item, "1", "L.K. 2024")
    assert status(staff, headers, item)["eligible_for_delivery"] is False

    # A changed answer is a new submission and is judged at the new time.
    answer(staff, headers, item, "1", "先随便改一个")
    answer(staff, headers, item, "1", "L.K. 2024")
    assert status(staff, headers, item)["eligible_for_delivery"] is True


def test_noop_correction_does_not_invalidate(staff: Staff):
    item = staff.create_item(DETAILS)
    _, headers = staff.create_claimant("认领者无变化")
    answer(staff, headers, item, "1", "L.K. 2024")
    answer(staff, headers, item, "2", "星星")
    dl = detail_id(item, "1")
    r = staff.post(
        f"/api/staff/items/{item['id']}/details/{dl}/correct",
        json={"answer": "  L.K. 2024  "},  # same after stripping
    )
    assert r.status_code == 200
    assert r.json()["changed"] is False
    assert status(staff, headers, item)["eligible_for_delivery"] is True


def test_correct_answer_submitted_before_publication_survives_publication(staff: Staff):
    """An answer submitted BEFORE publication stays a valid private clue."""
    item = staff.create_item(DETAILS)
    _, headers = staff.create_claimant("认领者公开前")
    answer(staff, headers, item, "1", "L.K. 2024")
    staff.post(
        f"/api/staff/items/{item['id']}/details/{detail_id(item, '1')}/publish"
    )
    answer(staff, headers, item, "2", "星星")
    body = status(staff, headers, item)
    assert body["eligible_for_delivery"] is True

    view = staff.get(f"/api/staff/items/{item['id']}/claimants").json()
    rows = {a["detail_code"]: a for a in view["claimants"][0]["answers"]}
    assert rows["1"]["clue_status"] == "valid_private"
