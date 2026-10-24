"""Preview or add only Tracker's confidential browser client and selected user grant."""

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
API_ROOT = Path('/srv/farm/sys/api')
CLIENT = 'tracker-web'
CALLBACK = 'https://tracker.noiseflood.farm'
CEILINGS = {'tracker': ['todos.read', 'todos.write']}


class SetupError(Exception):
    """Safe operator-facing message without confidential values."""


def private_directory(path, create=False):
    """Validate every ancestor and a user-owned private credential directory."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise SetupError('Credential directory must be an absolute path without traversal')
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if component != path:
                raise SetupError('Credential directory parent must already exist') from None
            if not create:
                return False
            path.mkdir(mode=0o700)
            info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SetupError('Credential paths must not contain symlinks')
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SetupError('Credential directory must be owned by this user with mode 0700')
    return True


def read_private(path):
    """Read a bounded private file without following symlinks or accepting hard links."""
    if not private_directory(path.parent):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise SetupError('Credential material must be a private regular file') from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise SetupError('Credential files must be owned by this user with mode 0600 and no hard links')
        raw = os.read(fd, 513)
        if len(raw) > 512:
            raise SetupError('Credential file exceeds its size limit')
        return raw.decode('ascii').strip()
    except UnicodeError:
        raise SetupError('Credential file has an invalid encoding') from None
    finally:
        os.close(fd)


def create_private(path, value):
    """Write a new durable secret without overwriting any existing file."""
    private_directory(path.parent, create=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as destination:
        destination.write(value + '\n')
        destination.flush()
        os.fsync(destination.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def validate_authority(connection, user_id):
    """Use Farm's existing live entitlement rules without changing its policy."""
    sys.path.insert(0, str(API_ROOT))
    try:
        from lib.managers.mcp_access_setup import validate_policy

        validate_policy(connection, [user_id], CEILINGS)
    except Exception:
        raise SetupError('Farm must have both service-access migrations, an active Tracker service, and existing read/write entitlement for this user') from None
    finally:
        sys.path.pop(0)
    service = connection.execute(
        "SELECT r.audience,r.base_url FROM auth_service_registry r "
        "JOIN services s ON s.id=r.service_id WHERE s.name='tracker'"
    ).fetchall()
    if len(service) != 1 or tuple(service[0]) != ('urn:farm:service:tracker', CALLBACK):
        raise SetupError('Tracker must already have its expected Farm audience and public URL')


def provision(connection, user_id, directory, apply=False, validator=validate_authority):
    """Insert only missing browser policy rows, preserving service and MCP registrations."""
    if not isinstance(user_id, str) or not user_id or len(user_id) > 128 or re.search(r'[\s\x00-\x1f\x7f]', user_id):
        raise SetupError('Provide an explicit immutable Farm user ID')
    if connection.in_transaction:
        raise SetupError('Setup needs its own transaction')
    directory = Path(directory)
    credential_path, key_path = directory / 'tracker-web.token', directory / 'tracker-web-vault.key'
    previous_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    connection.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
    try:
        validator(connection, user_id)
        client = connection.execute('SELECT * FROM auth_clients WHERE client_id=?', (CLIENT,)).fetchone()
        if client and (client['client_type'] != 'browser_bff' or client['enabled'] != 1
                       or json.loads(client['redirects_json']) != [CALLBACK]
                       or json.loads(client['ceilings_json']) != CEILINGS):
            raise SetupError('Existing tracker-web client differs or is disabled; review it explicitly')
        grant = connection.execute(
            "SELECT *, (expires_at IS NULL OR julianday(expires_at)>julianday('now')) AS live "
            'FROM auth_client_grants WHERE user_id=? AND client_id=?', (user_id, CLIENT)
        ).fetchone()
        if grant and (grant['enabled'] != 1 or grant['live'] != 1
                      or json.loads(grant['ceilings_json']) != CEILINGS):
            raise SetupError('Existing tracker-web grant differs, expired or was revoked; review it explicitly')
        registered = connection.execute(
            'SELECT credential_hash FROM auth_client_credentials WHERE client_id=?', (CLIENT,)
        ).fetchone()
        credential, key = read_private(credential_path), read_private(key_path)
        if credential is not None and not re.fullmatch(r'fbff_[A-Za-z0-9_-]{43,128}', credential):
            raise SetupError('Existing browser credential is malformed; it will not be replaced')
        if key is not None:
            try:
                decoded = base64.b64decode(key, validate=True)
                if len(decoded) != 32 or base64.b64encode(decoded).decode() != key:
                    raise ValueError()
            except ValueError:
                raise SetupError('Existing vault key is malformed; it will not be replaced') from None
        if registered:
            if credential is None or not hmac.compare_digest(registered[0], hashlib.sha256(credential.encode()).hexdigest()):
                raise SetupError('Recover the existing registered browser credential; setup never rotates it')
            if key is None:
                raise SetupError('Recover the existing browser vault key; setup never replaces it')
        report = {
            'client_id': CLIENT, 'user_id': user_id, 'redirect_uri': CALLBACK, 'ceilings': CEILINGS,
            'create_client': client is None, 'create_grant': grant is None,
            'create_credential': credential is None, 'create_vault_key': key is None,
            'service_or_mcp_changed': False, 'applied': apply,
        }
        if apply:
            if credential is None:
                credential = 'fbff_' + secrets.token_urlsafe(48)
                create_private(credential_path, credential)
            if key is None:
                create_private(key_path, base64.b64encode(secrets.token_bytes(32)).decode())
            if client is None:
                connection.execute(
                    'INSERT INTO auth_clients(client_id,client_type,enabled,redirects_json,ceilings_json) VALUES(?,?,1,?,?)',
                    (CLIENT, 'browser_bff', json.dumps([CALLBACK]), json.dumps(CEILINGS)),
                )
            if registered is None:
                connection.execute(
                    'INSERT INTO auth_client_credentials(client_id,credential_hash) VALUES(?,?)',
                    (CLIENT, hashlib.sha256(credential.encode()).hexdigest()),
                )
            if grant is None:
                connection.execute(
                    'INSERT INTO auth_client_grants(user_id,client_id,ceilings_json,enabled,expires_at) VALUES(?,?,?,1,NULL)',
                    (user_id, CLIENT, json.dumps(CEILINGS)),
                )
            connection.commit()
        else:
            connection.rollback()
        return report
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.row_factory = previous_factory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id', required=True, help='Existing immutable Farm user ID with Tracker read/write access')
    parser.add_argument('--apply', action='store_true', help='Explicitly create missing browser client, grant and secrets')
    args = parser.parse_args()
    try:
        uri = 'file:' + quote('/data/databases/api.db') + '?mode=' + ('rw' if args.apply else 'ro')
        with sqlite3.connect(uri, uri=True, timeout=10) as connection:
            report = provision(connection, args.user_id, ROOT / 'secrets', args.apply)
        print(json.dumps(report, indent=2))
    except SetupError as error:
        parser.exit(1, str(error) + '\n')
    except Exception:
        parser.exit(1, 'Browser setup failed; verify Farm schema and private file permissions. Existing material was retained.\n')


if __name__ == '__main__':
    main()
