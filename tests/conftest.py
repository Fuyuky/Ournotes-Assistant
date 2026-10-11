"""Device workflow tests use a separate durable fake allocator, never real cloud."""
import json
import sqlite3
from contextlib import closing

import pytest

from ournotes.common.identity import FORMAL_START, MAILBOX, alias_at, next_usable_index
from ournotes.runtime.store import RuntimeStore


@pytest.fixture(autouse=True)
def isolated_system_memory(monkeypatch):
    # Workflow tests must not depend on the developer host's live VM commit load.
    # Low-budget tests override this reader explicitly.
    from ournotes.common.memory import GIB, CommitMemory
    monkeypatch.setattr('ournotes.common.memory.read_commit_memory',
                        lambda: CommitMemory(8 * GIB, 32 * GIB, 16 * GIB))


@pytest.fixture(autouse=True)
def isolated_allocator(monkeypatch, tmp_path_factory):
    original = RuntimeStore._allocate

    def allocate(store, request_id, account_id, channel):
        if store.allocator is not None:
            return original(store, request_id, account_id, channel)
        assert store.root.resolve().is_relative_to(tmp_path_factory.getbasetemp().resolve()), 'real data in test'
        with closing(sqlite3.connect(store.root / 'fake-cloud.sqlite3', timeout=20)) as cloud, cloud:
            cloud.execute('CREATE TABLE IF NOT EXISTS allocations(request_id TEXT PRIMARY KEY, channel TEXT,index_value INTEGER,result TEXT)')
            cloud.execute('BEGIN IMMEDIATE')
            old = cloud.execute('SELECT result FROM allocations WHERE request_id=?', (request_id,)).fetchone()
            if old:
                return json.loads(old[0])
            start = store.counters()[channel]
            latest = cloud.execute('SELECT max(index_value)+1 FROM allocations WHERE channel=?', (channel,)).fetchone()[0]
            index = next_usable_index(max(start, latest or start))
            end = FORMAL_START if channel == 'test' else 1 << (len(MAILBOX.split('@')[0]) - 1)
            if index >= end:
                raise ValueError('当前邮箱别名区段已耗尽。')
            value = {'account_id': account_id, 'index': index, 'alias': alias_at(index), 'node_id': 'test-node'}
            cloud.execute('INSERT INTO allocations VALUES(?,?,?,?)', (request_id, channel, index, json.dumps(value)))
            return value

    monkeypatch.setattr(RuntimeStore, '_allocate', allocate)
