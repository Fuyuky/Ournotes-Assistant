import json

import pytest
from test_storage_policy import batch

from ournotes.common.files import file_hash
from ournotes.runtime.store import RuntimeStore
from ournotes_dev.storage_cleanup import apply_database, plan, rewrite_indexes, verify


def test_explicit_cleanup_preserves_success_and_numbering(tmp_path):
    db = RuntimeStore(tmp_path)
    _, _, good, _ = batch(db, 1, account_status='complete')
    card = {'key': 'member:1', 'id': 1, 'kind': 'member', 'rarity': 'SSR', 'title': 'a', 'name': 'b'}
    db.save_draw({'id': 'good-draw', 'account_id': good['id'], 'ordinal': 0,
                  'status': 'complete', 'pulls': 1, 'result': {'slots': [{'status': 'matched', 'card': card}]}})
    db.export(good['id'])
    _, _, bad, folder = batch(db, 2, account_status='partial')
    db.save_draw({'id': 'bad-draw', 'account_id': bad['id'], 'ordinal': 0,
                  'status': 'submitted', 'pulls': 1})
    db.action(bad['id'], {'label': 'old action'})
    counters = db.counters()
    expected = plan(tmp_path, db.db)
    assert expected['account_ids'] == [bad['id']]
    assert expected['draw_ids'] == ['bad-draw']
    assert (folder/'run.log').relative_to(tmp_path).as_posix() in expected['files']
    archives = {p:file_hash(p) for p in (tmp_path/'results/archive/accounts').glob('*.json')}
    apply_database(tmp_path, db.db, expected)
    rewrite_indexes(tmp_path, expected)
    verify(tmp_path, db.db, expected)
    assert db.account(bad['id']) is None and db.draws(bad['id']) == []
    assert db.db.execute('SELECT count(*) FROM actions WHERE account_id=?', (bad['id'],)).fetchone()[0] == 0
    assert db.counters() == counters and db.account(good['id'])['status'] == 'complete'
    assert {p:file_hash(p) for p in archives} == archives
    db.close()


def test_active_or_changed_scope_refuses_cleanup(tmp_path):
    db = RuntimeStore(tmp_path)
    value, _, account, _ = batch(db, 1)
    expected = plan(tmp_path, db.db)
    value['status'] = 'paused'
    db.save_batch(value)
    with pytest.raises(ValueError, match='未结束'):
        apply_database(tmp_path, db.db, expected)
    assert db.account(account['id']) is not None
    value['status'] = 'complete'
    db.save_batch(value)
    batch(db, 2)
    with pytest.raises(ValueError, match='变化'):
        apply_database(tmp_path, db.db, expected)
    assert db.account(account['id']) is not None
    db.close()


def test_conflicting_success_archive_blocks_cleanup(tmp_path):
    db = RuntimeStore(tmp_path)
    _, _, account, _ = batch(db, 1)
    folder = tmp_path/'results/archive/accounts'
    folder.mkdir(parents=True)
    (folder/'unexpected.json').write_text(json.dumps({'account_id': account['id'], 'status': 'complete'}))
    with pytest.raises(ValueError, match='成功档案'):
        plan(tmp_path, db.db)
    assert db.account(account['id']) is not None
    db.close()
