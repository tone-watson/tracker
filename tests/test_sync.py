from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from tracker.app import create_app
from tracker.auth import Principal
from tracker.errors import TrackerError
from tracker.markdown_tasks import parse_tasks
from tracker.repo_files import RepositoryFile
from tracker.store import Store
from tracker import sync

ACTOR = {"subject": "test-user", "client_id": "test-browser"}


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "farm"
    root.mkdir()
    store = Store(tmp_path / "database/tracker.db", lambda: datetime(2026, 10, 24, tzinfo=timezone.utc))

    def repository(name="one", data=b"# Work\n\n- [ ] Original task\n", initialized=True):
        repo = root / "code" / name
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        (repo / "farm.json").write_text(json.dumps({"name": name, "workflow": "release"}))
        if initialized:
            (repo / ".farm").mkdir(mode=0o700)
        if data is not None:
            (repo / "TODO.md").write_bytes(data)
        return repo

    return store, root, repository


def items(store, **query):
    return store.page("item", {"status": "all", "limit": "50", **query})["items"]


def farm_list(store):
    return next(value for value in store.page("list", {})["items"] if value.get("type") == "farm")


def edit(store, item, **changes):
    return store.update_item(item["id"], {"expected_revision": item["revision"], **changes}, ACTOR)


def history(store, item):
    return store.page("history", {"limit": "50"}, item["id"])["items"]


def state(repo):
    return json.loads((repo / ".farm/tracker/state.json").read_bytes())


def test_import_preserves_checked_tasks_long_titles_heading_paths_and_source(workspace):
    store, root, repository = workspace
    title = "Long task " + "details " * 160
    raw = ("# Top\n\nNotes remain exactly as authored.\n\n## Audio\n\n"
           "- [X] Already finished\n  Its supporting note.\n"
           f"- [ ] {title.rstrip()}\n\n```md\n- [ ] Example only\n```\n").encode()
    repo = repository(data=raw)
    assert sync.run(store, root) == {"repositories": 1, "synced": 1, "errors": 0}
    records = {item["title"]: item for item in items(store)}
    assert set(records) == {"Already finished", title.rstrip()}
    done = records["Already finished"]
    assert done["status"] == "completed" and done["completed_at"]
    assert done["source"] == {"path": "code/one/TODO.md", "line": 7,
                              "headings": ["Top", "Audio"], "present": True}
    assert all(item["list_id"] == farm_list(store)["id"] for item in records.values())
    assert (repo / "TODO.md").read_bytes() == raw
    receipt = state(repo)
    assert receipt["snapshot"] == hashlib.sha256(raw).hexdigest()
    assert (repo / ".farm/tracker/objects" / receipt["snapshot"]).read_bytes() == raw
    assert not (repo / ".farm/tracker/pending.json").exists()
    assert store.repositories()["items"][0]["task_count"] == 2


def test_repeated_noop_sync_preserves_item_revisions_history_receipts_and_file(workspace):
    store, root, repository = workspace
    repo = repository()
    sync.run(store, root)
    before = items(store)
    events = {item["id"]: history(store, item) for item in before}
    receipt = (repo / ".farm/tracker/state.json").read_bytes()
    source = (repo / "TODO.md").stat()
    with store.connect() as db:
        commits = db.execute("SELECT count(*) FROM sync_commits").fetchone()[0]
        version = db.execute("SELECT version FROM metadata").fetchone()[0]
    for _ in range(3):
        assert sync.run(store, root)["errors"] == 0
    assert items(store) == before
    assert {item["id"]: history(store, item) for item in before} == events
    assert (repo / ".farm/tracker/state.json").read_bytes() == receipt
    assert (repo / "TODO.md").stat().st_ino == source.st_ino
    assert (repo / "TODO.md").stat().st_mtime_ns == source.st_mtime_ns
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM sync_commits").fetchone()[0] == commits
        assert db.execute("SELECT version FROM metadata").fetchone()[0] == version


