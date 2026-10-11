"""Cloud confirmation, response loss, and local retirement boundaries."""
import json
import uuid

import pytest

from ournotes.integrations.cloud_client import CloudError
from ournotes.integrations.cloud_sync import CloudSync
from ournotes.runtime.store import AllocationClosed, RuntimeStore


@pytest.fixture
def completed(tmp_path):
    db = RuntimeStore(tmp_path)
    run = db.create_run('fixture', 'test', None)
    batch = {'id': uuid.uuid4().hex, 'created_at': 1, 'status': 'running',
             'count_mode': 'shared', 'total_count': 1, 'lanes': [{'run_id': run['id'], 'instance': 0}]}
    run['batch_id'] = batch['id']
    db.save_run(run)
    db.save_batch(batch)
    plan = [{'pulls': 1, 'pool_key': 'fixture', 'resource': 'stars'}]
    account = db.reserve(run, plan)
    card = {'key': 'member:1', 'kind': 'member', 'id': 1, 'rarity': 'SSR', 'title': 'fixture', 'name': 'fixture'}
    db.save_draw({'id': uuid.uuid4().hex, 'account_id': account['id'], 'ordinal': 0,
                  'status': 'complete', 'pulls': 1, 'result': {'slots': [{'status': 'matched', 'card': card}]}})
    account.update(status='complete', phase='finished', pull_count=1)
    db.save_account(account)
    run.update(completed=1, active_account=None)
    db.save_run(run)
    db.export(account['id'])
    yield db, run, account, CloudSync(tmp_path)
    db.close()


class ReceiptClient:
    def __init__(self):
        self.events = []
        self.fail = False

    def upload(self, payload, event, version):
        self.events.append((event, version))
        if self.fail:
            raise CloudError('unavailable', 'synthetic lost response')
        return {'result': payload, 'source_version': version, 'version': version}


def test_confirmation_removes_local_payload_and_preserves_quota(completed):
    db, run, account, sync = completed
    assert not (db.root / 'results/archive/accounts').exists()
    assert sync.flush(ReceiptClient())['results_uploaded'] == 1
    assert db.account(account['id']) is None
    assert db.draws(account['id']) == []
    assert not (db.root / 'results/accounts' / (account['id'] + '.json')).exists()
    saved = db.get_run(run['id'])
    assert saved['cloud_confirmed'] == 1
    assert saved['cloud_totals']['member']['SSR'] == 1
    db.save_run(run)  # A worker holding an older run snapshot cannot lose the acknowledgement.
    assert db.get_run(run['id'])['cloud_confirmed'] == 1
    with sync.database() as outbox:
        row = outbox.execute('SELECT * FROM outbox').fetchone()
        assert row['pending'] == 0
        assert set(json.loads(row['payload'])) == {'sha256'}
    with pytest.raises(AllocationClosed):
        db.reserve(run, account['plan'])


def test_lost_upload_response_preserves_data_and_original_event(completed):
    db, run, account, sync = completed
    client = ReceiptClient()
    client.fail = True
    with pytest.raises(CloudError):
        sync.flush(client)
    assert db.account(account['id'])['alias'] == account['alias']
    assert sync.pending_results() == 1
    client.fail = False
    sync.flush(client)
    assert client.events[0] == client.events[1]
    assert db.get_run(run['id'])['cloud_confirmed'] == 1
    assert sync.flush(client)['uploaded'] == 0


def test_mismatched_receipt_does_not_retire(completed):
    db, _, account, sync = completed

    class WrongClient:
        def upload(self, payload, event, version):
            return {'result': {**payload, 'index': payload['index'] + 1}}

    with pytest.raises(ValueError, match='回执'):
        sync.flush(WrongClient())
    assert db.account(account['id']) is not None
    assert sync.pending_results() == 1


def test_cleanup_failure_retries_without_uploading_again(completed, monkeypatch):
    db, _, account, sync = completed
    from ournotes.runtime import cloud_storage
    original = cloud_storage.retire_confirmed
    client = ReceiptClient()

    def fail(*args):
        raise OSError('synthetic disk failure')

    monkeypatch.setattr(cloud_storage, 'retire_confirmed', fail)
    with pytest.raises(OSError):
        sync.flush(client)
    assert db.account(account['id']) is not None
    monkeypatch.setattr(cloud_storage, 'retire_confirmed', original)
    sync.flush(client)
    assert len(client.events) == 1
    assert db.account(account['id']) is None


def test_legacy_migration_dry_run_and_acknowledgement(completed):
    from ournotes.common.files import atomic_json
    from ournotes.maintenance.cloud_storage import migrate
    db, run, account, sync = completed
    from ournotes.accounts.archive import compact_account
    archive = compact_account(db.report(account['id']))
    path = db.root / 'results/archive/accounts/fixture.json'
    atomic_json(path, archive)
    with sync.database() as outbox:
        payload = json.loads(outbox.execute('SELECT payload FROM outbox').fetchone()[0])
    class Server:
        def get_result(self, identifier):
            assert identifier == account['id']
            return {'result': payload, 'source_version': 1}
    assert migrate(db.root, Server())['verified'] == 1
    assert path.exists() and db.account(account['id'])
    assert migrate(db.root, Server(), apply=True)['retired'] == 1
    assert not path.exists() and db.account(account['id']) is None
    assert db.get_run(run['id'])['cloud_confirmed'] == 1


def test_legacy_migration_preserves_uncertain_usage(completed):
    from ournotes.accounts.archive import compact_account
    from ournotes.common.files import atomic_json
    from ournotes.maintenance.cloud_storage import migrate
    db, _, account, _ = completed
    value = compact_account(db.report(account['id']))
    path = db.root / 'results/archive/accounts/fixture.json'
    atomic_json(path, {**value, 'used_pending': True})
    assert migrate(db.root, object(), apply=True)['protected'] == 1
    assert path.exists() and db.account(account['id'])
