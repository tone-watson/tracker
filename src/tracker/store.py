"""Transactional task storage, immutable events, and durable recurrence cursors."""

import base64
import hashlib
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from .errors import TrackerError
from .temporal import next_due, normalize_recurrence, parse_due

ACTIVE = {"open", "in_progress", "blocked"}
STATUSES = ACTIVE | {"completed", "cancelled"}
SYSTEM = {"subject": "system", "client_id": "tracker-scheduler"}
SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE objects (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('list','item','detail','recurrence')),
    data TEXT NOT NULL CHECK(json_valid(data)), list_id TEXT REFERENCES objects(id),
    item_id TEXT REFERENCES objects(id), status TEXT, due_at TEXT, created_at TEXT NOT NULL,
    name_key TEXT, revision INTEGER NOT NULL CHECK(revision > 0)
);
CREATE UNIQUE INDEX list_names ON objects(name_key) WHERE kind='list';
CREATE UNIQUE INDEX recurrence_occurrences ON objects(
    json_extract(data,'$.recurrence_id'),json_extract(data,'$.occurrence_index'))
    WHERE kind='item' AND json_extract(data,'$.recurrence_id') IS NOT NULL;
CREATE INDEX item_query ON objects(kind,status,due_at,id);
CREATE INDEX list_items ON objects(list_id,kind,status);
CREATE INDEX item_details ON objects(item_id,kind,created_at,id);
CREATE TABLE blockers (
    item_id TEXT NOT NULL REFERENCES objects(id), blocker_id TEXT NOT NULL REFERENCES objects(id),
    PRIMARY KEY(item_id,blocker_id), CHECK(item_id != blocker_id)
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES objects(id), item_id TEXT,
    action TEXT NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL, changes TEXT NOT NULL
);
CREATE INDEX item_history ON events(item_id,id);
CREATE TABLE receipts (
    actor TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
    fingerprint TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(actor,operation,key)
);
CREATE TABLE metadata (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL);
INSERT INTO metadata VALUES(1,0);
PRAGMA user_version=1;
COMMIT;
"""

REPOSITORY_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE repositories (
    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    pending INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
    generation INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT, error TEXT, task_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sync_control (id INTEGER PRIMARY KEY CHECK(id=1), requested INTEGER NOT NULL);
INSERT INTO sync_control VALUES(1,0);
CREATE TABLE sync_commits (token TEXT PRIMARY KEY);
CREATE INDEX repository_items ON objects(json_extract(data,'$.repository_id')) WHERE kind='item';
PRAGMA user_version=2;
COMMIT;
"""


def encoded(value):
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=False, allow_nan=False, sort_keys=True
    )


def stamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def identifier(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except ValueError:
        raise TrackerError("invalid_id", "IDs must be canonical UUID strings") from None
    return value


def fields(body, allowed, required=()):
    if not isinstance(body, dict) or set(body) - set(allowed) or set(required) - set(body):
        raise TrackerError("invalid_request", "Unknown or missing request fields")


def text(value, label, maximum=200, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise TrackerError(
            "invalid_request", f"{label} must be text of at most {maximum} characters"
        )
    return value if empty else value.strip()


def detail_value(body):
    fields(body, {"label", "kind", "value"}, {"label", "kind", "value"})
    label = text(body["label"], "label")
    if not isinstance(body["kind"], str) or body["kind"] not in {"text", "json"}:
        raise TrackerError("invalid_request", "Detail kind must be text or json")
    if body["kind"] == "text" and not isinstance(body["value"], str):
        raise TrackerError("invalid_request", "Text details require a string value")
    try:
        size = len(encoded(body["value"]).encode())
    except (TypeError, ValueError, RecursionError):
        raise TrackerError("invalid_request", "Detail value must be valid JSON") from None
    if size > 16384:
        raise TrackerError("detail_too_large", "Each detail value is limited to 16 KiB", 413)
    return {"label": label, "kind": body["kind"], "value": body["value"]}


def detail_template(details):
    if not isinstance(details, list) or len(details) > 50:
        raise TrackerError("invalid_request", "Supply at most 50 initial details per request")
    result = [detail_value(d) for d in details]
    if len(encoded(result).encode()) > 32768:
        raise TrackerError(
            "details_too_large", "Initial/template details are limited to 32 KiB", 413
        )
    return result


class Store:
    def __init__(self, path, now=None):
        self.path = str(path)
        self.now = now or (lambda: datetime.now(timezone.utc))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with sqlite3.connect(self.path, isolation_level=None) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._schema(db, SCHEMA)
                version = 1
            if version == 1:
                self._schema(db, REPOSITORY_SCHEMA)
            elif version != 2:
                raise RuntimeError(
                    "Unsupported Tracker database schema; use the matching service version"
                )

    @staticmethod
    def _schema(db, script):
        for statement in script.split(";"):
            statement = statement.strip()
            if statement and statement not in {"BEGIN IMMEDIATE", "COMMIT"}:
                db.execute(statement)

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            if db.in_transaction:
                db.commit()
            if write and (
                db.execute("SELECT 1 FROM repositories WHERE pending=1 LIMIT 1").fetchone()
                or db.execute("SELECT requested FROM sync_control WHERE id=1").fetchone()[0]
            ):
                self._notify_sync()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _notify_sync(self):
        try:
            descriptor = os.open(
                str(Path(self.path).parent / "sync.request"),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "w") as stream:
                stream.write(stamp(self.now()))
        except OSError:
            logging.getLogger("tracker").warning("Sync notification failed; periodic sync will retry")

    def repositories(self):
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM repositories ORDER BY path")]
        for row in rows:
            row["pending"] = bool(row["pending"])
        return {"items": rows, "interval_hours": 4}

    def request_sync(self):
        with self.connect(True) as db:
            db.execute("UPDATE sync_control SET requested=1 WHERE id=1")
        return {"queued": True}

    def _get(self, db, kind, object_id):
        identifier(object_id)
        row = db.execute(
            "SELECT data FROM objects WHERE id=? AND kind=?", (object_id, kind)
        ).fetchone()
        if row is None:
            raise TrackerError("not_found", "Tracker record not found", 404)
        return json.loads(row["data"])

    def _expected(self, obj, revision):
        if type(revision) is not int or revision < 1:
            raise TrackerError("invalid_request", "expected_revision must be a positive integer")
        if obj["revision"] != revision:
            raise TrackerError(
                "revision_conflict", "Record changed; read it again before updating", 409
            )

    def _put(self, db, kind, obj, actor, action, before=None):
        obj = dict(obj)
        obj.setdefault("id", str(uuid4()))
        obj.setdefault("created_at", stamp(self.now()))
        obj["updated_at"] = stamp(self.now())
        obj["revision"] = 1 if before is None else before["revision"] + 1
        due_at = stamp(obj["due"]["due_at"]) if obj.get("due") else None
        try:
            db.execute(
                """INSERT INTO objects VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET data=excluded.data,list_id=excluded.list_id,
                item_id=excluded.item_id,status=excluded.status,due_at=excluded.due_at,
                name_key=excluded.name_key,revision=excluded.revision""",
                (
                    obj["id"],
                    kind,
                    encoded(obj),
                    obj.get("list_id"),
                    obj.get("item_id"),
                    obj.get("status"),
                    due_at,
                    obj["created_at"],
                    obj["name"].casefold() if kind == "list" else None,
                    obj["revision"],
                ),
            )
        except sqlite3.IntegrityError as error:
            if "name_key" in str(error):
                raise TrackerError(
                    "name_conflict", "A list already has this name, including cancelled lists", 409
                ) from None
            raise
        changes = {
            k: {"before": before.get(k) if before else None, "after": v}
            for k, v in obj.items()
            if k not in {"revision", "updated_at"} and (before is None or before.get(k) != v)
        }
        db.execute(
            "INSERT INTO events(entity_id,item_id,action,at,actor,changes) VALUES(?,?,?,?,?,?)",
            (
                obj["id"],
                obj["id"] if kind == "item" else obj.get("item_id"),
                action,
                obj["updated_at"],
                encoded(actor),
                encoded(changes),
            ),
        )
        db.execute("UPDATE metadata SET version=version+1 WHERE id=1")
        if kind == "item" and obj.get("repository_id") and actor.get("client_id") != "tracker-sync":
            db.execute(
                "UPDATE repositories SET pending=1,generation=generation+1,state='pending',error=NULL WHERE id=?",
                (obj["repository_id"],),
            )
        return obj

    def _create(self, operation, body, actor, callback):
        key = body.get("idempotency_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise TrackerError(
                "invalid_request", "A stable idempotency_key is required for creation/skip"
            )
        fingerprint = hashlib.sha256(encoded(body).encode()).hexdigest()
        owner = encoded(actor)
        with self.connect(True) as db:
            old = db.execute(
                "SELECT * FROM receipts WHERE actor=? AND operation=? AND key=?",
                (owner, operation, key),
            ).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise TrackerError(
                        "idempotency_conflict", "This key was used with different input", 409
                    )
                return json.loads(old["response"])
            result = callback(db)
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?,?,?)",
                (owner, operation, key, fingerprint, encoded(result)),
            )
            return result

    def get(self, kind, object_id):
        with self.connect() as db:
            return self._get(db, kind, object_id)

    def create_list(self, body, actor):
        fields(body, {"name", "description", "idempotency_key"}, {"name", "idempotency_key"})
        obj = {
            "name": text(body["name"], "name"),
            "description": text(body.get("description", ""), "description", 4000, True),
            "status": "active",
            "cancelled_at": None,
        }
        return self._create(
            "lists", body, actor, lambda db: self._put(db, "list", obj, actor, "created")
        )

    def update_list(self, object_id, body, actor):
        fields(body, {"expected_revision", "name", "description", "status"}, {"expected_revision"})
        with self.connect(True) as db:
            before = self._get(db, "list", object_id)
            self._expected(before, body["expected_revision"])
            if before.get("type") == "farm" and body.get("status") == "cancelled":
                raise TrackerError("managed_list", "The Farm list is managed by repository sync", 409)
            obj = dict(before)
            if "name" in body:
                obj["name"] = text(body["name"], "name")
            if "description" in body:
                obj["description"] = text(body["description"], "description", 4000, True)
            if "status" in body:
                if not isinstance(body["status"], str) or body["status"] not in {
                    "active",
                    "cancelled",
                }:
                    raise TrackerError("invalid_request", "List status must be active or cancelled")
                obj["status"] = body["status"]
                obj["cancelled_at"] = stamp(self.now()) if obj["status"] == "cancelled" else None
                if obj["status"] == "cancelled":
                    for row in db.execute(
                        "SELECT data,kind FROM objects WHERE list_id=? AND kind IN ('item','recurrence')",
                        (object_id,),
                    ).fetchall():
                        child = json.loads(row["data"])
                        if child["status"] in ACTIVE | {"active"}:
                            self._put(
                                db,
                                row["kind"],
                                {**child, "status": "cancelled", "cancelled_at": stamp(self.now())},
                                actor,
                                "list_cancelled",
                                child,
                            )
            return self._put(db, "list", obj, actor, "updated", before)

    def _due(self, value):
        if value is None:
            return None
        fields(value, {"value", "timezone"}, {"value"})
        try:
            result = parse_due(value)
            result["starts_at"], result["due_at"] = (
                stamp(result["starts_at"]),
                stamp(result["due_at"]),
            )
            return result
        except (ValueError, TypeError, OverflowError) as error:
            raise TrackerError("invalid_due", str(error)) from None

    def _recurrence(self, value, due):
        try:
            return normalize_recurrence(value, due)
        except (ValueError, TypeError, OverflowError) as error:
            raise TrackerError("invalid_recurrence", str(error)) from None

    def _blockers(self, db, obj):
        blockers = obj.get("blocked_by", [])
        if (
            not isinstance(blockers, list)
            or len(blockers) > 100
            or any(not isinstance(b, str) for b in blockers)
            or len(set(blockers)) != len(blockers)
        ):
            raise TrackerError(
                "invalid_request", "blocked_by must contain at most 100 distinct item IDs"
            )
        unresolved = []
        for blocker in blockers:
            other = self._get(db, "item", blocker)
            if other["id"] == obj["id"]:
                raise TrackerError("dependency_cycle", "An item cannot block itself", 409)
            cycle = db.execute(
                """WITH RECURSIVE ancestors(id) AS (
                SELECT blocker_id FROM blockers WHERE item_id=? UNION
                SELECT b.blocker_id FROM blockers b JOIN ancestors a ON b.item_id=a.id)
                SELECT 1 FROM ancestors WHERE id=? LIMIT 1""",
                (blocker, obj["id"]),
            ).fetchone()
            if cycle:
                raise TrackerError(
                    "dependency_cycle", "Blocking references cannot form a cycle", 409
                )
            if other["status"] != "completed":
                unresolved.append(blocker)
        if unresolved and obj["status"] in {"open", "in_progress", "completed"}:
            raise TrackerError(
                "unresolved_dependencies",
                "Resolve blockers or explicitly clear blocked_by first",
                409,
            )
        return blockers

    def _save_blockers(self, db, obj):
        db.execute("DELETE FROM blockers WHERE item_id=?", (obj["id"],))
        db.executemany(
            "INSERT INTO blockers VALUES(?,?)", [(obj["id"], b) for b in obj["blocked_by"]]
        )

    def _new_item(
        self, db, template, due, actor, recurrence_id=None, index=None, blockers=None, status="open"
    ):
        obj = {
            "id": str(uuid4()),
            "list_id": template["list_id"],
            "title": template["title"],
            "description": template["description"],
            "due": due,
            "status": status,
            "blocked_by": blockers or [],
            "completed_at": stamp(self.now()) if status == "completed" else None,
            "cancelled_at": None,
            "skipped_at": None,
            "recurrence_id": recurrence_id,
            "occurrence_index": index,
            "recurrence_advanced": False,
        }
        if template.get("repository_id"):
            obj["repository_id"] = template["repository_id"]
        self._blockers(db, obj)
        obj = self._put(db, "item", obj, actor, "created")
        self._save_blockers(db, obj)
        for value in template.get("details", []):
            self._put(
                db,
                "detail",
                {**value, "item_id": obj["id"], "cancelled": False},
                actor,
                "detail_added",
            )
        return obj

    def create_item(self, body, actor):
        fields(
            body,
            {
                "idempotency_key",
                "list_id",
                "title",
                "description",
                "status",
                "blocked_by",
                "due",
                "recurrence",
                "details",
                "repository_id",
            },
            {"idempotency_key", "list_id", "title"},
        )
        template = {
            "list_id": identifier(body["list_id"]),
            "title": text(body["title"], "title", 4000),
            "description": text(body.get("description", ""), "description", 4000, True),
            "details": detail_template(body.get("details", [])),
        }
        if body.get("repository_id"):
            template["repository_id"] = identifier(body["repository_id"])
            self._source_title(template["title"])
        due = self._due(body.get("due"))
        recurrence = (
            self._recurrence(body["recurrence"], due)
            if body.get("recurrence") is not None
            else None
        )
        status = body.get("status", "open")
        if not isinstance(status, str) or status not in ACTIVE:
            raise TrackerError("invalid_request", "New items must be open, in_progress, or blocked")

        def create(db):
            parent = self._get(db, "list", template["list_id"])
            if parent["status"] != "active":
                raise TrackerError("list_cancelled", "Restore the list before adding items", 409)
            repository_id = template.get("repository_id")
            if bool(repository_id) != (parent.get("type") == "farm"):
                raise TrackerError("repository_required", "Farm tasks require a repository; other lists do not")
            if repository_id:
                if not db.execute("SELECT 1 FROM repositories WHERE id=?", (repository_id,)).fetchone():
                    raise TrackerError("not_found", "Repository not found", 404)
                if recurrence:
                    raise TrackerError("invalid_recurrence", "Repository tasks cannot recur automatically")
            series = None
            if recurrence:
                series = self._put(
                    db,
                    "recurrence",
                    {
                        "list_id": template["list_id"],
                        "title": template["title"],
                        "description": template["description"],
                        "details": template["details"],
                        "recurrence": recurrence,
                        "anchor_due": due,
                        "next_due": None,
                        "last_due": due,
                        "next_index": 2,
                        "status": "active",
                        "cancelled_at": None,
                    },
                    actor,
                    "created",
                )
            item = self._new_item(
                db,
                template,
                due,
                actor,
                series["id"] if series else None,
                1 if series else None,
                body.get("blocked_by", []),
                status,
            )
            if series and recurrence["mode"] == "calendar":
                updated = {**series, "next_due": next_due(due, recurrence, anchor_due=due)}
                self._put(db, "recurrence", updated, actor, "scheduled", series)
            return item

        return self._create("items", body, actor, create)

    def _cancel_series(self, db, series_id, actor):
        series = self._get(db, "recurrence", series_id)
        if series["status"] != "cancelled":
            self._put(
                db,
                "recurrence",
                {**series, "status": "cancelled", "cancelled_at": stamp(self.now())},
                actor,
                "cancelled",
                series,
            )
        rows = db.execute(
            "SELECT data FROM objects WHERE kind='item' AND json_extract(data,'$.recurrence_id')=?",
            (series_id,),
        ).fetchall()
        for row in rows:
            item = json.loads(row[0])
            if item["status"] in ACTIVE:
                self._put(
                    db,
                    "item",
                    {**item, "status": "cancelled", "cancelled_at": stamp(self.now())},
                    actor,
                    "series_cancelled",
                    item,
                )

    def _advance_completion(self, db, item, actor):
        if not item["recurrence_id"]:
            return
        series = self._get(db, "recurrence", item["recurrence_id"])
        if series["status"] != "active":
            return
        if series["recurrence"]["mode"] == "calendar":
            if item["occurrence_index"] != series["next_index"] - 1 or series["next_due"] is None:
                return
            due = series["next_due"]
            following = next_due(due, series["recurrence"], anchor_due=series["anchor_due"])
        else:
            if item["occurrence_index"] != series["next_index"] - 1:
                return
            due = next_due(
                item["due"] or series["last_due"],
                series["recurrence"],
                anchor_due=series["anchor_due"],
                after=self.now(),
            )
            following = None
        if due is None:
            return
        self._new_item(db, series, due, actor, series["id"], series["next_index"])
        self._put(
            db,
            "recurrence",
            {
                **series,
                "last_due": due,
                "next_due": following,
                "next_index": series["next_index"] + 1,
            },
            actor,
            "scheduled",
            series,
        )

    def update_item(self, object_id, body, actor):
        fields(
            body,
            {"expected_revision", "title", "description", "list_id", "status", "due", "blocked_by"},
            {"expected_revision"},
        )
        with self.connect(True) as db:
            before = self._get(db, "item", object_id)
            self._expected(before, body["expected_revision"])
            obj = dict(before)
            for key, maximum in (("title", 4000), ("description", 4000)):
                if key in body:
                    obj[key] = text(body[key], key, maximum, key == "description")
            if "list_id" in body:
                obj["list_id"] = identifier(body["list_id"])
            parent = self._get(db, "list", obj["list_id"])
            if obj.get("repository_id"):
                self._source_title(obj["title"])
                if obj["list_id"] != before["list_id"]:
                    raise TrackerError("managed_item", "Repository tasks must stay in the Farm list", 409)
            elif parent.get("type") == "farm":
                raise TrackerError("repository_required", "Create a repository task in Farm instead of moving it")
            if self._get(db, "list", obj["list_id"])["status"] != "active":
                raise TrackerError(
                    "list_cancelled", "Restore the list before editing its items", 409
                )
            if "due" in body:
                obj["due"] = self._due(body["due"])
            if "status" in body:
                if not isinstance(body["status"], str) or body["status"] not in STATUSES:
                    raise TrackerError("invalid_request", "Unknown item status")
                obj["status"] = body["status"]
            if "blocked_by" in body:
                obj["blocked_by"] = body["blocked_by"]
            self._blockers(db, obj)
            changed_status = obj["status"] != before["status"]
            if changed_status:
                obj["completed_at"] = stamp(self.now()) if obj["status"] == "completed" else None
                obj["cancelled_at"] = stamp(self.now()) if obj["status"] == "cancelled" else None
            advance = (
                changed_status
                and obj["status"] == "completed"
                and not before["recurrence_advanced"]
            )
            if advance and obj["recurrence_id"]:
                obj["recurrence_advanced"] = True
            obj = self._put(db, "item", obj, actor, "updated", before)
            self._save_blockers(db, obj)
            if body.get("status") == "cancelled" and obj["recurrence_id"]:
                self._cancel_series(db, obj["recurrence_id"], actor)
            if advance:
                self._advance_completion(db, obj, actor)
            return obj

    @staticmethod
    def _source_title(value):
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise TrackerError("invalid_request", "Repository task titles must occupy one Markdown line")

    def add_detail(self, item_id, body, actor):
        fields(
            body,
            {"idempotency_key", "label", "kind", "value"},
            {"idempotency_key", "label", "kind", "value"},
        )
        value = detail_value({k: body[k] for k in ("label", "kind", "value")})

        def create(db):
            self._get(db, "item", item_id)
            return self._put(
                db,
                "detail",
                {**value, "item_id": item_id, "cancelled": False},
                actor,
                "detail_added",
            )

        return self._create("details:" + item_id, body, actor, create)

    def update_detail(self, item_id, object_id, body, actor):
        fields(
            body,
            {"expected_revision", "label", "kind", "value", "cancelled"},
            {"expected_revision"},
        )
        with self.connect(True) as db:
            before = self._get(db, "detail", object_id)
            if before["item_id"] != item_id:
                raise TrackerError("not_found", "Detail does not belong to this item", 404)
            self._expected(before, body["expected_revision"])
            value = detail_value({k: body.get(k, before[k]) for k in ("label", "kind", "value")})
            cancelled = body.get("cancelled", before["cancelled"])
            if type(cancelled) is not bool:
                raise TrackerError("invalid_request", "cancelled must be boolean")
            return self._put(
                db,
                "detail",
                {**before, **value, "cancelled": cancelled},
                actor,
                "detail_updated",
                before,
            )

    def update_recurrence(self, object_id, body, actor):
        fields(
            body,
            {"expected_revision", "status", "title", "description", "details"},
            {"expected_revision"},
        )
        with self.connect(True) as db:
            before = self._get(db, "recurrence", object_id)
            self._expected(before, body["expected_revision"])
            obj = dict(before)
            for key, maximum in (("title", 4000), ("description", 4000)):
                if key in body:
                    obj[key] = text(body[key], key, maximum, key == "description")
            if "details" in body:
                obj["details"] = detail_template(body["details"])
            if "status" in body:
                if not isinstance(body["status"], str) or body["status"] not in {
                    "active",
                    "cancelled",
                }:
                    raise TrackerError(
                        "invalid_request", "Series status must be active or cancelled"
                    )
                if (
                    body["status"] == "active"
                    and self._get(db, "list", obj["list_id"])["status"] != "active"
                ):
                    raise TrackerError("list_cancelled", "Restore the list before the series", 409)
                obj["status"] = body["status"]
                obj["cancelled_at"] = stamp(self.now()) if obj["status"] == "cancelled" else None
            result = self._put(db, "recurrence", obj, actor, "updated", before)
            if obj["status"] == "cancelled":
                self._cancel_series(db, object_id, actor)
            elif (
                before["status"] == "cancelled" and obj["recurrence"]["mode"] == "after_completion"
            ):
                due = next_due(
                    obj["last_due"],
                    obj["recurrence"],
                    anchor_due=obj["anchor_due"],
                    after=self.now(),
                )
                if due is None:
                    return result
                self._new_item(db, obj, due, actor, object_id, obj["next_index"])
                result = self._put(
                    db,
                    "recurrence",
                    {**result, "last_due": due, "next_index": obj["next_index"] + 1},
                    actor,
                    "resumed",
                    result,
                )
            return result

    def skip(self, object_id, body, actor):
        fields(
            body, {"expected_revision", "idempotency_key"}, {"expected_revision", "idempotency_key"}
        )

        def skip_one(db):
            before = self._get(db, "item", object_id)
            self._expected(before, body["expected_revision"])
            if not before["recurrence_id"] or before["status"] not in ACTIVE:
                raise TrackerError(
                    "cannot_skip", "Only an active recurring occurrence can be skipped", 409
                )
            obj = self._put(
                db,
                "item",
                {
                    **before,
                    "status": "cancelled",
                    "cancelled_at": stamp(self.now()),
                    "skipped_at": stamp(self.now()),
                    "recurrence_advanced": True,
                },
                actor,
                "skipped",
                before,
            )
            if not before["recurrence_advanced"]:
                self._advance_completion(db, obj, actor)
            return obj

        return self._create("skip:" + object_id, body, actor, skip_one)

    def materialize(self, limit=100):
        """Persist bounded catch-up batches, retaining a durable cursor for every series."""
        count = 0
        with self.connect(True) as db:
            rows = db.execute(
                "SELECT data FROM objects WHERE kind='recurrence' AND status='active' ORDER BY json_extract(data,'$.updated_at'),id"
            ).fetchall()
            for row in rows:
                series = json.loads(row[0])
                if series["recurrence"]["mode"] != "calendar":
                    continue
                while series["next_due"] and count < limit:
                    due = series["next_due"]
                    last_start = datetime.fromisoformat(
                        series["last_due"]["starts_at"].replace("Z", "+00:00")
                    )
                    if last_start > self.now():
                        break
                    self._new_item(db, series, due, SYSTEM, series["id"], series["next_index"])
                    series = self._put(
                        db,
                        "recurrence",
                        {
                            **series,
                            "last_due": due,
                            "next_due": next_due(
                                due, series["recurrence"], anchor_due=series["anchor_due"]
                            ),
                            "next_index": series["next_index"] + 1,
                        },
                        SYSTEM,
                        "scheduled",
                        series,
                    )
                    count += 1
                if count >= limit:
                    break
        return count

    def page(self, kind, query, item_id=None):
        allowed = {"limit", "cursor"}
        if kind in {"list", "item", "recurrence"}:
            allowed |= {"status", "q"}
        if kind == "item":
            allowed |= {"list_id", "repository_id", "order_by", "overdue_only"}
        fields(query, allowed)
        try:
            limit = int(query.get("limit", 10))
            if not 1 <= limit <= 50:
                raise ValueError()
        except (TypeError, ValueError):
            raise TrackerError("invalid_request", "limit must be between 1 and 50") from None
        filters = {k: v for k, v in query.items() if k != "cursor"}
        fingerprint = hashlib.sha256(encoded([kind, item_id, filters]).encode()).hexdigest()
        with self.connect() as db:
            version = db.execute("SELECT version FROM metadata").fetchone()[0]
            offset = 0
            as_of = stamp(self.now())
            if query.get("cursor"):
                try:
                    cursor = query["cursor"]
                    if len(cursor) > 512:
                        raise ValueError()
                    value = json.loads(base64.urlsafe_b64decode(cursor))
                    if (
                        value["fingerprint"] != fingerprint
                        or type(value["offset"]) is not int
                        or value["offset"] < 0
                    ):
                        raise ValueError()
                    if value["version"] != version:
                        raise TrackerError(
                            "cursor_conflict",
                            "Records changed during pagination; restart the query",
                            409,
                        )
                    offset = value["offset"]
                    if not isinstance(value["as_of"], str) or not re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value["as_of"]
                    ):
                        raise ValueError()
                    as_of = stamp(value["as_of"])
                except (ValueError, KeyError, TypeError):
                    raise TrackerError(
                        "invalid_cursor", "Cursor does not match this query"
                    ) from None
            if item_id:
                self._get(db, "item", item_id)
            args = []
            if kind == "history":
                sql, args = "SELECT * FROM events WHERE item_id=? ORDER BY id", [item_id]
            else:
                where = ["kind=?"]
                args = [kind]
                if item_id:
                    where.append("item_id=?")
                    args.append(item_id)
                if kind in {"item", "list", "recurrence"}:
                    status = query.get("status", "active")
                    if status == "active" and kind == "item":
                        where.append("status IN ('open','in_progress','blocked')")
                    elif status != "all":
                        valid = STATUSES if kind == "item" else {"active", "cancelled"}
                        if status not in valid:
                            raise TrackerError("invalid_request", "Invalid status filter")
                        where.append("status=?")
                        args.append(status)
                    if query.get("q"):
                        term = text(query["q"], "q", 200)
                        where.append(
                            "(json_extract(data,'$.title') LIKE ? ESCAPE '\\' OR json_extract(data,'$.name') LIKE ? ESCAPE '\\' OR json_extract(data,'$.description') LIKE ? ESCAPE '\\')"
                        )
                        term = (
                            "%"
                            + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                            + "%"
                        )
                        args.extend([term, term, term])
                order = "created_at,id"
                if kind == "item":
                    if query.get("repository_id"):
                        where.append("json_extract(data,'$.repository_id')=?")
                        args.append(identifier(query["repository_id"]))
                    if query.get("list_id"):
                        self._get(db, "list", query["list_id"])
                        where.append("list_id=?")
                        args.append(query["list_id"])
                    order_by = query.get("order_by", "due_at")
                    if order_by not in {"due_at", "created_at"}:
                        raise TrackerError(
                            "invalid_request", "order_by must be due_at or created_at"
                        )
                    if order_by == "due_at":
                        order = "due_at IS NULL,due_at,id"
                    overdue = query.get("overdue_only", "false")
                    if overdue not in {"true", "false"}:
                        raise TrackerError("invalid_request", "overdue_only must be true or false")
                    if overdue == "true":
                        where.append("due_at <= ? AND status IN ('open','in_progress','blocked')")
                        args.append(as_of)
                sql = "SELECT data FROM objects WHERE " + " AND ".join(where) + " ORDER BY " + order
            rows = db.execute(sql + " LIMIT ? OFFSET ?", [*args, limit + 1, offset]).fetchall()
            items, size = [], 0
            for row in rows[:limit]:
                if kind == "history":
                    item = dict(row)
                    item["actor"], item["changes"] = (
                        json.loads(item["actor"]),
                        json.loads(item["changes"]),
                    )
                else:
                    item = json.loads(row[0])
                    if kind == "recurrence":
                        item = {**item, "detail_count": len(item["details"])}
                        del item["details"]
                record_size = len(encoded(item).encode())
                if items and size + record_size > 40960:
                    break
                items.append(item)
                size += record_size
            cursor = None
            if len(rows) > len(items):
                cursor = base64.urlsafe_b64encode(
                    encoded(
                        {
                            "version": version,
                            "offset": offset + len(items),
                            "fingerprint": fingerprint,
                            "as_of": as_of,
                        }
                    ).encode()
                ).decode()
            return {"items": items, "next_cursor": cursor, "as_of": as_of}