@pytest.mark.parametrize("pending_only", [False, True])
@pytest.mark.parametrize("registration", ["farm.json", ".git"])
def test_removed_registration_pauses_existing_repository_writes(workspace, pending_only, registration):
    store, root, repository = workspace
    repo = repository()
    sync.run(store, root)
    original = (repo / "TODO.md").read_bytes()
    baseline = (repo / ".farm/tracker/state.json").read_bytes()
    changed = edit(store, items(store)[0], title="Queued edit")
    (repo / registration).rename(repo / (registration + ".removed"))
    assert sync.run(store, root, pending_only=pending_only)["errors"] == 1
    assert (repo / "TODO.md").read_bytes() == original
    assert (repo / ".farm/tracker/state.json").read_bytes() == baseline
    assert store.get("item", changed["id"])["title"] == "Queued edit"
    status = store.repositories()["items"][0]
    assert status["state"] == "attention" and "no longer" in status["error"]


def test_source_edits_update_same_tracker_record_and_completion_history(workspace):
    store, root, repository = workspace
    repo = repository()
    sync.run(store, root)
    before = items(store)[0]
    raw = b"# Work\n\n- [x] Renamed from source\n"
    (repo / "TODO.md").write_bytes(raw)
    assert sync.run(store, root)["errors"] == 0
    after = items(store)[0]
    assert after["id"] == before["id"]
    assert after["title"] == "Renamed from source" and after["status"] == "completed"
    assert after["revision"] == before["revision"] + 1
    event = history(store, after)[-1]
    assert event["action"] == "source_updated"
    assert event["changes"]["status"] == {"before": "open", "after": "completed"}
    assert (repo / "TODO.md").read_bytes() == raw


def test_tracker_edits_publish_to_source_and_retain_original_snapshot(workspace):
    store, root, repository = workspace
    original = b"# Work\r\n\r\n* [ ] Original task  \r\n  Keep this note.\r\n"
    repo = repository(data=original)
    sync.run(store, root)
    updated = edit(store, items(store)[0], title="Changed in Tracker", status="completed")
    assert store.repositories()["items"][0]["pending"] is True
    assert (Path(store.path).parent / "sync.request").exists()
    assert sync.run(store, root, pending_only=True)["synced"] == 1
    expected = original.replace(b"[ ] Original task", b"[x] Changed in Tracker")
    assert (repo / "TODO.md").read_bytes() == expected
    assert (repo / ".farm/tracker/objects" / hashlib.sha256(original).hexdigest()).read_bytes() == original
    assert store.get("item", updated["id"])["revision"] == updated["revision"]
    assert store.repositories()["items"][0]["pending"] is False


def test_independent_source_completion_and_tracker_title_merge(workspace):
    store, root, repository = workspace
    repo = repository()
    sync.run(store, root)
    item = edit(store, items(store)[0], title="Tracker title")
    (repo / "TODO.md").write_bytes(b"# Work\n\n- [x] Original task\n")
    assert sync.run(store, root)["errors"] == 0
    after = store.get("item", item["id"])
    assert (after["title"], after["status"]) == ("Tracker title", "completed")
    assert (repo / "TODO.md").read_bytes() == b"# Work\n\n- [x] Tracker title\n"


@pytest.mark.parametrize("changes,source,expected", [
    ({"title": "Tracker title"}, b"- [ ] Source title\n", b"- [ ] Tracker title\n"),
    ({"status": "in_progress"}, b"- [x] Original task\n", b"- [ ] Original task\n"),
])
def test_tracker_wins_same_field_conflicts(workspace, changes, source, expected):
    store, root, repository = workspace
    repo = repository(data=b"- [ ] Original task\n")
    sync.run(store, root)
    item = edit(store, items(store)[0], **changes)
    (repo / "TODO.md").write_bytes(source)
    assert sync.run(store, root)["errors"] == 0
    assert (repo / "TODO.md").read_bytes() == expected
    after = store.get("item", item["id"])
    assert all(after[key] == value for key, value in changes.items())


