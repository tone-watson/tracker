"""Bounded repository TODO reconciliation with a durable filesystem/SQLite journal."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from .errors import TrackerError
from .markdown_tasks import match_tasks, parse_tasks, patch_tasks
from .repo_files import RepositoryFile, discover_repositories
from .store import Store, stamp, text

ACTOR = {"subject": "system", "client_id": "tracker-sync"}


class SyncError(Exception):
    """A repository requires attention before reconciliation can safely continue."""


def _farm_list(store, db):
    for row in db.execute("SELECT data FROM objects WHERE kind='list'"):
        item = json.loads(row[0])
        if item.get("type") == "farm":
            return item
    existing = db.execute("SELECT 1 FROM objects WHERE kind='list' AND name_key='farm'").fetchone()
    if existing:
        raise SyncError("An ordinary list named Farm already exists; rename it before enabling sync")
    return store._put(db, "list", {
        "name": "Farm", "type": "farm", "description": "Repository TODO.md tasks",
        "status": "active", "cancelled_at": None,
    }, ACTOR, "created")


def _save_item(store, db, before, changes, action):
    item = {**before, **changes}
    if item == before:
        return before
    store._blockers(db, item)
    if item["status"] != before["status"]:
        item["completed_at"] = stamp(store.now()) if item["status"] == "completed" else None
        item["cancelled_at"] = stamp(store.now()) if item["status"] == "cancelled" else None
    return store._put(db, "item", item, ACTOR, action, before)


def _source(repository, task, present=True):
    return {"path": repository["path"] + "/TODO.md", "line": task.line + 1,
            "headings": list(task.heading_path), "present": present}


def _plan(store, db, repository, parent, state, base, current):
    bindings = state.get("bindings", []) if state else []
    if not isinstance(bindings, list):
        raise SyncError("Invalid sync mapping; restore .farm/tracker from its snapshots")
    items = {item["id"]: item for item in (
        json.loads(row[0]) for row in db.execute(
            "SELECT data FROM objects WHERE kind='item' AND json_extract(data,'$.repository_id')=?",
            (repository["id"],),
        )
    )}
    if state is None and any(item.get("source") for item in items.values()):
        raise SyncError("Sync mapping is missing; restore .farm/tracker before continuing")
    matched = match_tasks(base, current, bindings)
    if matched.ambiguities:
        raise SyncError("Task identity is ambiguous after Markdown edits; restore the last synced ordering or clarify duplicate titles")
    originals = {binding["item_id"]: binding for binding in bindings}
    if set(originals) - set(items):
        raise SyncError("Sync mapping refers to missing Tracker records; restore matching sync state")
    edits, linked, appended = {}, {}, []
    for item_id, task in matched.matches.items():
        before, baseline = items[item_id], originals[item_id]
        title = before["title"] if before["title"] != baseline["title"] else task.title
        title = text(title, "title", 4000)
        store._source_title(title)
        status = before["status"]
        if status == baseline["status"] and task.checked != baseline["checked"]:
            status = "completed" if task.checked else "open"
        if status == "cancelled":
            edits[task.line] = {"remove_checkbox": True}
            items[item_id] = _save_item(store, db, before,
                {"source": {**_source(repository, task, False), "archived_status": status,
                            "archived_title": title}}, "source_archived")
            continue
        items[item_id] = _save_item(store, db, before,
            {"title": title, "status": status, "source": _source(repository, task)}, "source_updated")
        edits[task.line] = {"title": title, "checked": status == "completed"}
        linked[task.line] = item_id
    for item_id in matched.removals:
        before, baseline = items[item_id], originals[item_id]
        tracker_changed = (before["title"] != baseline["title"] or before["status"] != baseline["status"])
        if tracker_changed and before["status"] != "cancelled":
            appended.append(item_id)
        else:
            source = {**before.get("source", {}), "present": False}
            status = "completed" if before["status"] == "completed" else "cancelled"
            source.update(archived_status=status, archived_title=before["title"])
            items[item_id] = _save_item(store, db, before,
                {"status": status, "source": source}, "source_removed")
    for task in matched.additions:
        title = text(task.title, "title", 4000)
        store._source_title(title)
        item = store._new_item(db, {"list_id": parent["id"], "repository_id": repository["id"],
            "title": title, "description": ""}, None, ACTOR,
            status="completed" if task.checked else "open")
        item = _save_item(store, db, item, {"source": _source(repository, task)}, "source_linked")
        items[item["id"]], linked[task.line] = item, item["id"]
    for item_id, item in items.items():
        source = item.get("source")
        explicitly_reopened = source and not source.get("present") and (
            item["status"] != source.get("archived_status")
            or item["title"] != source.get("archived_title")
        )
        if (item_id not in originals and item_id not in linked.values()
                and item["status"] != "cancelled" and (not source or explicitly_reopened)):
            appended.append(item_id)
    append_edits = [{"title": items[key]["title"], "checked": items[key]["status"] == "completed"}
                    for key in appended]
    candidate = patch_tasks(current, edits, append_edits)
    final = parse_tasks(candidate)
    if appended:
        for item_id, task in zip(appended, final.tasks[-len(appended):], strict=True):
            linked[task.line] = item_id
            items[item_id] = _save_item(store, db, items[item_id],
                {"source": _source(repository, task)}, "source_linked")
    new_bindings = []
    for task in final.tasks:
        item_id = linked.get(task.line)
        if item_id is None:
            raise SyncError("Markdown structure changed unexpectedly while applying edits")
        item = items[item_id]
        new_bindings.append({"item_id": item_id, "line": task.line, "title": task.title,
            "checked": task.checked, "status": item["status"], "revision": item["revision"]})
    return candidate, {"schema": 1, "repository_id": repository["id"],
        "snapshot": hashlib.sha256(candidate).hexdigest(), "bindings": new_bindings}


def sync_repository(store, root, repository):
    repo_path = Path(root) / repository["path"]
    with RepositoryFile(repo_path) as workspace:
        def committed(token):
            with store.connect() as db:
                return bool(db.execute("SELECT 1 FROM sync_commits WHERE token=?", (token,)).fetchone())
        workspace.recover(committed)
        raw, state = workspace.read(), workspace.state()
        if state and (state.get("schema") != 1 or state.get("repository_id") != repository["id"]):
            raise SyncError("Sync state belongs to a different repository or database")
        base = parse_tasks(workspace.snapshot(state["snapshot"]) if state else b"")
        current = parse_tasks(raw)
        token = None
        with store.connect(True) as db:
            repository["generation"] = db.execute(
                "SELECT generation FROM repositories WHERE id=?", (repository["id"],)
            ).fetchone()[0]
            parent = _farm_list(store, db)
            candidate, next_state = _plan(store, db, repository, parent, state, base, current)
            if state != next_state or candidate != raw:
                token = str(uuid4())
                workspace.save(candidate, next_state, raw, token)
                db.execute("INSERT INTO sync_commits VALUES(?)", (token,))
            db.execute("UPDATE repositories SET pending=0,state='synced',error=NULL,last_synced_at=?,task_count=? WHERE id=?",
                       (stamp(store.now()), len(next_state["bindings"]), repository["id"]))
        if token:
            workspace.finish(token)


def run(store, root=Path("/srv/farm"), pending_only=False):
    root = Path(root)
    discovered = discover_repositories(root)
    registered = {repo.path for repo in discovered}
    with store.connect() as db:
        requested = bool(db.execute("SELECT requested FROM sync_control WHERE id=1").fetchone()[0])
    full = not pending_only or requested
    if full:
        with store.connect(True) as db:
            _farm_list(store, db)
            for repo in discovered:
                if not repo.has_todo:
                    continue
                db.execute("INSERT INTO repositories(id,path,name) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET name=excluded.name",
                           (str(uuid4()), repo.path, repo.name))
            db.execute("UPDATE sync_control SET requested=0 WHERE id=1")
    with store.connect() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM repositories" + (" WHERE pending=1" if not full else "") + " ORDER BY path")]
    result = {"repositories": len(rows), "synced": 0, "errors": 0}
    for repo in rows:
        try:
            if repo["path"] not in registered:
                raise SyncError("Repository is no longer a registered primary Farm checkout; restore its registration before synchronizing")
            sync_repository(store, root, repo)
            result["synced"] += 1
        except Exception as error:
            result["errors"] += 1
            if isinstance(error, (SyncError, TrackerError)):
                message = str(error)
            else:
                message = "Repository files or sync state could not be safely read or written; check farm init, permissions, and .farm/tracker recovery state"
            with store.connect(True) as db:
                db.execute("UPDATE repositories SET pending=0,state='attention',error=? WHERE id=? AND generation=?",
                           (message, repo["id"], repo["generation"]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/srv/farm"))
    parser.add_argument("--database", default=os.environ.get("TRACKER_DATABASE", "/data/databases/personal/tracker/tracker.db"))
    parser.add_argument("--pending", action="store_true", help="Only process queued Tracker edits")
    parser.add_argument("--initialize", action="store_true", help="Run farm init for each discovered repository with TODO.md, then sync")
    args = parser.parse_args()
    if args.initialize:
        for repo in discover_repositories(args.root):
            if repo.has_todo:
                subprocess.run([sys.executable, "/srv/farm/sys/cli/commands/init.py"], cwd=repo.root, check=True)
    result = run(Store(args.database), args.root, args.pending)
    print(json.dumps(result))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
