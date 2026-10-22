from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from tracker.errors import TrackerError
from tracker.store import Store

ACTOR = {"subject": "1", "client_id": "agent"}


@pytest.fixture
def setup(tmp_path):
    clock = [datetime(2026, 10, 22, 12, tzinfo=timezone.utc)]
    store = Store(tmp_path / "tracker.db", lambda: clock[0])
    todo_list = store.create_list({"name": "Personal", "idempotency_key": "list"}, ACTOR)
    return store, todo_list, clock


def item(setup, **values):
    store, todo_list, _ = setup
    return store.create_item(
        {
            "list_id": todo_list["id"],
            "title": "Call finance",
            "idempotency_key": str(uuid4()),
            **values,
        },
        ACTOR,
    )


def update(store, obj, **values):
    return store.update_item(obj["id"], {"expected_revision": obj["revision"], **values}, ACTOR)


def test_lists_are_independent_and_names_unique(setup):
    store, todo_list, _ = setup
    other = store.create_list({"name": "Work", "idempotency_key": "work"}, ACTOR)
    first = item(setup)
    second = item(setup, list_id=other["id"])
    assert store.page("item", {"list_id": todo_list["id"]})["items"] == [first]
    assert store.page("item", {"list_id": other["id"]})["items"] == [second]
    with pytest.raises(TrackerError, match="already has"):
        store.create_list({"name": "PERSONAL", "idempotency_key": "duplicate"}, ACTOR)


def test_idempotent_create_concurrent_and_mismatched_replay(setup):
    store, todo_list, _ = setup
    body = {"list_id": todo_list["id"], "title": "One task", "idempotency_key": "retry"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.create_item(body, ACTOR), range(8)))
    assert len({r["id"] for r in results}) == 1
    assert len(store.page("item", {})["items"]) == 1
    with pytest.raises(TrackerError) as error:
        store.create_item({**body, "title": "Different"}, ACTOR)
    assert error.value.code == "idempotency_conflict"


def test_completion_reopen_preserves_history_and_guards_stale_writes(setup):
    store, _, clock = setup
    obj = item(setup)
    clock[0] = datetime(2026, 10, 23, tzinfo=timezone.utc)
    done = update(store, obj, status="completed")
    assert done["completed_at"] == "2026-10-23T00:00:00.000000Z"
    with pytest.raises(TrackerError) as error:
        update(store, obj, title="Stale")
    assert error.value.code == "revision_conflict"
    reopened = update(store, done, status="open")
    assert reopened["completed_at"] is None
    events = store.page("history", {}, obj["id"])["items"]
    assert events[1]["changes"]["completed_at"]["after"] == done["completed_at"]
    assert events[2]["changes"]["completed_at"]["before"] == done["completed_at"]


def test_due_order_and_overdue_only_exclude_done_cancelled_and_undated(setup):
    store, _, _ = setup
    undated = item(setup)
    future = item(setup, due={"value": "2027"})
    earlier = item(setup, due={"value": "2026-10-19"})
    later = item(setup, due={"value": "2026-10-21"})
    today = item(setup, due={"value": "2026-10-22"})
    done = item(setup, due={"value": "2020"})
    update(store, done, status="completed")
    assert [i["id"] for i in store.page("item", {})["items"]] == [
        earlier["id"],
        later["id"],
        today["id"],
        future["id"],
        undated["id"],
    ]
    assert [
        i["id"] for i in store.page("item", {"overdue_only": "true", "status": "all"})["items"]
    ] == [earlier["id"], later["id"]]


def test_blocked_dependencies_cycles_and_cancelled_blocker(setup):
    store, _, _ = setup
    first = item(setup)
    second = item(setup, status="blocked", blocked_by=[first["id"]])
    with pytest.raises(TrackerError) as error:
        update(store, first, status="blocked", blocked_by=[second["id"]])
    assert error.value.code == "dependency_cycle"
    with pytest.raises(TrackerError) as error:
        update(store, second, status="completed")
    assert error.value.code == "unresolved_dependencies"
    first = update(store, first, status="cancelled")
    with pytest.raises(TrackerError):
        update(store, second, status="in_progress")
    first = update(store, first, status="completed")
    assert store.get("item", second["id"])["status"] == "blocked"
    second = update(store, second, status="in_progress")
    assert second["blocked_by"] == [first["id"]]


