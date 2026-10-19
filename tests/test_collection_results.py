import json

import pytest

from backend import store as s
from tests.test_system import client, product


def finish(p, status="success", *, stamp=None, code=None, job_id=None):
    job_id = job_id or s.enqueue(p["id"])
    result = {
        "price": 12345 if status == "success" else None,
        "status": code or ("success" if status == "success" else "timeout"),
        "diagnostics": {"stage": "subprocess", "timeout_seconds": 60},
    }
    with s.connection() as db:
        db.execute(
            "UPDATE jobs SET status=?,finished_at=?,attempts=1,error=?,result=? WHERE id=?",
            (
                status,
                stamp or s.now(),
                None if status == "success" else result["status"],
                s.dumps(result),
                job_id,
            ),
        )
    return job_id


def results(client, cursor, limit=100):
    response = client.get(
        "/api/collection/results", params={"after": cursor, "limit": limit}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_baseline_skips_history_then_success_and_failure_are_delivered_once(client):
    p = product(client)
    old = finish(p)
    baseline = client.get("/api/collection/results").json()
    assert baseline["items"] == []
    assert json.loads(baseline["cursor"])[1] == old
    success = finish(p)
    failure = finish(p, "failed")
    page = results(client, baseline["cursor"])
    assert [row["id"] for row in page["items"]] == [success, failure]
    assert page["items"][0]["price"] == 12345
    assert page["items"][0]["diagnostics"]["detected_price"] == "123.45"
    failed = page["items"][1]
    assert failed["price"] is None and failed["code"] == "timeout"
    assert failed["product_name"] == p["name"] and failed["product_available"]
    assert failed["diagnostics"]["timeout_seconds"] == 60
    assert failed["diagnostics"]["message"] == "采集超过允许时间"
    assert results(client, page["cursor"])["items"] == []


def test_running_job_and_retry_do_not_notify_until_terminal(client):
    p = product(client)
    baseline = client.get("/api/collection/results").json()["cursor"]
    job = s.enqueue(p["id"])
    with s.connection() as db:
        db.execute("UPDATE jobs SET status='running',attempts=1 WHERE id=?", (job,))
    assert results(client, baseline)["items"] == []
    with s.connection() as db:
        db.execute("UPDATE jobs SET attempts=2 WHERE id=?", (job,))
    assert results(client, baseline)["items"] == []
    finish(p, "failed", job_id=job)
    assert results(client, baseline)["items"][0]["id"] == job
    cancelled = s.enqueue(p["id"])
    with s.connection() as db:
        db.execute(
            "UPDATE jobs SET status='cancelled',finished_at=? WHERE id=?",
            (s.now(), cancelled),
        )
    assert len(results(client, baseline)["items"]) == 1


def test_completion_order_not_queue_order_and_paginated_timestamps(client):
    p = product(client)
    baseline = client.get("/api/collection/results").json()["cursor"]
    stamp = s.now()
    with s.connection() as db:
        for i in reversed(range(205)):
            # Completion timestamp ties still have a stable page boundary.
            db.execute(
                "INSERT INTO jobs(id,product_id,status,created_at,finished_at,result) VALUES(?,?,?,?,?,?)",
                (
                    f"job-{i:04}",
                    p["id"],
                    "success",
                    stamp,
                    stamp,
                    s.dumps({"status": "success", "price": 10000}),
                ),
            )
    collected = []
    cursor = baseline
    while True:
        page = results(client, cursor, limit=40)
        collected.extend(row["id"] for row in page["items"])
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    assert collected == [f"job-{i:04}" for i in range(205)]
    assert results(client, cursor)["items"] == []
    # A job queued earlier but completed later must still be delivered.
    late = finish(p, "failed")
    assert results(client, cursor)["items"][0]["id"] == late


@pytest.mark.parametrize(
    "after",
    [
        "not-json",
        "[]",
        "{}",
        '["x"]',
        "[1,2]",
        '["x","y","z"]',
        json.dumps(["x" * 260, "id"]),
    ],
)
def test_rejects_bad_cursor(client, after):
    assert (
        client.get("/api/collection/results", params={"after": after}).status_code
        == 422
    )


@pytest.mark.parametrize("limit", [0, 101, -1, "abc"])
def test_page_size_is_bounded(client, limit):
    assert (
        client.get("/api/collection/results", params={"limit": limit}).status_code
        == 422
    )


def test_deleted_product_and_unexpected_failure_keep_result_visible(client):
    p = product(client)
    baseline = client.get("/api/collection/results").json()["cursor"]
    job = finish(p, "failed")
    with s.connection() as db:
        db.execute("UPDATE jobs SET error=NULL,result=NULL WHERE id=?", (job,))
        db.execute("UPDATE entities SET deleted_at=? WHERE id=?", (s.now(), p["id"]))
    item = results(client, baseline)["items"][0]
    assert item["product_name"] == p["name"]
    assert not item["product_available"]
    assert item["code"] == "crawler_error" and item["price"] is None
    assert item["diagnostics"]["message"] == "采集过程中发生异常"


def test_retired_token_cannot_read_feed_or_mutate_data(client):
    headers = {"Authorization": "Bearer read.only"}
    assert client.get("/api/collection/results", headers=headers).status_code == 401
    assert client.patch("/api/settings", json={"global_paused": True}, headers=headers).status_code == 401
