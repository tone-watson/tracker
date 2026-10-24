import hashlib
import json
import os
import stat
import subprocess

import pytest

from tracker import repo_files
from tracker.repo_files import (
    FileConflict,
    FileSafetyError,
    NeedsInitialization,
    RepositoryFile,
    discover_repositories,
)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".farm").mkdir()
    (root / "TODO.md").write_bytes(b"# Tasks\n\n- [ ] one\n")
    return root


def registered(root, name="example", initialize=True, todo=True):
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "farm.json").write_text(json.dumps({"name": name}))
    if initialize:
        (root / ".farm").mkdir()
    if todo:
        (root / "TODO.md").write_bytes(b"- [ ] task\n")
    return root


def test_discovery_registered_roots_only_and_prunes_before_traversal(tmp_path):
    registered(tmp_path / "sys" / "cli", "farm-cli")
    registered(tmp_path / "extensions" / "world", "world", initialize=False)
    registered(tmp_path / "private" / "empty", todo=False)
    for name in ("production", "archive", "backup-old", "tmp", ".uv", "node_modules"):
        registered(tmp_path / name / "hidden")
    (tmp_path / "symlink").symlink_to(tmp_path / "sys", target_is_directory=True)
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / ".git").write_text("gitdir: /not-followed\n")
    (linked / "farm.json").write_text('{}')
    nested = tmp_path / "sys" / "cli" / "nested"
    registered(nested)
    discovered = discover_repositories(tmp_path)
    assert [r.path for r in discovered] == ["extensions/world", "private/empty", "sys/cli"]
    assert discovered[0].needs_init
    assert not discovered[1].has_todo
    assert discovered[2].name == "farm-cli"
    assert discovered[2].root == tmp_path / "sys" / "cli"


def test_discovery_rejects_symlink_root_manifest_and_invalid_workflow(tmp_path):
    root = registered(tmp_path / "root")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(OSError):
        discover_repositories(alias)
    (root / "farm.json").write_text('{"workflow":"invalid"}')
    with pytest.raises(FileSafetyError, match="workflow"):
        discover_repositories(tmp_path)
    (root / "farm.json").unlink()
    (root / "farm.json").symlink_to(root / "TODO.md")
    with pytest.raises(OSError):
        discover_repositories(tmp_path)


def test_discovery_limit_is_an_error_not_a_partial_success(tmp_path, monkeypatch):
    registered(tmp_path / "one")
    registered(tmp_path / "two")
    monkeypatch.setattr(repo_files, "MAX_DIRECTORIES", 2)
    with pytest.raises(FileSafetyError, match="limit"):
        discover_repositories(tmp_path)


def test_media_installation_and_unregistered_project_boundaries_stay_bounded(tmp_path):
    (tmp_path / ".git").mkdir()
    registered(tmp_path / "sys" / "cli", "cli")
    registered(tmp_path / "sys" / "assets", "assets-service")
    registered(tmp_path / "extensions" / "audio", "audio-extension")
    for boundary in ("assets", "audio", "tools/blender", "media", "share"):
        deep = tmp_path / boundary
        for _ in range(repo_files.MAX_DEPTH + 3):
            deep /= "nested"
        deep.mkdir(parents=True)
    project = tmp_path / "code" / "unregistered"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text('[project]\nname="unregistered"\n')
    registered(project / "nested", "must-not-scan-project-internals")
    discovered = discover_repositories(tmp_path)
    assert [repo.path for repo in discovered] == ["extensions/audio", "sys/assets", "sys/cli"]


def test_unknown_unregistered_deep_tree_is_still_reported(tmp_path):
    deep = tmp_path
    for index in range(repo_files.MAX_DEPTH + 2):
        deep /= f"source-{index}"
    deep.mkdir(parents=True)
    with pytest.raises(FileSafetyError, match="depth limit at source-0/"):
        discover_repositories(tmp_path)


def test_registered_backup_service_is_discovered_without_entering_backup_data(tmp_path):
    registered(tmp_path / "sys" / "backups", "backup-service")
    registered(tmp_path / "backups" / "protected-copy", "never-visit")
    registered(tmp_path / "sys" / "archive" / "protected-copy", "never-visit")
    registered(tmp_path / "private" / "recovery" / "protected-copy", "never-visit")
    (tmp_path / "code" / "backups" / "deep-data").mkdir(parents=True)
    discovered = discover_repositories(tmp_path)
    assert [repo.path for repo in discovered] == ["sys/backups"]
    assert discovered[0].has_todo


def test_sync_requires_farm_init_and_never_changes_gitignore(tmp_path):
    with pytest.raises(NeedsInitialization):
        with RepositoryFile(tmp_path):
            pass
    assert not (tmp_path / ".farm").exists()
    assert not (tmp_path / ".gitignore").exists()