@pytest.mark.parametrize("checked,expected_status", [(False, "cancelled"), (True, "completed")])
def test_source_deletion_archives_binding_preserving_completed_history(workspace, checked, expected_status):
    store, root, repository = workspace
    repo = repository(data=b"# Work\n\n- [" + (b"x" if checked else b" ") + b"] Original task\n")
    sync.run(store, root)
    before = items(store)[0]
    (repo / "TODO.md").write_bytes(b"# Work\n\nStill useful notes.\n")
    assert sync.run(store, root)["errors"] == 0
    after = store.get("item", before["id"])
    assert after["status"] == expected_status
    assert after["source"]["present"] is False
    assert history(store, after)[-1]["action"] == "source_removed"
    assert state(repo)["bindings"] == []
    assert sync.run(store, root)["errors"] == 0
    assert store.get("item", before["id"]) == after


def test_tracker_cancellation_removes_only_checkbox_and_keeps_notes(workspace):
    store, root, repository = workspace
    original = b"# Work\n\n- [ ] Original task\n  Notes stay here.\n\nUnrelated prose.\n"
    repo = repository(data=original)
    sync.run(store, root)
    cancelled = edit(store, items(store)[0], status="cancelled")
    assert sync.run(store, root, pending_only=True)["errors"] == 0
    assert (repo / "TODO.md").read_bytes() == original.replace(b"[ ] ", b"")
    assert store.get("item", cancelled["id"])["source"]["present"] is False
    assert state(repo)["bindings"] == []
    assert sync.run(store, root)["errors"] == 0
    assert len(items(store)) == 1


def test_new_tracker_repository_item_appends_and_binds_without_duplicate_import(workspace):
    store, root, repository = workspace
    original = b"# Work\n\nExisting prose without a final newline."
    repo = repository(data=original)
    sync.run(store, root)
    bound = store.repositories()["items"][0]
    body = {"list_id": farm_list(store)["id"], "repository_id": bound["id"],
            "title": "New task from Tracker", "idempotency_key": "new-task"}
    created = store.create_item(body, ACTOR)
    assert store.create_item(body, ACTOR) == created
    assert sync.run(store, root, pending_only=True)["synced"] == 1
    assert (repo / "TODO.md").read_bytes() == original + b"\n\n- [ ] New task from Tracker\n"
    after = store.get("item", created["id"])
    assert after["source"]["present"] is True
    assert state(repo)["bindings"][0]["item_id"] == created["id"]
    assert sync.run(store, root)["errors"] == 0
    assert len(items(store)) == 1


def test_repository_filter_and_farm_creation_binding_validation(workspace):
    store, root, repository = workspace
    repository("one")
    repository("two", b"- [ ] Other repository task\n")
    sync.run(store, root)
    parent = farm_list(store)
    one, two = store.repositories()["items"]
    assert [item["title"] for item in items(store, repository_id=one["id"])] == ["Original task"]
    assert [item["title"] for item in items(store, repository_id=two["id"])] == ["Other repository task"]
    personal = store.create_list({"name": "Personal", "idempotency_key": "personal"}, ACTOR)
    for values, code in [
        ({"list_id": parent["id"]}, "repository_required"),
        ({"list_id": personal["id"], "repository_id": one["id"]}, "repository_required"),
        ({"list_id": parent["id"], "repository_id": str(uuid4())}, "not_found"),
        ({"list_id": parent["id"], "repository_id": "../../unsafe"}, "invalid_id"),
    ]:
        with pytest.raises(TrackerError) as error:
            store.create_item({"title": "Task", "idempotency_key": str(uuid4()), **values}, ACTOR)
        assert error.value.code == code
    with pytest.raises(TrackerError, match="must stay"):
        edit(store, items(store, repository_id=one["id"])[0], list_id=personal["id"])
    with pytest.raises(TrackerError):
        items(store, repository_id="not-an-id")