def test_details_multiple_values_separate_revisions_and_history(setup):
    store, _, _ = setup
    obj = item(setup)
    phone = store.add_detail(
        obj["id"],
        {"label": "Phone", "kind": "text", "value": "212-555-0100", "idempotency_key": "phone"},
        ACTOR,
    )
    store.add_detail(
        obj["id"],
        {
            "label": "Reference",
            "kind": "json",
            "value": {"case": 123, "notes": ["A", "B"]},
            "idempotency_key": "reference",
        },
        ACTOR,
    )
    phone = store.update_detail(
        obj["id"],
        phone["id"],
        {"expected_revision": phone["revision"], "value": "212-555-0101"},
        ACTOR,
    )
    assert len(store.page("detail", {}, obj["id"])["items"]) == 2
    assert store.get("item", obj["id"])["revision"] == obj["revision"]
    assert (
        store.page("history", {}, obj["id"])["items"][-1]["changes"]["value"]["before"]
        == "212-555-0100"
    )
    with pytest.raises(TrackerError):
        store.update_detail(
            str(uuid4()),
            phone["id"],
            {"expected_revision": phone["revision"], "cancelled": True},
            ACTOR,
        )


def test_pagination_detects_mutations_and_has_no_total_detail_limit(setup):
    store, _, _ = setup
    obj = item(setup)
    for i in range(75):
        store.add_detail(
            obj["id"],
            {"label": str(i), "kind": "json", "value": i, "idempotency_key": str(i)},
            ACTOR,
        )
    first = store.page("detail", {"limit": "50"}, obj["id"])
    second = store.page("detail", {"limit": "50", "cursor": first["next_cursor"]}, obj["id"])
    assert len(first["items"]) == 50 and len(second["items"]) == 25
    assert second["next_cursor"] is None
    item(setup)
    with pytest.raises(TrackerError) as error:
        store.page("detail", {"limit": "50", "cursor": first["next_cursor"]}, obj["id"])
    assert error.value.code == "cursor_conflict"