def test_save_journal_then_commit_preserves_permissions_and_snapshots(repo):
    todo = repo / "TODO.md"
    todo.chmod(0o640)
    original = todo.read_bytes()
    candidate = original.replace(b"[ ]", b"[x]")
    with RepositoryFile(repo) as file:
        assert file.read() == original
        assert file.state() is None
        file.save(candidate, {"bindings": []}, original, "commit-1")
        assert todo.read_bytes() == candidate
        assert file.state() is None
        pending = file.pending()
        assert file.snapshot(pending["before_hash"]) == original
        assert file.snapshot(pending["after_hash"]) == candidate
        assert pending["token"] == "commit-1"
        assert stat.S_IMODE(todo.stat().st_mode) == 0o640
        file.finish("commit-1")
        assert file.pending() is None
        assert file.state() == {
            "bindings": [], "snapshot": hashlib.sha256(candidate).hexdigest()
        }
        assert file.recover(lambda _: False) == "none"
    assert not (repo / ".gitignore").exists()
    assert stat.S_IMODE((repo / ".farm/tracker").stat().st_mode) == 0o700
    assert stat.S_IMODE((repo / ".farm/tracker/state.json").stat().st_mode) == 0o600


def test_uncommitted_crash_rolls_back_file_and_prior_state(repo):
    with RepositoryFile(repo) as file:
        original = file.read()
        file.save(original, {"old": 1}, original, "first")
        file.finish("first")
        previous_state = file.state()
        candidate = original.replace(b"one", b"two")
        file.save(candidate, {"new": 2}, original, "crashed")
    with RepositoryFile(repo) as file:
        assert file.recover(lambda token: token == "first") == "rolled_back"
        assert file.read() == original
        assert file.state() == previous_state
        assert file.pending() is None


def test_committed_crash_finalizes_state_without_overwriting_later_edit(repo):
    with RepositoryFile(repo) as file:
        original = file.read()
        candidate = original.replace(b"one", b"two")
        file.save(candidate, {"new": True}, original, "crashed")
    external = candidate + b"- [ ] human edit\n"
    (repo / "TODO.md").write_bytes(external)
    with RepositoryFile(repo) as file:
        assert file.recover(lambda token: token == "crashed") == "committed"
        assert file.read() == external
        assert file.state()["snapshot"] == hashlib.sha256(candidate).hexdigest()
        assert file.pending() is None


def test_uncommitted_crash_with_external_edit_never_overwrites_it(repo):
    with RepositoryFile(repo) as file:
        original = file.read()
        file.save(b"- [x] two\n", {}, original, "crashed")
    external = b"- [ ] personal new work\n"
    (repo / "TODO.md").write_bytes(external)
    with RepositoryFile(repo) as file:
        with pytest.raises(FileConflict, match="interrupted"):
            file.recover(lambda _: False)
        assert file.read() == external
        assert file.pending()["token"] == "crashed"
        assert file.snapshot(file.pending()["before_hash"]) == original


def test_state_only_crash_preserves_unrelated_external_edit(repo):
    with RepositoryFile(repo) as file:
        original = file.read()
        file.save(original, {}, original, "state-only")
    external = original + b"new note\n"
    (repo / "TODO.md").write_bytes(external)
    with RepositoryFile(repo) as file:
        assert file.recover(lambda _: False) == "rolled_back"
        assert file.read() == external
        assert file.state() is None


def test_change_after_read_fails_without_a_pending_write(repo):
    with RepositoryFile(repo) as file:
        original = file.read()
        (repo / "TODO.md").write_bytes(original + b"new notes\n")
        with pytest.raises(FileConflict, match="changed"):
            file.save(b"- [x] two\n", {}, original, "conflict")
        assert file.pending() is None


def test_same_bytes_replaced_inode_is_still_a_concurrent_change(repo):
    with RepositoryFile(repo) as file:
        original = file.read()
        next_file = repo / "next"
        next_file.write_bytes(original)
        next_file.replace(repo / "TODO.md")
        with pytest.raises(FileConflict):
            file.save(b"- [x] two\n", {}, original, "conflict")


def test_edit_at_final_publish_check_is_retained_with_pending_evidence(repo, monkeypatch):
    actual_write = repo_files._atomic_write
    external = b"- [ ] concurrent human edit\n"

    def racing_write(directory, name, data, *args, **kwargs):
        if name == "TODO.md":
            (repo / "TODO.md").write_bytes(external)
        return actual_write(directory, name, data, *args, **kwargs)

    monkeypatch.setattr(repo_files, "_atomic_write", racing_write)
    with RepositoryFile(repo) as file:
        original = file.read()
        with pytest.raises(FileConflict):
            file.save(b"- [x] two\n", {}, original, "conflict")
        assert (repo / "TODO.md").read_bytes() == external
        assert file.pending()["token"] == "conflict"


def test_failed_publication_recovers_if_original_is_still_present(repo, monkeypatch):
    actual_replace = os.replace

    def fail_todo(source, destination, **kwargs):
        if destination == "TODO.md":
            raise OSError("simulated publication failure")
        return actual_replace(source, destination, **kwargs)

    with RepositoryFile(repo) as file:
        original = file.read()
        monkeypatch.setattr(repo_files.os, "replace", fail_todo)
        with pytest.raises(OSError, match="simulated"):
            file.save(b"- [x] two\n", {}, original, "failed")
        assert file.recover(lambda _: False) == "rolled_back"
        assert file.read() == original


