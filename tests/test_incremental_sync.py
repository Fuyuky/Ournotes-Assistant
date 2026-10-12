"""Cloud-only synchronization: no archive downloads, with sparse authenticated queries."""
import json

import pytest
from test_cloud_integration import archive

from ournotes.integrations.cloud_client import CloudClient
from ournotes.integrations.cloud_sync import CloudSync, SyncWorker

pytest_plugins = ['test_cloud_integration']








def test_sparse_manifest_and_conditional_read_never_include_card_payloads(integration):
    _, client, _, root, _ = integration
    sync = CloudSync(root)
    for i in range(8):
        sync.enqueue(archive(client.allocate(f'alloc-{i}', f'account-{i}', 'formal')))
    sync.flush(client)
    client.set_usage('account-3', 'booked', 1)
    value = client.sync_manifest(inventory=True)
    assert len(value['accounts']) == 8 and len(value['states']) == 1
    assert value['states'][0]['result']['account_id'] == 'account-3'
    for private in ['cards', 'email', 'totals', 'draw_labels']:
        assert private not in json.dumps(value)
    assert client.sync_manifest(since=value['revision'])['states'] == []
    client.set_usage('account-3', 'available', 2)
    assert client.sync_manifest()['states'][0]['result']['used'] is False






def test_worker_uses_sparse_endpoint_and_unchanged_revision(integration, monkeypatch):
    _, client, _, root, _ = integration
    monkeypatch.setattr(CloudClient, 'from_root', lambda _: client)
    monkeypatch.setattr('ournotes.integrations.cloud_sync.CloudClientConfig.load', lambda _: object())
    monkeypatch.setattr(client, 'list_results', lambda: pytest.fail('full results read'))
    worker = SyncWorker(root)
    worker.run_once()
    assert worker.revision == 0
    worker.status = {'state': 'pending', 'reason': 'http_error'}
    worker.run_once()
    assert worker.status['state'] == 'connected'
    assert 'reason' not in worker.status
    assert not (root / 'results/archive/accounts').exists()


def test_cloud_event_invalidates_state_without_persisting_identity(tmp_path):
    received = []
    worker = SyncWorker(tmp_path, event_observer=received.append)
    worker.on_event({'type': 'results.changed', 'revision': 7})
    assert worker.wakeup.is_set()
    assert worker.status['event_revision'] == 1
    assert received == [{'type': 'results.changed', 'revision': 7}]
    assert not list(tmp_path.iterdir())
