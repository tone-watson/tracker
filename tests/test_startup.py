import io
import json

import pytest

from tracker import app
from tracker.health import check


def test_missing_credential_is_actionable_before_database_creation(tmp_path, monkeypatch):
    path = tmp_path / "missing.token"
    database = tmp_path / "db" / "tracker.db"
    monkeypatch.setenv("TRACKER_FARM_ENTITLEMENT_CREDENTIAL_FILE", str(path))
    with pytest.raises(app.StartupError, match="tracker.setup_auth --apply"):
        app.create_app(database)
    assert not database.parent.exists()


def test_malformed_credential_message_does_not_contain_secret(tmp_path):
    path = tmp_path / "token"
    secret = "THIS MUST NEVER APPEAR IN LOGS"
    path.write_text(secret)
    with pytest.raises(app.StartupError) as error:
        app.load_auth(path)
    assert secret not in str(error.value)
    assert "malformed" in str(error.value)


def test_unreadable_credential_has_safe_diagnostic(monkeypatch):
    def fail(*args, **kwargs):
        raise PermissionError("private detail")

    monkeypatch.setattr(app.Path, "read_text", fail)
    with pytest.raises(app.StartupError, match="ownership and permissions") as error:
        app.load_auth("/private/token")
    assert "private detail" not in str(error.value)


def test_startup_configuration_exit_does_not_start_server_or_scheduler(monkeypatch, caplog):
    def fail():
        raise app.StartupError("Setup required")

    monkeypatch.setattr(app, "create_app", fail)
    with pytest.raises(SystemExit) as error:
        app.main()
    assert error.value.code == 78
    assert "Setup required" in caplog.text
    assert "Traceback" not in caplog.text


class Opener:
    def __init__(self, value):
        self.value = value

    def open(self, url, timeout):
        assert url == "http://127.0.0.1:8792/healthz"
        assert timeout == 5
        return io.BytesIO(json.dumps(self.value).encode())


@pytest.mark.parametrize(
    "value", [{"status": "ok"}, {"data": {"status": "ok", "contract": "farm.dp/1"}}, None, []]
)
def test_health_rejects_another_services_success_response(value):
    with pytest.raises(ValueError):
        check(opener=Opener(value))


def test_health_accepts_tracker_contract():
    value = {"data": {"status": "ok", "contract": "farm.tracker/1"}}
    assert check(opener=Opener(value)) == value
