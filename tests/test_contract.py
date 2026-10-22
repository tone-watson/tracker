"""Exercise real Flask wire responses and bridge isolated MCP contract tests over stdio."""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from tracker.app import create_app
from tracker.auth import Principal
from tracker.errors import TrackerError


class FixtureAuth:
    """Keep the contract fixture isolated from Farm credentials and network access."""

    def authenticate(self, authorization, scope):
        allowed = {
            "Bearer fixture-read": frozenset({"todos.read"}),
            "Bearer fixture-write": frozenset({"todos.read", "todos.write"}),
        }.get(authorization)
        if allowed is None:
            raise TrackerError("invalid_token", "Fixture token required", 401)
        if scope not in allowed:
            raise TrackerError("insufficient_scope", "Fixture write permission required", 403)
        return Principal("fixture-owner", "farm-mcp", "fixture-session", allowed)


def fixture_app(path):
    """Return an application and mutable clock using only the supplied test database."""
    clock = [datetime(2026, 10, 22, 17, tzinfo=timezone.utc)]
    app = create_app(path, auth=FixtureAuth(), now=lambda: clock[0])
    app.testing = True
    return app, clock


def test_wire_contract_fields_and_errors(tmp_path):
    app, _clock = fixture_app(tmp_path / "contract.db")
    client = app.test_client()
    headers = {"Authorization": "Bearer fixture-write"}
    capabilities = client.get("/api/capabilities", headers=headers)
    assert capabilities.json["data"]["contract"] == "farm.tracker/1"
    assert capabilities.json["data"]["service_id"] == "tracker"
    created = client.post(
        "/api/v1/lists",
        headers=headers,
        json={"name": "Contract", "idempotency_key": "contract-list"},
    )
    assert created.status_code == 200
    listing = created.json["data"]
    assert isinstance(listing["revision"], int)
    item = client.post(
        "/api/v1/items",
        headers=headers,
        json={
            "list_id": listing["id"],
            "title": "Call finance",
            "due": {"value": "2026-11"},
            "details": [{"label": "phone", "kind": "text", "value": "212-555-0100"}],
            "idempotency_key": "contract-item",
        },
    ).json["data"]
    assert item["due"]["precision"] == "month"
    assert item["created_at"] and item["completed_at"] is None
    detail_page = client.get(f"/api/v1/items/{item['id']}/details", headers=headers).json["data"]
    assert detail_page["items"][0]["value"] == "212-555-0100"
    assert detail_page["next_cursor"] is None
    updated = client.put(
        f"/api/v1/items/{item['id']}",
        headers=headers,
        json={"expected_revision": item["revision"], "status": "completed"},
    )
    assert updated.json["data"]["completed_at"]
    stale = client.put(
        f"/api/v1/items/{item['id']}",
        headers=headers,
        json={"expected_revision": item["revision"], "status": "open"},
    )
    assert stale.status_code == 409
    assert stale.json["error"]["code"] == "revision_conflict"
    assert stale.json["request_id"]
    denied = client.post(
        "/api/v1/lists",
        headers={"Authorization": "Bearer fixture-read"},
        json={"name": "Denied", "idempotency_key": "denied"},
    )
    assert denied.status_code == 403


def serve_bridge():
    """Run JSON-line requests through Flask's test client without opening a socket."""
    with tempfile.TemporaryDirectory(prefix="tracker-mcp-contract-") as directory:
        app, clock = fixture_app(Path(directory) / "tracker.db")
        client = app.test_client()
        store = app.extensions["tracker_store"]
        for line in sys.stdin:
            message = json.loads(line)
            operation = message.get("operation", "request")
            if operation == "clock":
                clock[0] = datetime.fromisoformat(message["value"].replace("Z", "+00:00"))
                body, status = {"now": clock[0].isoformat()}, 200
            elif operation == "materialize":
                body, status = {"count": store.materialize(message.get("limit", 100))}, 200
            elif operation == "stats":
                with store.connect() as database:
                    body = {
                        "version": database.execute("SELECT version FROM metadata").fetchone()[0],
                        "events": database.execute("SELECT count(*) FROM events").fetchone()[0],
                    }
                status = 200
            else:
                response = client.open(
                    message["path"],
                    method=message.get("method", "GET"),
                    json=message.get("body"),
                    headers=message.get("headers", {"Authorization": "Bearer fixture-write"}),
                )
                body, status = response.get_json(), response.status_code
            print(
                json.dumps(
                    {"id": message["id"], "status": status, "body": body}, ensure_ascii=False
                ),
                flush=True,
            )


if __name__ == "__main__":
    if sys.argv[1:] != ["--bridge"]:
        raise SystemExit("Use --bridge to run the isolated MCP wire contract fixture")
    serve_bridge()
