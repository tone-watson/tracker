"""Schema upgrades preserve user records and serialize across service startup."""

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

from tracker.store import SCHEMA, Store


def test_upgrade_from_v1_keeps_existing_objects_events_and_receipts(tmp_path):
    path = tmp_path / 'tracker.db'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        obj = {'id': '6fcd0b15-7b9c-40a1-9cc0-9d50c3ec52ca', 'name': 'Personal', 'status': 'active',
               'revision': 7, 'created_at': '2026-10-22T00:00:00Z'}
        db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (obj['id'], 'list', json.dumps(obj), None, None, 'active', None,
                    obj['created_at'], 'personal', 7))
        db.execute('INSERT INTO events VALUES(1,?,NULL,?,?,?,?)',
                   (obj['id'], 'created', obj['created_at'], '{}', '{}'))
        db.execute('INSERT INTO receipts VALUES(?,?,?,?,?)', ('{}', 'lists', 'stable', 'hash', '{}'))
        before = tuple(db.execute('SELECT * FROM objects').fetchone())
    with ThreadPoolExecutor(max_workers=2) as pool:
        stores = list(pool.map(lambda _: Store(path), range(2)))
    with stores[0].connect() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2
        assert tuple(db.execute('SELECT * FROM objects').fetchone()) == before
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM repositories').fetchone()[0] == 0
    assert stores[1].get('list', obj['id']) == obj
