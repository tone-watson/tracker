import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tracker.setup_auth import SetupError, run_setup

SECRET = "fsvc_" + "a" * 64


@pytest.fixture
def setup(tmp_path):
    api = tmp_path / "api"
    runtime = api / ".venv/bin/python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("test runtime")
    runtime.chmod(0o700)
    credential = tmp_path / "secrets/tracker-entitlement.token"
    database = tmp_path / "api.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE services(id TEXT PRIMARY KEY,name TEXT UNIQUE);
            CREATE TABLE auth_service_registry(service_id TEXT PRIMARY KEY,audience TEXT,credential_hash TEXT);
            CREATE TABLE grants(user_id TEXT,client_id TEXT,service_id TEXT);
            INSERT INTO services VALUES('svc_other','other');
            INSERT INTO auth_service_registry VALUES('svc_other','urn:farm:service:other','unchanged-other-credential');
            INSERT INTO grants VALUES('original-user','original-client','svc_other');
        """)
    scopes = ["todos.read", "todos.write"]
    policy = tmp_path / "tracker-policy.json"
    policy.write_text(
        json.dumps(
            {
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
        )
    )
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        assert command[:4] == [str(runtime), "-m", "lib.managers.service_access_admin", "provision"]
        assert kwargs["cwd"] == str(api)
        config = json.loads(Path(command[command.index("--config") + 1]).read_text())
        assert config["clients"] == config["memberships"] == config["grants"] == []
        apply = "--apply" in command
        if apply:
            token = Path(config["services"][0]["credential_file"]).read_text().strip()
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT OR IGNORE INTO services VALUES('svc_tracker','tracker')")
                connection.execute(
                    "INSERT INTO auth_service_registry VALUES('svc_tracker','urn:farm:service:tracker',?) ON CONFLICT(service_id) DO UPDATE SET credential_hash=excluded.credential_hash",
                    (hashlib.sha256(token.encode()).hexdigest(),),
                )
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "services": 1,
                    "clients": 0,
                    "explicit_memberships": 0,
                    "explicit_app_grants": 0,
                    "applied": apply,
                }
            ),
            "",
        )

    args = dict(
        api_root=api,
        database=database,
        policy=policy,
        credential=credential,
        runner=runner,
        token_factory=lambda: SECRET,
    )
    return args, calls


def test_default_preview_creates_nothing_and_does_not_expose_secrets(setup):
    args, calls = setup
    before = args["database"].read_bytes()
    result = run_setup(**args)
    assert result["state"] == "pending_initialization"
    assert not args["credential"].parent.exists()
    assert args["database"].read_bytes() == before
    assert calls == []
    assert SECRET not in json.dumps(result)
    assert hashlib.sha256(SECRET.encode()).hexdigest() not in json.dumps(result)


def test_apply_previews_then_registers_without_rotating_or_grant_changes(setup):
    args, calls = setup
    result = run_setup(apply=True, **args)
    credential = args["credential"]
    before = credential.stat()
    assert result["credential_created"] and result["registered"]
    assert credential.read_text().strip() == SECRET
    assert credential.stat().st_mode & 0o777 == 0o600
    assert credential.parent.stat().st_mode & 0o777 == 0o700
    assert "--apply" not in calls[0][0] and "--apply" in calls[1][0]
    second = run_setup(
        apply=True, **{**args, "token_factory": lambda: pytest.fail("Must not rotate")}
    )
    assert not second["credential_created"]
    assert credential.stat().st_ino == before.st_ino
    assert credential.stat().st_mtime_ns == before.st_mtime_ns
    with sqlite3.connect(args["database"]) as connection:
        assert connection.execute("SELECT * FROM grants").fetchall() == [
            ("original-user", "original-client", "svc_other")
        ]
        assert (
            connection.execute(
                "SELECT credential_hash FROM auth_service_registry WHERE service_id='svc_other'"
            ).fetchone()[0]
            == "unchanged-other-credential"
        )
    assert SECRET not in json.dumps(calls)
    assert hashlib.sha256(SECRET.encode()).hexdigest() not in json.dumps(result)


@pytest.mark.parametrize("missing", [False, True])
def test_registered_service_requires_original_matching_credential(setup, missing):
    args, calls = setup
    run_setup(apply=True, **args)
    calls.clear()
    if missing:
        args["credential"].unlink()
    else:
        args["credential"].write_text("fsvc_" + "b" * 64)
    with pytest.raises(SetupError, match="missing|does not match"):
        run_setup(apply=True, **args)
    assert calls == []


@pytest.mark.parametrize("target", ["directory", "file"])
def test_symlink_credential_paths_are_rejected(setup, tmp_path, target):
    args, calls = setup
    directory = args["credential"].parent
    if target == "directory":
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        directory.symlink_to(real, target_is_directory=True)
    else:
        directory.mkdir(mode=0o700)
        real = tmp_path / "real-token"
        real.write_text(SECRET)
        real.chmod(0o600)
        args["credential"].symlink_to(real)
    with pytest.raises(SetupError, match="symlink"):
        run_setup(apply=True, **args)
    assert not calls


def test_existing_insecure_credential_is_not_chmodded_or_overwritten(setup):
    args, calls = setup
    args["credential"].parent.mkdir(mode=0o700)
    args["credential"].write_text(SECRET)
    args["credential"].chmod(0o644)
    with pytest.raises(SetupError, match="0600"):
        run_setup(apply=True, **args)
    assert args["credential"].stat().st_mode & 0o777 == 0o644
    assert not calls


@pytest.mark.parametrize("section", ["clients", "memberships", "grants"])
def test_policy_cannot_add_any_user_or_client_access(setup, section):
    args, calls = setup
    policy = json.loads(args["policy"].read_text())
    policy[section] = [{"unauthorized": "addition"}]
    args["policy"].write_text(json.dumps(policy))
    with pytest.raises(SetupError, match="only the supported Tracker"):
        run_setup(apply=True, **args)
    assert not args["credential"].parent.exists()
    assert not calls


def test_failed_provisioner_cannot_leak_secret_output_and_retains_credential(setup):
    args, _ = setup

    def fail(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, SECRET, SECRET)

    with pytest.raises(SetupError) as failure:
        run_setup(apply=True, **{**args, "runner": fail})
    assert SECRET not in str(failure.value)
    assert args["credential"].read_text().strip() == SECRET


@pytest.mark.parametrize(
    "field,value",
    [
        ("service_id", "other"),
        ("audience", "urn:farm:service:other"),
        ("base_url", "https://elsewhere.example"),
        ("scopes", ["todos.read", "todos.write", "admin"]),
        ("credential_file", "/tmp/different-token"),
    ],
)
def test_policy_identity_scope_host_and_credential_path_are_fixed(setup, field, value):
    args, calls = setup
    policy = json.loads(args["policy"].read_text())
    policy["services"][0][field] = value
    args["policy"].write_text(json.dumps(policy))
    with pytest.raises(SetupError, match="only the supported Tracker"):
        run_setup(apply=True, **args)
    assert not args["credential"].parent.exists()
    assert not calls


def test_existing_unregistered_credential_preview_is_read_only(setup):
    args, calls = setup
    args["credential"].parent.mkdir(mode=0o700)
    args["credential"].write_text(SECRET)
    args["credential"].chmod(0o600)
    before = args["credential"].stat()
    database = args["database"].read_bytes()
    result = run_setup(**args)
    assert result["state"] == "pending_registration"
    assert len(calls) == 1 and "--apply" not in calls[0][0]
    assert args["database"].read_bytes() == database
    assert args["credential"].stat().st_mtime_ns == before.st_mtime_ns


def test_world_accessible_credential_directory_is_rejected_without_repair(setup):
    args, calls = setup
    args["credential"].parent.mkdir(mode=0o755)
    args["credential"].parent.chmod(0o755)
    with pytest.raises(SetupError, match="0700"):
        run_setup(apply=True, **args)
    assert args["credential"].parent.stat().st_mode & 0o777 == 0o755
    assert not calls