def test_concurrent_workers_are_rejected_and_locks_release(repo):
    with RepositoryFile(repo):
        with pytest.raises(FileConflict, match="already running"):
            with RepositoryFile(repo):
                pass
    with RepositoryFile(repo) as file:
        assert file.read()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo", "large", "nonutf8"])
def test_source_must_be_single_link_bounded_utf8_regular_file(repo, kind):
    todo = repo / "TODO.md"
    todo.unlink()
    if kind == "symlink":
        target = repo / "target"
        target.write_bytes(b"- [ ] outside\n")
        todo.symlink_to(target)
    elif kind == "hardlink":
        todo.write_bytes(b"- [ ] shared\n")
        os.link(todo, repo / "other")
    elif kind == "directory":
        todo.mkdir()
    elif kind == "fifo":
        os.mkfifo(todo)
    elif kind == "large":
        todo.write_bytes(b"a" * (1024 * 1024 + 1))
    else:
        todo.write_bytes(b"\xff")
    with RepositoryFile(repo) as file:
        with pytest.raises((OSError, FileSafetyError)):
            file.read()


def test_sidecar_and_repository_symlinks_are_not_followed(repo, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    with pytest.raises(OSError):
        with RepositoryFile(alias):
            pass
    target = tmp_path / "target"
    target.mkdir()
    (repo / ".farm/tracker").symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        with RepositoryFile(repo):
            pass
    assert not list(target.iterdir())


def test_snapshot_corruption_and_journal_traversal_fail_closed(repo):
    with RepositoryFile(repo) as file:
        original = file.read()
        file.save(original, {}, original, "first")
        pending = file.pending()
        with pytest.raises(FileSafetyError, match="digest"):
            file.snapshot("../../TODO.md")
        snapshot = repo / ".farm/tracker/objects" / pending["after_hash"]
        snapshot.write_bytes(b"corrupted\n")
        with pytest.raises(FileSafetyError, match="hash"):
            file.finish("first")
        assert file.pending() is not None


def test_pending_token_cannot_be_replaced_or_finished_by_another_transaction(repo):
    with RepositoryFile(repo) as file:
        original = file.read()
        file.save(original, {}, original, "first")
        with pytest.raises(FileConflict, match="pending"):
            file.save(original, {}, original, "second")
        with pytest.raises(FileConflict, match="token"):
            file.finish("second")
        assert file.pending()["token"] == "first"


def test_crash_during_state_finalization_is_idempotently_recovered(repo, monkeypatch):
    actual_unlink = os.unlink

    def fail_journal(path, **kwargs):
        if path == "pending.json":
            raise OSError("simulated crash after state publication")
        return actual_unlink(path, **kwargs)

    with RepositoryFile(repo) as file:
        original = file.read()
        file.save(original.replace(b"one", b"two"), {"bindings": []}, original, "committed")
        monkeypatch.setattr(repo_files.os, "unlink", fail_journal)
        with pytest.raises(OSError, match="simulated"):
            file.finish("committed")
        assert file.state() is not None
        assert file.pending() is not None
        monkeypatch.setattr(repo_files.os, "unlink", actual_unlink)
        assert file.recover(lambda token: token == "committed") == "committed"
        assert file.pending() is None


def test_crash_during_snapshot_publication_leaves_no_corrupt_named_object(repo, monkeypatch):
    actual_replace = os.replace

    def fail_snapshot(source, destination, **kwargs):
        if len(destination) == 64:
            raise OSError("simulated snapshot publication failure")
        return actual_replace(source, destination, **kwargs)

    with RepositoryFile(repo) as file:
        original = file.read()
        monkeypatch.setattr(repo_files.os, "replace", fail_snapshot)
        with pytest.raises(OSError, match="simulated"):
            file.save(original, {}, original, "failed")
        assert file.pending() is None
        assert not list((repo / ".farm/tracker/objects").iterdir())
        monkeypatch.setattr(repo_files.os, "replace", actual_replace)
        file.save(original, {}, original, "retry")
        file.finish("retry")


def test_sidecar_directory_or_lock_replacement_is_detected(repo):
    with RepositoryFile(repo) as file:
        lock = repo / ".farm/tracker/lock"
        lock.rename(lock.with_name("old-lock"))
        lock.write_bytes(b"")
        with pytest.raises(FileConflict, match="lock changed"):
            file.read()
    with RepositoryFile(repo) as file:
        original = file.read()
        objects = repo / ".farm/tracker/objects"
        objects.rename(objects.with_name("old-objects"))
        objects.mkdir(mode=0o700)
        with pytest.raises(FileConflict, match="directories changed"):
            file.save(original, {}, original, "conflict")