@pytest.mark.parametrize("checked", [False, True])
def test_explicit_tracker_reopen_rebinds_source_archived_item(workspace, checked):
    store, root, repository = workspace
    repo = repository(data=b"- [" + (b"x" if checked else b" ") + b"] Original task\n")
    sync.run(store, root)
    original = items(store)[0]
    (repo / "TODO.md").write_bytes(b"Retained source notes.\n")
    assert sync.run(store, root)["errors"] == 0
    archived = store.get("item", original["id"])
    assert archived["source"]["present"] is False
    edit(store, archived, status="open")
    assert sync.run(store, root, pending_only=True)["errors"] == 0
    reopened = store.get("item", original["id"])
    assert reopened["status"] == "open" and reopened["source"]["present"] is True
    assert (repo / "TODO.md").read_bytes() == b"Retained source notes.\n\n- [ ] Original task\n"
    assert state(repo)["bindings"][0]["item_id"] == original["id"]
    assert len(items(store)) == 1


def test_tracker_reopen_after_cancellation_retains_archived_notes_and_adds_checkbox(workspace):
    store, root, repository = workspace
    repo = repository(data=b"- [ ] Original task\n  Keep cancellation notes.\n")
    sync.run(store, root)
    cancelled = edit(store, items(store)[0], status="cancelled")
    assert sync.run(store, root, pending_only=True)["errors"] == 0
    archived_source = (repo / "TODO.md").read_bytes()
    assert archived_source == b"- Original task\n  Keep cancellation notes.\n"
    archived = store.get("item", cancelled["id"])
    edit(store, archived, title="Reopened task", status="open")
    assert sync.run(store, root, pending_only=True)["errors"] == 0
    assert (repo / "TODO.md").read_bytes() == archived_source + b"\n- [ ] Reopened task\n"
    assert len(items(store)) == 1


def test_file_race_rolls_back_tracker_changes_and_preserves_concurrent_source(workspace, monkeypatch):
    store, root, repository = workspace
    repo = repository()
    sync.run(store, root)
    before = items(store)[0]
    events = history(store, before)
    (repo / "TODO.md").write_bytes(b"# Work\n\n- [x] Changed before read\n")
    original = RepositoryFile.save

    def race(file, *args):
        (repo / "TODO.md").write_bytes(b"# Work\n\n- [ ] Concurrent edit\n")
        return original(file, *args)

    monkeypatch.setattr(RepositoryFile, "save", race)
    assert sync.run(store, root) == {"repositories": 1, "synced": 0, "errors": 1}
    assert store.get("item", before["id"]) == before
    assert history(store, before) == events
    assert (repo / "TODO.md").read_bytes().endswith(b"Concurrent edit\n")
    assert not (repo / ".farm/tracker/pending.json").exists()
    assert store.repositories()["items"][0]["state"] == "attention"


def test_ambiguous_renames_do_not_create_cancel_or_overwrite_tasks(workspace):
    store, root, repository = workspace
    repo = repository(data=b"- [ ] First task\n- [ ] Second task\n")
    sync.run(store, root)
    before = items(store)
    source = b"- [ ] Both renamed one\n- [ ] Both renamed two\n"
    (repo / "TODO.md").write_bytes(source)
    assert sync.run(store, root)["errors"] == 1
    assert items(store) == before
    assert (repo / "TODO.md").read_bytes() == source
    repository_state = store.repositories()["items"][0]
    assert repository_state["state"] == "attention" and "ambiguous" in repository_state["error"]
    assert "Both renamed" not in repository_state["error"]


def test_missing_previously_synced_file_preserves_tasks_and_reports_attention(workspace):
    store, root, repository = workspace
    repo = repository()
    sync.run(store, root)
    before = items(store)
    (repo / "TODO.md").unlink()
    assert sync.run(store, root)["errors"] == 1
    assert items(store) == before
    assert not (repo / "TODO.md").exists()
    assert store.repositories()["items"][0]["state"] == "attention"


