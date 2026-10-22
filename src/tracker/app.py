"""Authenticated HTTP contract used exclusively by the Farm MCP gateway."""

import logging
import os
import sqlite3
import threading
from pathlib import Path
from uuid import uuid4

from flask import Flask, g, jsonify, request
from werkzeug.exceptions import HTTPException

from .auth import FarmAuth
from .errors import TrackerError
from .store import Store, encoded


class StartupError(Exception):
    """Operator configuration is incomplete; restarting cannot repair it."""


def load_auth(path):
    try:
        credential = Path(path).read_text().strip()
    except FileNotFoundError:
        raise StartupError(
            f"Tracker entitlement credential is missing: {path}. "
            "Run .venv/bin/python -m tracker.setup_auth --apply from the Tracker repository, "
            "then restart farm-tracker.service."
        ) from None
    except (OSError, UnicodeError):
        raise StartupError(
            "Tracker cannot read its entitlement credential. Check the configured path, "
            "file ownership and permissions before restarting."
        ) from None
    try:
        return FarmAuth(credential)
    except ValueError:
        raise StartupError(
            "Tracker entitlement credential is malformed. Restore its registered private "
            "credential before restarting; do not replace it with an unrelated token."
        ) from None


def create_app(database=None, auth=None, now=None):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    app.json.ensure_ascii = False
    if auth is None:
        path = os.environ.get(
            "TRACKER_FARM_ENTITLEMENT_CREDENTIAL_FILE",
            "/srv/farm/private/tracker/secrets/tracker-entitlement.token",
        )
        auth = load_auth(path)
    store = Store(
        database
        or os.environ.get("TRACKER_DATABASE", "/data/databases/personal/tracker/tracker.db"),
        now,
    )
    app.extensions["tracker_store"] = store

    @app.before_request
    def authorize():
        g.request_id = str(uuid4())
        if request.path == "/healthz" and request.method in {"GET", "HEAD"}:
            return None
        if request.endpoint is None:
            raise TrackerError("not_found", "Unknown Tracker route", 404)
        scope = "todos.read" if request.method in {"GET", "HEAD"} else "todos.write"
        g.principal = auth.authenticate(request.headers.get("Authorization"), scope)
        if request.method not in {"GET", "HEAD"}:
            if not request.is_json:
                raise TrackerError("invalid_request", "Content-Type must be application/json", 415)
            if not isinstance(request.get_json(), dict):
                raise TrackerError("invalid_request", "Request body must be a JSON object")
            try:
                encoded(request.get_json())
            except (ValueError, TypeError, RecursionError):
                raise TrackerError(
                    "invalid_request", "Body must contain finite JSON values"
                ) from None
        if any(len(request.args.getlist(key)) != 1 for key in request.args):
            raise TrackerError("invalid_request", "Duplicate query parameters are not accepted")

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = g.get("request_id", "")
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.errorhandler(TrackerError)
    def tracker_error(error):
        response = jsonify(
            error={"code": error.code, "message": str(error)}, request_id=g.get("request_id")
        )
        response.status_code = error.status
        if error.status == 401:
            response.headers["WWW-Authenticate"] = 'Bearer error="invalid_token"'
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        return tracker_error(TrackerError("invalid_request", error.name, error.code or 400))

    @app.errorhandler(sqlite3.OperationalError)
    def database_error(error):
        return tracker_error(
            TrackerError("database_unavailable", "Tracker database is temporarily unavailable", 503)
        )

    def result(data):
        return jsonify(data=data)

    def body():
        return request.get_json()

    def actor():
        return g.principal.actor()

    @app.get("/healthz")
    def health():
        with store.connect() as db:
            db.execute("SELECT version FROM metadata").fetchone()
        return result({"status": "ok", "contract": "farm.tracker/1"})

    @app.get("/api/capabilities")
    def capabilities():
        return result(
            {
                "contract": "farm.tracker/1",
                "service_id": "tracker",
                "default_timezone": "America/Chicago",
                "recurrence_catchup": "all_occurrences",
                "statuses": ["open", "in_progress", "blocked", "completed", "cancelled"],
            }
        )

    @app.get("/api/v1/lists")
    def lists():
        return result(store.page("list", request.args.to_dict()))

    @app.post("/api/v1/lists")
    def create_list():
        return result(store.create_list(body(), actor()))

    @app.get("/api/v1/lists/<object_id>")
    def get_list(object_id):
        return result(store.get("list", object_id))

    @app.put("/api/v1/lists/<object_id>")
    def update_list(object_id):
        return result(store.update_list(object_id, body(), actor()))

    @app.get("/api/v1/items")
    def items():
        return result(store.page("item", request.args.to_dict()))

    @app.post("/api/v1/items")
    def create_item():
        return result(store.create_item(body(), actor()))

    @app.get("/api/v1/items/<object_id>")
    def get_item(object_id):
        return result(store.get("item", object_id))

    @app.put("/api/v1/items/<object_id>")
    def update_item(object_id):
        return result(store.update_item(object_id, body(), actor()))

    @app.get("/api/v1/items/<object_id>/details")
    def details(object_id):
        return result(store.page("detail", request.args.to_dict(), object_id))

    @app.post("/api/v1/items/<object_id>/details")
    def add_detail(object_id):
        return result(store.add_detail(object_id, body(), actor()))

    @app.put("/api/v1/items/<object_id>/details/<detail_id>")
    def update_detail(object_id, detail_id):
        return result(store.update_detail(object_id, detail_id, body(), actor()))

    @app.get("/api/v1/items/<object_id>/history")
    def history(object_id):
        return result(store.page("history", request.args.to_dict(), object_id))

    @app.post("/api/v1/items/<object_id>/skip")
    def skip(object_id):
        return result(store.skip(object_id, body(), actor()))

    @app.get("/api/v1/recurrences")
    def recurrences():
        return result(store.page("recurrence", request.args.to_dict()))

    @app.get("/api/v1/recurrences/<object_id>")
    def get_recurrence(object_id):
        return result(store.get("recurrence", object_id))

    @app.put("/api/v1/recurrences/<object_id>")
    def update_recurrence(object_id):
        return result(store.update_recurrence(object_id, body(), actor()))

    return app


def run_scheduler(store, stop):
    while not stop.is_set():
        try:
            count = store.materialize(100)
            delay = 0.1 if count == 100 else 10
        except Exception:
            logging.getLogger("tracker").error(
                "Recurrence catch-up failed; retrying without advancing failed transaction"
            )
            delay = 10
        stop.wait(delay)


def main():
    from waitress import serve

    os.umask(0o077)
    try:
        app = create_app()
    except StartupError as error:
        logging.getLogger("tracker").error("%s", error)
        raise SystemExit(78) from None
    stop = threading.Event()
    worker = threading.Thread(
        target=run_scheduler, args=(app.extensions["tracker_store"], stop), daemon=True
    )
    worker.start()
    try:
        serve(
            app,
            host="127.0.0.1",
            port=int(os.environ.get("TRACKER_PORT", "8792")),
            threads=4,
            max_request_body_size=256 * 1024,
            ident="Tracker",
        )
    finally:
        stop.set()
        worker.join(timeout=15)
