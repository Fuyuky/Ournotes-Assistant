"""Length filtering happens before allocation, with durable receipts and no game/mail calls."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from ournotes.cloud.api import create_app
from ournotes.cloud.store import Store, Unavailable
from ournotes.common.identity import FORMAL_START, MAILBOX, alias_at, alias_is_usable, next_usable_index
from ournotes.integrations.cloud_client import CloudClient, CloudError
from ournotes.runtime.store import RuntimeStore


@pytest.mark.parametrize('index,usable', [(1023, True), (2047, False), (3583, False), (3584, True)])
def test_game_limit_counts_only_local_part_and_preserves_mapping(index, usable):
    alias = alias_at(index)
    assert len(alias.split('@')[0]) == 30 + index.bit_count()
    assert len(alias) > 40  # Domain characters do not count toward the game limit.
    assert alias_is_usable(alias) is usable
    if index == 1023:
        assert len(alias.split('@')[0]) == 40


@pytest.mark.parametrize('local_length', [30, 37, 39, 40])
def test_next_mask_matches_first_usable_alias(local_length):
    mailbox = 'a' * local_length + '@gmail.com'
    for start in range(512):
        if local_length == 40 and start > 0:
            with pytest.raises(ValueError):
                next_usable_index(start, mailbox)
        else:
            expected = next(i for i in range(start, 1025)
                            if len(alias_at(i, mailbox).split('@')[0]) <= 40)
            assert next_usable_index(start, mailbox) == expected


@pytest.mark.parametrize('start,mailbox', [(-1, MAILBOX), (True, MAILBOX),
    (1 << 29, MAILBOX), (0, 'a' * 41 + '@gmail.com'),
    ((1 << 29) - 1, MAILBOX)])
def test_invalid_or_exhausted_namespace(start, mailbox):
    with pytest.raises(ValueError):
        next_usable_index(start, mailbox)


def server(tmp_path, test_start=3583, formal_start=FORMAL_START):
    cloud = Store(tmp_path / 'cloud.sqlite3', MAILBOX)
    token = cloud.create_node('node')['token']
    cloud.high_water({'test_next_index': test_start, 'formal_next_index': formal_start,
                      'evidence': 'synthetic isolated namespace'})
    return cloud, token


def allocation(cloud, number=0, channel='test'):
    return cloud.allocate('node', {'request_id': f'request-{number}',
                                  'account_id': f'account-{number}', 'channel': channel})


@pytest.mark.parametrize('channel,start,expected', [('test', 3583, 3584),
    ('formal', FORMAL_START + 1023, FORMAL_START + 1024)])
def test_server_skips_before_creating_any_allocation_or_ticket(tmp_path, channel, start, expected):
    cloud, _ = server(tmp_path, **{channel + '_start': start})
    response = allocation(cloud, channel=channel)
    assert response['index'] == expected
    assert alias_is_usable(response['alias'])
    with cloud.connection() as db:
        assert db.execute('SELECT index_no FROM allocations').fetchall()[0][0] == expected
        assert db.execute('SELECT count(*) FROM allocations').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM receipts').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM tickets').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM results').fetchone()[0] == 0
        assert int(cloud.meta(db, channel + '_next_index')) == expected + 1
    assert cloud.audit_records() == [response]
    assert allocation(Store(cloud.path, MAILBOX), channel=channel) == response


def test_server_exactly_40_is_allocated(tmp_path):
    cloud, _ = server(tmp_path, test_start=1023)
    assert allocation(cloud)['index'] == 1023


def test_concurrent_skip_is_permanent_and_keeps_receipts(tmp_path):
    cloud, _ = server(tmp_path, test_start=3570)
    with ThreadPoolExecutor(max_workers=8) as workers:
        values = list(workers.map(lambda n: allocation(cloud, n), range(32)))
    expected = [i for i in range(3570, 3700) if alias_is_usable(alias_at(i))][:32]
    assert sorted(v['index'] for v in values) == expected
    assert 3583 not in expected
    restarted = Store(cloud.path, MAILBOX)
    assert [allocation(restarted, n) for n in range(32)] == values
    assert len(restarted.audit_records()) == 32


def test_fsynced_skip_survives_database_rollback(tmp_path, monkeypatch):
    cloud, _ = server(tmp_path)
    save = cloud.save_receipt
    monkeypatch.setattr(cloud, 'save_receipt', lambda *args: (_ for _ in ()).throw(OSError('disk failure')))
    with pytest.raises(OSError):
        allocation(cloud)
    monkeypatch.setattr(cloud, 'save_receipt', save)
    recovered = Store(cloud.path, MAILBOX)
    assert allocation(recovered)['index'] == 3584
    assert allocation(recovered, 1)['index'] == 3585
    assert [r['index'] for r in recovered.audit_records()] == [3584, 3585]


@pytest.mark.parametrize('channel,start', [('test', FORMAL_START - 1), ('formal', (1 << 29) - 1)])
def test_exhaustion_does_not_cross_channel_or_create_records(tmp_path, channel, start):
    cloud, _ = server(tmp_path, **{channel + '_start': start})
    with pytest.raises(Unavailable):
        allocation(cloud, channel=channel)
    with cloud.connection() as db:
        assert db.execute('SELECT count(*) FROM allocations').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM receipts').fetchone()[0] == 0
        assert int(cloud.meta(db, channel + '_next_index')) == start
    assert not cloud.audit_path.exists()


@pytest.mark.parametrize('failure', ['response', 'local_commit'])
def test_real_http_skip_and_recovery_create_one_local_account(tmp_path, monkeypatch, failure):
    cloud, token = server(tmp_path / 'server')
    with TestClient(create_app(cloud, 'synthetic-administrator-credential-1234567890')) as transport:
        client = CloudClient('http://127.0.0.1', token, transport=transport)
        calls = []

        class Allocator:
            def allocate(self, request_id, account_id, channel):
                result = client.allocate(request_id, account_id, channel)
                calls.append(result)
                if failure == 'response' and len(calls) == 1:
                    raise CloudError('unavailable', 'synthetic lost response')
                return result

        root = tmp_path / 'client'
        local = RuntimeStore(root, allocator=Allocator())
        run = local.create_run('都抽', 'test', 1)
        if failure == 'local_commit':
            def fail(_):
                raise OSError('synthetic local disk failure')
            monkeypatch.setattr(local, 'save_run', fail)
        with pytest.raises((OSError, CloudError)):
            local.reserve(run, [])
        assert local.db.execute('SELECT count(*) FROM accounts').fetchone()[0] == 0
        local.close()
        local = RuntimeStore(root, allocator=Allocator())
        try:
            account = local.reserve(local.get_run(run['id']), [])
            assert account['alias_index'] == 3584
            assert calls[0] == calls[1]
            assert local.db.execute('SELECT count(*) FROM accounts').fetchone()[0] == 1
            assert local.counters()['test'] == 3585
            assert len(cloud.audit_records()) == 1
        finally:
            local.close()


def test_client_rejects_old_server_overlong_receipt_without_artifacts(tmp_path):
    requests = []

    class OldAllocator:
        def allocate(self, request_id, account_id, channel):
            requests.append((request_id, account_id, channel))
            return {'account_id': account_id, 'index': 3583, 'alias': alias_at(3583), 'node_id': 'node'}

    local = RuntimeStore(tmp_path, allocator=OldAllocator())
    run = local.create_run('都抽', 'test', 1)
    for _ in range(2):
        with pytest.raises(ValueError, match='40'):
            local.reserve(run, [])
        for table in ('accounts', 'draws', 'actions'):
            assert local.db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
        assert local.get_run(run['id'])['active_account'] is None
        assert local.counters()['test'] == 8
        local.close()
        local = RuntimeStore(tmp_path, allocator=OldAllocator())
    assert requests[0] == requests[1]  # Never silently replace an already committed receipt.
    assert not (tmp_path / 'results').exists()
    assert not (tmp_path / 'state/runs').exists()
    assert local.db.execute('SELECT count(*) FROM allocation_intents').fetchone()[0] == 1
    local.close()