def test_uninitialized_repository_is_not_written_or_imported(workspace):
    store, root, repository = workspace
    repo = repository(initialized=False)
    original = (repo / "TODO.md").read_bytes()
    assert sync.run(store, root)["errors"] == 1
    assert items(store) == []
    assert not (repo / ".farm").exists()
    assert (repo / "TODO.md").read_bytes() == original
    assert "farm init" in store.repositories()["items"][0]["error"]


def test_pending_only_skips_unqueued_source_edits_but_explicit_request_scans_all(workspace):
    store, root, repository = workspace
    one = repository("one")
    two = repository("two", b"- [ ] Other task\n")
    sync.run(store, root)
    first = next(item for item in items(store) if item["title"] == "Original task")
    edit(store, first, title="Queued Tracker change")
    (two / "TODO.md").write_bytes(b"- [x] Other task\n")
    assert sync.run(store, root, pending_only=True) == {"repositories": 1, "synced": 1, "errors": 0}
    assert b"Queued Tracker change" in (one / "TODO.md").read_bytes()
    assert next(item for item in items(store) if item["title"] == "Other task")["status"] == "open"
    assert sync.run(store, root, pending_only=True)["repositories"] == 0
    assert store.request_sync() == {"queued": True}
    assert sync.run(store, root, pending_only=True)["repositories"] == 2
    assert next(item for item in items(store) if item["title"] == "Other task")["status"] == "completed"


def test_uncommitted_filesystem_publication_recovers_before_reconciliation(workspace, monkeypatch):
    store, root, repository = workspace
    repo = repository()
    sync.run(store, root)
    updated = edit(store, items(store)[0], title="Tracker changed title")
    source = b"# Work\n\n- [x] Original task\n"
    (repo / "TODO.md").write_bytes(source)
    original_save = RepositoryFile.save

    def interrupted_save(file, *args):
        original_save(file, *args)
        raise RuntimeError("Simulated failure before SQLite receipt commit")

    monkeypatch.setattr(RepositoryFile, "save", interrupted_save)
    assert sync.run(store, root)["errors"] == 1
    assert store.get("item", updated["id"])["status"] == "open"
    assert b"[x] Tracker changed title" in (repo / "TODO.md").read_bytes()
    pending = json.loads((repo / ".farm/tracker/pending.json").read_bytes())
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM sync_commits WHERE token=?", (pending["token"],)).fetchone()
    recovered = []
    original_recover = RepositoryFile.recover

    def observe_recovery(file, committed):
        result = original_recover(file, committed)
        recovered.append((result, (repo / "TODO.md").read_bytes()))
        return result

    monkeypatch.setattr(RepositoryFile, "save", original_save)
    monkeypatch.setattr(RepositoryFile, "recover", observe_recovery)
    assert sync.run(store, root)["errors"] == 0
    assert recovered == [("rolled_back", source)]
    assert store.get("item", updated["id"])["status"] == "completed"
    assert not (repo / ".farm/tracker/pending.json").exists()


def test_committed_receipt_recovers_without_overwriting_later_source_edits(workspace, monkeypatch):
    store, root, repository = workspace
    repo = repository()
    sync.run(store, root)
    updated = edit(store, items(store)[0], title="Tracker changed title")
    original_finish = RepositoryFile.finish

    def interrupted_finish(file, token):
        raise RuntimeError("Simulated process stop after SQLite commit")

    monkeypatch.setattr(RepositoryFile, "finish", interrupted_finish)
    assert sync.run(store, root)["errors"] == 1
    pending = json.loads((repo / ".farm/tracker/pending.json").read_bytes())
    with store.connect() as db:
        assert db.execute("SELECT 1 FROM sync_commits WHERE token=?", (pending["token"],)).fetchone()
    later_source = b"# Work\n\n- [x] Tracker changed title\n"
    (repo / "TODO.md").write_bytes(later_source)
    monkeypatch.setattr(RepositoryFile, "finish", original_finish)
    assert sync.run(store, root)["errors"] == 0
    assert (repo / "TODO.md").read_bytes() == later_source
    assert store.get("item", updated["id"])["status"] == "completed"
    assert len(parse_tasks((repo / "TODO.md").read_bytes()).tasks) == 1
    assert not (repo / ".farm/tracker/pending.json").exists()


