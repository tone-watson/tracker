from datetime import datetime, timezone

import pytest

from tracker.app import create_app
from tracker.auth import Principal
from tracker.errors import TrackerError


class TestAuth:
    __test__ = False

    def authenticate(self, token, scope):
        if token not in {"Bearer read", "Bearer write"}:
            raise TrackerError("invalid_token", "Missing token", 401)
        if scope == "todos.write" and token != "Bearer write":
            raise TrackerError("insufficient_scope", "Write permission required", 403)
        return Principal("1", "test", "test-session", frozenset({scope}))


@pytest.fixture
def client(tmp_path):
    app = create_app(
        tmp_path / "test.db", TestAuth(), lambda: datetime(2026, 10, 22, tzinfo=timezone.utc)
    )
    app.testing = True
    return app.test_client()


def test_scope_checks_apply_to_all_routes_and_health_is_public(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/api/v1/lists").status_code == 401
    assert (
        client.get("/api/capabilities", headers={"Authorization": "Bearer read"}).status_code == 200
    )
    denied = client.post(
        "/api/v1/lists",
        json={"name": "X", "idempotency_key": "x"},
        headers={"Authorization": "Bearer read"},
    )
    assert denied.status_code == 403
    assert (
        client.get("/api/v1/lists", headers={"Authorization": "Bearer read"}).json["data"]["items"]
        == []
    )


def test_http_create_update_history_and_overdue_filter(client):
    headers = {"Authorization": "Bearer write"}
    todo_list = client.post(
        "/api/v1/lists", json={"name": "Work", "idempotency_key": "work"}, headers=headers
    ).json["data"]
    task = client.post(
        "/api/v1/items",
        json={
            "list_id": todo_list["id"],
            "title": "Call",
            "due": {"value": "2026-10-20"},
            "idempotency_key": "call",
        },
        headers=headers,
    ).json["data"]
    assert (
        client.get("/api/v1/items?overdue_only=true", headers=headers).json["data"]["items"][0][
            "id"
        ]
        == task["id"]
    )
    response = client.put(
        "/api/v1/items/" + task["id"],
        json={"expected_revision": task["revision"], "status": "completed"},
        headers=headers,
    )
    assert response.status_code == 200 and response.json["data"]["completed_at"]
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Request-ID"]
    assert (
        len(
            client.get("/api/v1/items/" + task["id"] + "/history", headers=headers).json["data"][
                "items"
            ]
        )
        == 2
    )
    assert (
        client.get("/api/v1/items?overdue_only=true", headers=headers).json["data"]["items"] == []
    )


@pytest.mark.parametrize(
    "body",
    [
        [],
        None,
        {"name": "X", "idempotency_key": "x", "extra": True},
        {"name": "X", "idempotency_key": "x", "description": float("nan")},
    ],
)
def test_invalid_http_input_has_safe_json_error(client, body):
    response = client.post("/api/v1/lists", json=body, headers={"Authorization": "Bearer write"})
    assert 400 <= response.status_code < 500
    assert "code" in response.json["error"]


def test_request_body_bound_and_query_validation(client):
    headers = {"Authorization": "Bearer write"}
    assert (
        client.post(
            "/api/v1/lists",
            data="x" * (256 * 1024 + 1),
            content_type="application/json",
            headers=headers,
        ).status_code
        == 413
    )
    for query in (
        "limit=0",
        "limit=100",
        "limit=1&limit=2",
        "overdue_only=1",
        "cursor=bad",
        "unknown=x",
    ):
        assert client.get("/api/v1/items?" + query, headers=headers).status_code == 400


def test_reads_never_materialize_recurrence(client):
    headers = {"Authorization": "Bearer write"}
    todo_list = client.post(
        "/api/v1/lists", json={"name": "X", "idempotency_key": "x"}, headers=headers
    ).json["data"]
    client.post(
        "/api/v1/items",
        json={
            "list_id": todo_list["id"],
            "title": "Daily",
            "due": {"value": "2026-10-01"},
            "recurrence": {"mode": "calendar", "rule": "FREQ=DAILY"},
            "idempotency_key": "daily",
        },
        headers=headers,
    )
    for _ in range(3):
        assert len(client.get("/api/v1/items", headers=headers).json["data"]["items"]) == 1
