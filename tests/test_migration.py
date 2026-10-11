import json

import pytest

from ournotes.common.files import atomic_json
from ournotes.maintenance.migrate import migrate, normalize_archive
from ournotes.runtime.store import RuntimeStore


@pytest.mark.parametrize('uid', [None, 'retired-private-value'])
def test_migration_preserves_indices_draws_and_real_archive_set(tmp_path, uid):
    db = RuntimeStore(tmp_path)
    run = db.create_run('都抽', 'formal', 1)
    account = db.reserve(run, [])
    account.update(status='cancelled', owner_use=True, used=True, game_uid=uid)
    db.save_account(account)
    db.save_draw({'id': 'unresolved', 'account_id': account['id'], 'ordinal': 0, 'status': 'submitted'})
    before = db.counters()
    archive = {'schema_version': 1, 'alias_index': account['alias_index'], 'alias': account['alias'],
               'status': 'complete', 'cards': [], 'totals': {}, 'used': True,
               'owner_use': True, 'game_uid': 'retired-private-value'}
    path = tmp_path / 'results/archive/accounts/1048576 0 SSR.json'
    atomic_json(path, archive)
    db.close()
    assert migrate(tmp_path)['applied'] is False
    assert json.loads(path.read_text('utf-8')) == archive
    report = migrate(tmp_path, apply=True)
    value = json.loads(path.read_text('utf-8'))
    assert value['used'] and value['account_id'] == account['id']
    assert not {'owner_use', 'game_uid', 'UID', 'uid'} & value.keys()
    assert (tmp_path / report['backup'] / 'runtime.sqlite3').is_file()
    db = RuntimeStore(tmp_path)
    assert not {'UID','uid','game_uid','owner_use'} & db.account(account['id']).keys()
    assert db.counters() == before
    assert db.draws(account['id'])[0]['status'] == 'submitted'
    assert len(list(path.parent.glob('*.json'))) == 1
    db.close()
    assert migrate(tmp_path)['changed_archives'] == 0


def test_conflicting_usage_is_not_guessed():
    with pytest.raises(ValueError, match='冲突'):
        normalize_archive({'owner_use': True, 'used': False}, 'original-id')


def test_migration_refuses_live_controller(tmp_path):
    from ournotes.common.secrets import SecretStore
    with SecretStore(tmp_path / 'state').lock('runner'), pytest.raises(RuntimeError):
        migrate(tmp_path)