def test_due_ties_paginate_without_loss(setup):
    store, _, _ = setup
    ids = {item(setup, due={"value": "2026-10-01"})["id"] for _ in range(9)}
    seen, cursor = [], None
    while True:
        page = store.page("item", {"limit": "2", **({"cursor": cursor} if cursor else {})})
        seen.extend(i["id"] for i in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert set(seen) == ids and len(seen) == len(ids)


def test_every_missed_calendar_occurrence_survives_restart(setup):
    store, _, clock = setup
    original = item(
        setup, due={"value": "2026-10-18"}, recurrence={"mode": "calendar", "rule": "FREQ=DAILY"}
    )
    assert store.materialize(2) == 2
    restarted = Store(store.path, lambda: clock[0])
    assert restarted.materialize(100) == 3
    assert restarted.materialize(100) == 0
    items = restarted.page("item", {"limit": "50"})["items"]
    assert [i["due"]["value"] for i in items] == [f"2026-10-{i}" for i in range(18, 24)]
    assert {i["occurrence_index"] for i in items} == set(range(1, 7))
    assert all(i["recurrence_id"] == original["recurrence_id"] for i in items)


def test_calendar_count_exhaustion_and_early_skip(setup):
    store, _, _ = setup
    original = item(
        setup,
        due={"value": "2026-11-01"},
        recurrence={"mode": "calendar", "rule": "FREQ=DAILY;COUNT=2"},
    )
    skipped = store.skip(
        original["id"],
        {"expected_revision": original["revision"], "idempotency_key": "skip"},
        ACTOR,
    )
    assert skipped["skipped_at"] and skipped["status"] == "cancelled"
    active = store.page("item", {})["items"]
    assert len(active) == 1 and active[0]["due"]["value"] == "2026-11-02"
    update(store, active[0], status="completed")
    assert store.get("recurrence", original["recurrence_id"])["next_due"] is None


def test_completion_recurrence_reopening_does_not_duplicate_successor(setup):
    store, _, clock = setup
    original = item(
        setup,
        due={"value": "2026-10-20T15:00"},
        recurrence={"mode": "after_completion", "every": 3, "unit": "days"},
    )
    assert store.materialize() == 0
    done = update(store, original, status="completed")
    active = store.page("item", {})["items"]
    assert len(active) == 1
    assert active[0]["due"]["starts_at"] == "2026-10-25T12:00:00.000000Z"
    reopened = update(store, done, status="open")
    clock[0] = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    update(store, reopened, status="completed")
    assert len(store.page("item", {"status": "all"})["items"]) == 2


def test_cancel_series_cancels_all_active_siblings_and_preserves_done(setup):
    store, _, clock = setup
    original = item(
        setup, due={"value": "2026-10-19"}, recurrence={"mode": "calendar", "rule": "FREQ=DAILY"}
    )
    store.materialize()
    update(store, original, status="completed")
    current = store.page("item", {})["items"][0]
    update(store, current, status="cancelled")
    assert not store.page("item", {})["items"]
    assert store.get("item", original["id"])["status"] == "completed"
    clock[0] = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert store.materialize() == 0


def test_list_cancel_and_restore_does_not_resurrect_items(setup):
    store, todo_list, _ = setup
    original = item(
        setup, due={"value": "2026-10-20"}, recurrence={"mode": "calendar", "rule": "FREQ=DAILY"}
    )
    cancelled = store.update_list(
        todo_list["id"], {"expected_revision": todo_list["revision"], "status": "cancelled"}, ACTOR
    )
    assert store.get("item", original["id"])["status"] == "cancelled"
    assert store.get("recurrence", original["recurrence_id"])["status"] == "cancelled"
    store.update_list(
        todo_list["id"], {"expected_revision": cancelled["revision"], "status": "active"}, ACTOR
    )
    assert store.page("item", {})["items"] == []


def test_template_updates_apply_only_to_future_occurrences(setup):
    store, _, _ = setup
    original = item(
        setup,
        due={"value": "2026-11-01"},
        recurrence={"mode": "calendar", "rule": "FREQ=DAILY"},
        details=[{"label": "Phone", "kind": "text", "value": "old"}],
    )
    series = store.get("recurrence", original["recurrence_id"])
    store.update_recurrence(
        series["id"],
        {
            "expected_revision": series["revision"],
            "title": "New title",
            "details": [{"label": "Phone", "kind": "text", "value": "new"}],
        },
        ACTOR,
    )
    update(store, original, status="completed")
    successor = store.page("item", {})["items"][0]
    assert successor["title"] == "New title"
    assert store.page("detail", {}, original["id"])["items"][0]["value"] == "old"
    assert store.page("detail", {}, successor["id"])["items"][0]["value"] == "new"


@pytest.mark.parametrize(
    "values",
    [
        {"status": []},
        {"blocked_by": [[]]},
        {"due": []},
        {"title": None},
        {"recurrence": {}},
        {"details": [{"label": "X", "kind": [], "value": 1}]},
    ],
)
def test_invalid_input_is_rejected_without_partial_writes(setup, values):
    store, _, _ = setup
    with pytest.raises(TrackerError):
        item(setup, **values)
    assert store.page("item", {})["items"] == []
    with store.connect() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_resuming_series_and_old_occurrence_cannot_duplicate_successors(setup):
    store, _, _ = setup
    original = item(
        setup,
        due={"value": "2026-10-20"},
        recurrence={"mode": "after_completion", "every": 1, "unit": "days"},
    )
    cancelled = update(store, original, status="cancelled")
    series = store.get("recurrence", original["recurrence_id"])
    store.update_recurrence(
        series["id"], {"expected_revision": series["revision"], "status": "active"}, ACTOR
    )
    reopened = update(store, cancelled, status="open")
    update(store, reopened, status="completed")
    assert len(store.page("item", {})["items"]) == 1


def test_idempotency_is_independent_of_json_key_order(setup):
    store, todo_list, _ = setup
    body = {"list_id": todo_list["id"], "title": "Same", "idempotency_key": "ordered"}
    first = store.create_item(body, ACTOR)
    assert (
        store.create_item(dict(reversed(list(body.items()))), dict(reversed(list(ACTOR.items()))))
        == first
    )
