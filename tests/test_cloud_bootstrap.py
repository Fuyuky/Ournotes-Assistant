import json

import pytest
from test_account_browser import folders as build_folders

from ournotes.common.files import atomic_json
from ournotes.runtime.store import RuntimeStore
from ournotes_dev.cloud_bootstrap import digest, prepare


@pytest.fixture
def bootstrap(tmp_path):
    _, catalog = build_folders.__wrapped__(tmp_path)
    db = RuntimeStore(tmp_path)
    run = db.create_run('test', 'test', 1)
    account = db.reserve(run, [])
    account['status'] = 'complete'
    db.save_account(account)
    archive = {'schema_version': 1, 'account_id': account['id'], 'alias_index': account['alias_index'],
               'alias': account['alias'], 'status': 'complete', 'used': False, 'pull_count': 43,
               'cards': [{'card': {'kind': 'member', 'id': 1, 'rarity': 'SSR',
                                  'name': 'name-member-1', 'title': 'title-1'}, 'count': 1}],
               'totals': {'member': {'SSR': 1, 'SR': 0, 'R': 22},
                          'support': {'SSR': 0, 'SR': 0, 'R': 20}}, 'UID': 'not-for-package'}
    path = tmp_path / 'results/archive/accounts/8 1 SSR.json'
    atomic_json(path, archive)
    yield tmp_path, catalog, db, archive, path
    db.close()


def test_prepare_is_readonly_private_and_deterministic(bootstrap, monkeypatch):
    root, catalog, db, archive, source = bootstrap
    # There must be no RuntimeStore initialization or HTTP operation in prepare.
    monkeypatch.setattr(RuntimeStore, '__init__', lambda *a, **k: pytest.fail('write connection'))
    before = db.db.total_changes
    destination, package = prepare(root, 'node-1', catalog_dir=catalog)
    assert destination.parent == root / 'results/maintenance'
    assert package['high_water']['test_next_index'] == 9
    assert package['high_water']['formal_next_index'] == 1 << 20
    item = package['imports'][0]
    assert item['result']['account_id'] == archive['account_id']
    assert item['channel'] == 'test' and item['node_id'] == 'node-1'
    assert item['request_id'].startswith('historical-')
    assert 'UID' not in json.dumps(package)
    assert 'status' not in item['result'] and 'owner_use' not in item['result']
    assert package['evidence']['archives'][0]['path'] == source.relative_to(root).as_posix()
    copy = dict(package)
    claimed = copy.pop('sha256')
    assert digest(copy) == claimed
    second, replay = prepare(root, 'node-1', catalog_dir=catalog)
    assert second == destination and replay == package
    assert db.db.total_changes == before


@pytest.mark.parametrize('change', ['highwater', 'uuid', 'alias', 'duplicate', 'summary'])
def test_prepare_rejects_identity_or_evidence_conflicts(bootstrap, change):
    root, catalog, db, archive, source = bootstrap
    if change == 'highwater':
        db.db.execute("UPDATE counters SET next_index=8 WHERE channel='test'")
    elif change == 'uuid':
        archive['account_id'] = 'not-original-uuid'
    elif change == 'alias':
        archive['alias'] = 'different@example.test'
    elif change == 'duplicate':
        atomic_json(source.with_name('duplicate.json'), archive)
    else:
        archive['totals']['member']['SSR'] = 2
    atomic_json(source, archive)
    with pytest.raises(ValueError):
        prepare(root, 'node-1', catalog_dir=catalog)
    assert not (root / 'results/maintenance').exists()


@pytest.mark.parametrize('change,reason', [('partial', 'not_complete'), ('cancelled', 'not_complete'),
                                         ('unknown', 'unknown_card_or_rarity'), ('summary', 'inconsistent_totals')])
def test_prepare_excludes_invalid_existing_files_and_keeps_success(bootstrap, change, reason):
    root, catalog, db, archive, _source = bootstrap
    run = db.create_run('another', 'test', 1)
    other = db.reserve(run, [])
    other['status'] = change if change in ('partial', 'cancelled') else 'complete'
    db.save_account(other)
    invalid = json.loads(json.dumps(archive))
    invalid.update(account_id=other['id'], alias_index=other['alias_index'], alias=other['alias'],
                   status=other['status'])
    if change == 'unknown':
        invalid['cards'][0]['card']['id'] = 999999
    elif change == 'summary':
        invalid['totals']['member']['SSR'] = 2
    path = root / 'results/archive/accounts/excluded.json'
    atomic_json(path, invalid)
    _, package = prepare(root, 'node-1', catalog_dir=catalog)
    assert len(package['imports']) == 1
    assert package['excluded'][0]['reason'] == reason
    assert package['excluded'][0]['path'] == path.relative_to(root).as_posix()
    assert package['excluded'][0]['sha256']
