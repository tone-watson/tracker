import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

SPEC = importlib.util.spec_from_file_location(
    'tracker_setup_web', Path(__file__).resolve().parents[1] / 'scripts/setup_web.py'
)
web = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(web)


@pytest.fixture
def setup(tmp_path):
    connection = sqlite3.connect(tmp_path / 'farm-test.db')
    connection.executescript('''
        CREATE TABLE users(id TEXT PRIMARY KEY,is_active INTEGER,global_role TEXT);
        CREATE TABLE services(id TEXT PRIMARY KEY,name TEXT,display_name TEXT,is_active INTEGER);
        CREATE TABLE user_services(user_id TEXT,service_id TEXT,role TEXT,is_enabled INTEGER,expires_at TEXT);
        CREATE TABLE auth_service_registry(service_id TEXT,audience TEXT,base_url TEXT,auth_contract TEXT);
        CREATE TABLE auth_service_scopes(service_id TEXT,scope TEXT);
        CREATE TABLE auth_service_role_scopes(service_id TEXT,role TEXT,scope TEXT);
        CREATE TABLE auth_app_sessions(id TEXT);
        CREATE TABLE auth_clients(client_id TEXT PRIMARY KEY,client_type TEXT,enabled INTEGER,redirects_json TEXT,ceilings_json TEXT);
        CREATE TABLE auth_client_credentials(client_id TEXT PRIMARY KEY,credential_hash TEXT);
        CREATE TABLE auth_client_grants(user_id TEXT,client_id TEXT,ceilings_json TEXT,enabled INTEGER,expires_at TEXT,PRIMARY KEY(user_id,client_id));
        INSERT INTO users VALUES('user-1',1,'user');
        INSERT INTO services VALUES('svc-tracker','tracker','Tracker',1);
        INSERT INTO user_services VALUES('user-1','svc-tracker','user',1,NULL);
        INSERT INTO auth_service_registry VALUES('svc-tracker','urn:farm:service:tracker','https://tracker.noiseflood.farm','farm.service-access/1');
        INSERT INTO auth_service_scopes VALUES('svc-tracker','todos.read'),('svc-tracker','todos.write');
        INSERT INTO auth_service_role_scopes VALUES('svc-tracker','user','todos.read'),('svc-tracker','user','todos.write');
        INSERT INTO auth_clients VALUES('farm-mcp','browser_bff',1,'["https://mcp.noiseflood.farm"]','{"tracker":["todos.read"]}');
        INSERT INTO auth_client_credentials VALUES('farm-mcp','unchanged-credential-hash');
        INSERT INTO auth_client_grants VALUES('user-1','farm-mcp','{"tracker":["todos.read"]}',1,NULL);
    ''')
    yield connection, tmp_path / 'secrets'
    connection.close()


def dump(connection):
    return '\n'.join(connection.iterdump())


def test_preview_changes_no_files_or_policy(setup):
    connection, directory = setup
    before = dump(connection)
    report = web.provision(connection, 'user-1', directory)
    assert report['create_client'] and report['create_grant'] and not report['applied']
    assert not directory.exists()
    assert dump(connection) == before


def test_apply_is_idempotent_and_preserves_service_membership_mcp_and_secrets(setup):
    connection, directory = setup
    tables = ['users', 'services', 'user_services', 'auth_service_registry', 'auth_service_scopes', 'auth_service_role_scopes']
    before = {table: connection.execute(f'SELECT * FROM {table}').fetchall() for table in tables}
    first = web.provision(connection, 'user-1', directory, apply=True)
    secrets = {p.name: p.read_bytes() for p in directory.iterdir()}
    after_first = dump(connection)
    second = web.provision(connection, 'user-1', directory, apply=True)
    assert first['applied'] and not second['create_client'] and not second['create_grant']
    assert secrets == {p.name: p.read_bytes() for p in directory.iterdir()}
    assert dump(connection) == after_first
    assert directory.stat().st_mode & 0o777 == 0o700
    for p in directory.iterdir():
        assert p.stat().st_mode & 0o777 == 0o600
        assert p.read_text().strip() not in json.dumps(first)
    assert all(connection.execute(f'SELECT * FROM {table}').fetchall() == before[table] for table in tables)
    assert connection.execute("SELECT credential_hash FROM auth_client_credentials WHERE client_id='farm-mcp'").fetchone()[0] == 'unchanged-credential-hash'
    assert connection.execute("SELECT ceilings_json FROM auth_client_grants WHERE client_id='farm-mcp'").fetchone()[0] == '{"tracker":["todos.read"]}'


@pytest.mark.parametrize('change', [
    "UPDATE users SET is_active=0",
    "UPDATE user_services SET is_enabled=0",
    "UPDATE user_services SET expires_at='2000-01-01'",
    "UPDATE services SET is_active=0",
    "DELETE FROM auth_service_role_scopes WHERE scope='todos.write'",
    "UPDATE auth_service_registry SET audience='wrong'",
    "UPDATE auth_service_registry SET base_url='https://other.invalid'",
])
def test_provision_requires_existing_current_tracker_entitlement(setup, change):
    connection, directory = setup
    connection.execute(change)
    connection.commit()
    before = dump(connection)
    with pytest.raises(web.SetupError):
        web.provision(connection, 'user-1', directory, apply=True)
    assert not directory.exists()
    assert dump(connection) == before


@pytest.mark.parametrize('change', [
    "UPDATE auth_clients SET enabled=0 WHERE client_id='tracker-web'",
    "UPDATE auth_clients SET redirects_json='[\"https://wrong.invalid\"]' WHERE client_id='tracker-web'",
    "UPDATE auth_client_grants SET enabled=0 WHERE client_id='tracker-web'",
    "UPDATE auth_client_grants SET expires_at='2000-01-01' WHERE client_id='tracker-web'",
    "UPDATE auth_client_grants SET ceilings_json='{\"tracker\":[\"todos.read\"]}' WHERE client_id='tracker-web'",
    "UPDATE auth_client_credentials SET credential_hash='wrong' WHERE client_id='tracker-web'",
])
def test_existing_conflicting_or_revoked_policy_is_not_overwritten(setup, change):
    connection, directory = setup
    web.provision(connection, 'user-1', directory, apply=True)
    connection.execute(change)
    connection.commit()
    before = dump(connection)
    secrets = {p.name: p.read_bytes() for p in directory.iterdir()}
    with pytest.raises(web.SetupError):
        web.provision(connection, 'user-1', directory, apply=True)
    assert dump(connection) == before
    assert secrets == {p.name: p.read_bytes() for p in directory.iterdir()}


@pytest.mark.parametrize('filename', ['tracker-web.token', 'tracker-web-vault.key'])
def test_missing_registered_secret_is_not_rotated(setup, filename):
    connection, directory = setup
    web.provision(connection, 'user-1', directory, apply=True)
    (directory / filename).unlink()
    with pytest.raises(web.SetupError, match='Recover'):
        web.provision(connection, 'user-1', directory, apply=True)
    assert not (directory / filename).exists()


def test_symlink_secret_directory_is_rejected_without_modifying_target(setup, tmp_path):
    connection, directory = setup
    other = tmp_path / 'other'
    other.mkdir(mode=0o700)
    directory.symlink_to(other)
    with pytest.raises(web.SetupError, match='symlinks'):
        web.provision(connection, 'user-1', directory, apply=True)
    assert list(other.iterdir()) == []
