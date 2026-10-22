"""Preview or explicitly initialize Tracker's service entitlement credential."""

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

API_ROOT = Path("/srv/farm/sys/api")
DATABASE = Path("/data/databases/api.db")
POLICY = API_ROOT / "docs/security/fixtures/tracker-service-policy.json"
CREDENTIAL = Path("/srv/farm/private/tracker/secrets/tracker-entitlement.token")
TOKEN = re.compile(r"fsvc_[A-Za-z0-9_-]{43,128}")


class SetupError(Exception):
    """An operator-facing setup failure that never includes credential material."""


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise SetupError("Setup paths must be absolute and must not contain parent traversal")
    return path


def _policy(path, credential):
    try:
        raw = path.read_bytes()
        if len(raw) > 32768:
            raise ValueError()
        config = json.loads(raw)
    except (OSError, ValueError):
        raise SetupError("Cannot read a valid Tracker service policy") from None
    scopes = ["todos.read", "todos.write"]
    expected = {
        "contract": "farm.service-access/1",
        "services": [
            {
                "service_id": "tracker",
                "audience": "urn:farm:service:tracker",
                "display_name": "Tracker",
                "base_url": "https://tracker.noiseflood.farm",
                "owner": "tracker",
                "capabilities_path": "/api/capabilities",
                "credential_file": str(credential),
                "scopes": scopes,
                "roles": {"admin": scopes, "user": scopes},
            }
        ],
        "clients": [],
        "memberships": [],
        "grants": [],
    }
    if config != expected:
        raise SetupError(
            "Policy must contain only the supported Tracker registration, matching credential path, and empty clients, memberships and grants"
        )
    return raw


def _private_directory(path):
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if component == path:
                return False
            raise SetupError("The credential directory's parent must already exist") from None
        if stat.S_ISLNK(info.st_mode):
            raise SetupError("Credential paths must not contain symlinks")
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SetupError("Credential directory must be owned by this user with mode 0700")
    return True


def _credential(path):
    if not _private_directory(path.parent):
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise SetupError("Credential must be a private regular file, not a symlink") from None
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise SetupError(
                "Credential must be owned by this user with mode 0600 and no hard links"
            )
        raw = os.read(descriptor, 512)
        token = raw.decode("ascii").strip()
        if len(raw) == 512 or TOKEN.fullmatch(token) is None:
            raise SetupError(
                "Existing Tracker credential has an invalid format; it will not be replaced"
            )
        return token
    except UnicodeError:
        raise SetupError(
            "Existing Tracker credential has an invalid format; it will not be replaced"
        ) from None
    finally:
        os.close(descriptor)


def _registration(database):
    try:
        with sqlite3.connect(
            "file:" + quote(str(database)) + "?mode=ro", uri=True, timeout=10
        ) as connection:
            rows = connection.execute(
                "SELECT r.audience,r.credential_hash FROM auth_service_registry r "
                "JOIN services s ON s.id=r.service_id WHERE s.name='tracker'"
            ).fetchall()
    except sqlite3.Error:
        raise SetupError(
            "Cannot read API service registration; verify the database and service-access migrations"
        ) from None
    if len(rows) > 1:
        raise SetupError("Tracker has multiple service registrations; resolve them before setup")
    if rows and rows[0][0] != "urn:farm:service:tracker":
        raise SetupError("Existing Tracker service audience does not match the reviewed policy")
    return rows[0] if rows else None


def _matching(registration, token):
    if registration is None:
        return
    if token is None:
        raise SetupError(
            "Tracker is already registered but its credential is missing; recover the original credential before continuing"
        )
    registered = registration[1]
    if not isinstance(registered, str) or not hmac.compare_digest(
        registered, hashlib.sha256(token.encode()).hexdigest()
    ):
        raise SetupError(
            "Existing Tracker credential does not match its registration; no credential or registration was changed"
        )


