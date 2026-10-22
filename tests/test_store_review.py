from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from tracker.store import Store


def recurring_store(tmp_path):
    def clock():
        return datetime(2026, 10, 22, 12, tzinfo=timezone.utc)

    store = Store(tmp_path / "tracker.db", now=clock)
    actor = {"subject": "review", "client_id": "review"}
    todo_list = store.create_list({"name": "Review", "idempotency_key": "list"}, actor)
    original = store.create_item(
        {
            "list_id": todo_list["id"],
            "title": "Every daily occurrence",
            "due": {"value": "2026-10-19"},
            "recurrence": {"mode": "calendar", "rule": "FREQ=DAILY"},
            "idempotency_key": "series",
        },
        actor,
    )
    return store, original, actor, clock


def test_overdue_pagination_freezes_time_when_sorted_by_creation(tmp_path):
    clock = [datetime(2026, 10, 22, 12, tzinfo=timezone.utc)]
    store = Store(tmp_path / "tracker.db", now=lambda: clock[0])
    actor = {"subject": "review", "client_id": "review"}
    todo_list = store.create_list({"name": "Review", "idempotency_key": "list"}, actor)

    def create(title, due):
        result = store.create_item(
            {
                "list_id": todo_list["id"],
                "title": title,
                "due": {"value": due},
                "idempotency_key": str(uuid4()),
            },
            actor,
        )
        clock[0] += timedelta(seconds=1)
        return result

    upcoming = create("Upcoming, oldest creation", "2026-10-22T12:01:00Z")
    first = create("Already overdue", "2026-10-22T11:00:00Z")
    second = create("Also overdue", "2026-10-22T11:30:00Z")
    query = {"overdue_only": "true", "order_by": "created_at", "limit": "1"}
    page = store.page("item", query)
    assert [item["id"] for item in page["items"]] == [first["id"]]

    clock[0] += timedelta(minutes=2)
    continuation = store.page("item", {**query, "cursor": page["next_cursor"]})
    assert [item["id"] for item in continuation["items"]] == [second["id"]]
    assert continuation["next_cursor"] is None
    assert store.page("item", query)["items"][0]["id"] == upcoming["id"]


def test_concurrent_completion_and_catchup_preserve_one_durable_occurrence(tmp_path):
    store, original, actor, clock = recurring_store(tmp_path)
    other = Store(store.path, now=clock)
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(store.materialize),
            executor.submit(other.materialize),
            executor.submit(
                other.update_item,
                original["id"],
                {"expected_revision": original["revision"], "status": "completed"},
                actor,
            ),
        ]
        for future in futures:
            future.result()
    restarted = Store(store.path, now=clock)
    assert restarted.materialize() == 0
    items = restarted.page("item", {"status": "all"})["items"]
    assert [item["due"]["value"] for item in items] == [f"2026-10-{day}" for day in range(19, 24)]
    assert [item["occurrence_index"] for item in items] == list(range(1, 6))


def test_failed_batch_rolls_back_series_cursor_before_recurrence_cache_retry(tmp_path, monkeypatch):
    store, original, _, clock = recurring_store(tmp_path)
    real_new_item = store._new_item
    calls = 0

    def fail_third(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("Injected failure after recurrence cursor advanced")
        return real_new_item(*args, **kwargs)

    monkeypatch.setattr(store, "_new_item", fail_third)
    with pytest.raises(RuntimeError, match="Injected failure"):
        store.materialize()
    assert store.page("item", {})["items"] == [original]
    restarted = Store(store.path, now=clock)
    assert restarted.materialize() == 4
    assert [item["due"]["value"] for item in restarted.page("item", {})["items"]] == [
        f"2026-10-{day}" for day in range(19, 24)
    ]
