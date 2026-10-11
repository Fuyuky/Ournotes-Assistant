import hashlib
import json
import sqlite3

from ournotes.control.query import RuntimeQuery


def test_status_missing_and_old_schema_do_not_initialize(tmp_path):
    assert RuntimeQuery(tmp_path).runtime()["batch"] is None
    assert not (tmp_path / "state").exists()
    folder = tmp_path / "state"
    folder.mkdir()
    path = folder / "runtime.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE old_schema (id TEXT)")
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    assert RuntimeQuery(tmp_path).runtime()["error"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original


def test_status_reads_wal_and_preserves_batch_ownership(tmp_path):
    folder = tmp_path / "state"
    folder.mkdir()
    path = folder / "runtime.sqlite3"
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("CREATE TABLE batches(id TEXT,created REAL,data TEXT); CREATE TABLE runs(id TEXT,data TEXT); CREATE TABLE accounts(id TEXT,alias_index INTEGER,status TEXT,phase TEXT,run_id TEXT); CREATE TABLE counters(channel TEXT,next_index INTEGER);")
        db.execute("INSERT INTO batches VALUES(?,?,?)", ("current", 1, json.dumps({"id": "current", "status": "paused", "lanes": []})))
        db.commit()
        state = RuntimeQuery(tmp_path).runtime()
        assert state["batch"]["id"] == "current"
        assert state["version"]
        assert state["observed_at"] > 0
        assert not state["error"]
        run = {'id': 'lane', 'status': 'paused', 'active_account': None, 'completed': 0}
        batch = {'id': 'current', 'status': 'paused', 'lanes': [
            {'instance': 3, 'run_id': 'lane', 'preset': 'synthetic'}]}
        db.execute('INSERT INTO runs VALUES(?,?)', ('lane', json.dumps(run)))
        db.execute('UPDATE batches SET data=? WHERE id=?', (json.dumps(batch), 'current'))
        db.commit()
        state = RuntimeQuery(tmp_path).runtime()
        assert not state['lanes'][0]['recovery_hold']
        run['recovery_hold'] = '需要核查'
        db.execute('UPDATE runs SET data=? WHERE id=?', (json.dumps(run), 'lane'))
        db.commit()
        assert RuntimeQuery(tmp_path).runtime()['lanes'][0]['recovery_hold'] == '需要核查'
    finally:
        db.close()