def _create_credential(path, token_factory):
    if not _private_directory(path.parent):
        try:
            path.parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _private_directory(path.parent)
    token = token_factory()
    if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
        raise SetupError("Credential generator returned an invalid token")
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
    except FileExistsError:
        return False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        raise SetupError(
            "Credential creation failed; inspect the private file before retrying"
        ) from None
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return True


def _provision(api_root, database, policy, apply, runner):
    command = [
        str(api_root / ".venv/bin/python"),
        "-m",
        "lib.managers.service_access_admin",
        "provision",
        "--database",
        str(database),
        "--config",
        str(policy),
    ]
    if apply:
        command.append("--apply")
    try:
        result = runner(
            command, cwd=str(api_root), capture_output=True, text=True, check=False, timeout=60
        )
        if result.returncode:
            raise SetupError(
                "API service provisioning failed; credential material was retained and command output was suppressed"
            )
        report = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise SetupError(
            "API service provisioning could not complete; command output was suppressed"
        ) from None
    expected = {
        "services": 1,
        "clients": 0,
        "explicit_memberships": 0,
        "explicit_app_grants": 0,
        "applied": apply,
    }
    if report != expected:
        raise SetupError(
            "API provisioner returned an unexpected report; inspect registration before retrying"
        )


def run_setup(
    *,
    apply=False,
    api_root=API_ROOT,
    database=DATABASE,
    policy=POLICY,
    credential=CREDENTIAL,
    runner=None,
    token_factory=None,
):
    """Validate a Tracker-only policy; apply requires explicit caller intent.

    Existing credentials are never replaced. Missing credentials on registered
    services require recovery, not rotation. API provisioning receives the
    reviewed policy only; no user memberships or client grants are added.
    """
    api_root, database, policy, credential = map(
        _absolute, (api_root, database, policy, credential)
    )
    runner = runner or subprocess.run
    token_factory = token_factory or (lambda: "fsvc_" + secrets.token_urlsafe(48))
    raw_policy = _policy(policy, credential)
    if not os.access(api_root / ".venv/bin/python", os.X_OK):
        raise SetupError(
            "The API Python runtime is missing or not executable; prepare it before setup"
        )
    registration = _registration(database)
    token = _credential(credential)
    _matching(registration, token)
    report = {
        "service": "tracker",
        "applied": False,
        "credential_created": False,
        "registered": registration is not None,
        "user_or_client_grants_changed": False,
    }
    if token is None and not apply:
        return {
            **report,
            "state": "pending_initialization",
            "next_step": "Run the same command with --apply to create the private credential and register Tracker",
        }
    if not apply:
        _provision(api_root, database, policy, False, runner)
        return {**report, "state": "ready" if registration else "pending_registration"}
    if token is None:
        report["credential_created"] = _create_credential(credential, token_factory)
        token = _credential(credential)
        if token is None:
            raise SetupError("Credential creation did not produce a readable private credential")
    _matching(_registration(database), token)
    with tempfile.TemporaryDirectory(prefix="tracker-auth-policy-") as directory:
        snapshot = Path(directory) / "policy.json"
        with snapshot.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw_policy)
            stream.flush()
            os.fsync(stream.fileno())
        _provision(api_root, database, snapshot, False, runner)
        if _credential(credential) != token:
            raise SetupError("Credential changed during preview; registration was not applied")
        _matching(_registration(database), token)
        _provision(api_root, database, snapshot, True, runner)
    registered = _registration(database)
    if registered is None:
        raise SetupError("API provisioning did not create the Tracker registration")
    _matching(registered, token)
    return {**report, "applied": True, "registered": True, "state": "ready"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create a missing private credential and register Tracker; grants remain unchanged",
    )
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run_setup(apply=args.apply), indent=2))
    except SetupError as error:
        raise SystemExit("Tracker auth setup failed: " + str(error)) from None
    except OSError:
        raise SystemExit(
            "Tracker auth setup failed: cannot access the required files; verify ownership and permissions"
        ) from None


if __name__ == "__main__":
    main()