def test_repository_api_requires_read_and_sync_queue_requires_write(workspace):
    store, root, repository = workspace
    repository()
    sync.run(store, root)

    class Auth:
        def authenticate(self, authorization, scope):
            if authorization not in {"Bearer read", "Bearer write"}:
                raise TrackerError("invalid_token", "Missing token", 401)
            if scope == "todos.write" and authorization != "Bearer write":
                raise TrackerError("insufficient_scope", "Write required", 403)
            return Principal("test-user", "test-browser", "session", frozenset({scope}))

    client = create_app(store.path, Auth(), store.now).test_client()
    assert client.get("/api/v1/repositories").status_code == 401
    read = {"Authorization": "Bearer read"}
    write = {"Authorization": "Bearer write"}
    response = client.get("/api/v1/repositories", headers=read)
    assert response.status_code == 200 and response.json["data"]["items"][0]["path"] == "code/one"
    assert client.post("/api/v1/sync", headers=read, json={}).status_code == 403
    assert client.post("/api/v1/sync", headers=write, json={"root": "/arbitrary"}).status_code == 400
    queued = client.post("/api/v1/sync", headers=write, json={})
    assert queued.status_code == 202 and queued.json == {"data": {"queued": True}}
    with store.connect() as db:
        assert db.execute("SELECT requested FROM sync_control WHERE id=1").fetchone()[0] == 1


def test_sync_failure_preserves_a_newer_queued_tracker_write(workspace, monkeypatch):
    store, root, repository = workspace
    repo = repository()
    assert sync.run(store, root)["errors"] == 0
    queued = edit(store, items(store)[0], title="First queued title")
    before_generation = store.repositories()["items"][0]["generation"]

    def fail_after_concurrent_edit(_store, _root, _repository):
        edit(store, store.get("item", queued["id"]), title="Newer concurrent title")
        raise OSError("Simulated source publication failure")

    monkeypatch.setattr(sync, "sync_repository", fail_after_concurrent_edit)
    assert sync.run(store, root, pending_only=True) == {
        "repositories": 1, "synced": 0, "errors": 1,
    }
    record = store.repositories()["items"][0]
    assert record["generation"] > before_generation
    assert record["pending"] is True
    assert record["state"] == "pending" and record["error"] is None
    assert store.get("item", queued["id"])["title"] == "Newer concurrent title"
    assert (repo / "TODO.md").read_bytes() == b"# Work\n\n- [ ] Original task\n"


def test_finish_failure_marks_attention_for_generation_incorporated_after_scan(workspace, monkeypatch):
    store, root, repository = workspace
    repo = repository()
    assert sync.run(store, root)["errors"] == 0
    original_sync = sync.sync_repository
    original_finish = RepositoryFile.finish
    original_item = items(store)[0]

    def edit_after_scan(current_store, current_root, current_repository):
        edit(store, store.get("item", original_item["id"]), title="Edit before transaction")
        return original_sync(current_store, current_root, current_repository)

    def fail_finish(_workspace, _token):
        raise OSError("Simulated state finalization failure after commit")

    monkeypatch.setattr(sync, "sync_repository", edit_after_scan)
    monkeypatch.setattr(RepositoryFile, "finish", fail_finish)
    assert sync.run(store, root) == {"repositories": 1, "synced": 0, "errors": 1}
    record = store.repositories()["items"][0]
    assert record["state"] == "attention" and record["error"] is not None
    assert (repo / ".farm/tracker/pending.json").exists()
    assert (repo / "TODO.md").read_bytes().endswith(b"Edit before transaction\n")

    monkeypatch.setattr(sync, "sync_repository", original_sync)
    monkeypatch.setattr(RepositoryFile, "finish", original_finish)
    assert sync.run(store, root)["errors"] == 0
    assert store.repositories()["items"][0]["state"] == "synced"
    assert not (repo / ".farm/tracker/pending.json").exists()
