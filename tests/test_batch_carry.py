import copy
from unittest.mock import Mock

import pytest

from ournotes.runtime.pools import make_plan
from ournotes.runtime.scheduler import MultiRunner, create_batch, preserve_unsettled_devices
from ournotes.runtime.store import RuntimeStore


def test_submission_abandon_replaces_shared_slot_without_reusing_identity(tmp_path):
    from ournotes.runtime.abandon import retire_submission_error
    db = RuntimeStore(tmp_path)
    try:
        batch = create_batch(db, [(2, 'MyGO only')], 'test', 1)
        run = db.get_run(batch['lanes'][0]['run_id'])
        account = db.reserve(run, make_plan(run['preset']))
        draw = {'id': 'submitted', 'account_id': account['id'], 'ordinal': 0,
                'status': 'submitted', 'pulls': 10, 'pool_key': 'our-notes-standard'}
        db.save_draw(draw)
        assert db.batch_quota_exhausted(run)
        retire_submission_error(db, run, account, '已提交抽卡期间发生网络错误')
        assert not db.batch_quota_exhausted(run)
        replacement = db.reserve(run, make_plan(run['preset']))
        assert replacement['id'] != account['id']
        assert replacement['alias_index'] > account['alias_index']
        assert db.draws(account['id']) == [draw]
        assert db.get_run(run['id'])['completed'] == 0
        assert db.batch_quota_exhausted(run)
    finally:
        db.close()


def closed_batch(db):
    batch = create_batch(db, [(1, 'MyGO only'), (2, 'Mujica only')], 'test', 200)
    accounts = []
    for lane in batch['lanes']:
        run = db.get_run(lane['run_id'])
        account = db.reserve(run, make_plan(run['preset']))
        account.update(game_uid='test-uid', phase='draws')
        db.save_account(account)
        db.save_draw({'id': 'draw-' + account['id'], 'account_id': account['id'], 'ordinal': 0,
                      'status': 'submitted', 'pulls': 10, 'submitted_at': 123})
        run.update(status='cancelled', completed=7)
        db.save_run(run)
        accounts.append(account)
    batch['status'] = 'cancelled'
    db.save_batch(batch)
    return batch, accounts


def test_carry_keeps_identity_transactions_counts_and_quota(tmp_path):
    db = RuntimeStore(tmp_path)
    old, accounts = closed_batch(db)
    counters = db.counters()
    draws = [db.draws(a['id']) for a in accounts]
    new = create_batch(db, [(1, 'MyGO only'), (2, 'Mujica only')], 'test', 2, carry_from=old['id'])
    assert new['total_count'] == 2 and db.counters() == counters
    for a, lane, tx in zip(accounts, new['lanes'], draws):
        after = db.account(a['id'])
        assert {k: after[k] for k in a if k not in ('run_id', 'batch_id')} == {
            k: a[k] for k in a if k not in ('run_id', 'batch_id')}
        assert db.draws(a['id']) == tx
        run = db.get_run(lane['run_id'])
        assert run['active_account'] == a['id'] and run['completed'] == 0 and run['recovery_hold']
        assert db.batch_quota_exhausted(run)
        assert db.get_run(a['run_id'])['completed'] == 7
        assert db.get_run(a['run_id'])['active_account'] is None
    assert preserve_unsettled_devices(db, new)
    assert db.get_batch(old['id'])['status'] == 'cancelled'
    db.close()


@pytest.mark.parametrize('problem', ['wrong_source', 'preset', 'quota', 'mail_limit', 'needs_review'])
def test_carry_failure_is_atomic(tmp_path, problem):
    db = RuntimeStore(tmp_path)
    old, accounts = closed_batch(db)
    lanes, count, source = [(1, 'MyGO only'), (2, 'Mujica only')], 200, old['id']
    if problem == 'wrong_source':
        source = 'other'
    elif problem == 'preset':
        lanes = [(1, 'Mujica only'), (2, 'Mujica only')]
    elif problem == 'quota':
        count = 1
    elif problem == 'mail_limit':
        run = db.get_run(old['lanes'][0]['run_id'])
        run['mail_send_limit_reached'] = True
        db.save_run(run)
    else:
        draw = db.draws(accounts[0]['id'])[0]
        draw['status'] = 'needs_review'
        db.save_draw(draw)
    before = copy.deepcopy([tuple(row) for row in db.db.execute('SELECT * FROM accounts')])
    with pytest.raises(ValueError):
        create_batch(db, lanes, 'test', count, carry_from=source)
    assert db.get_batch()['id'] == old['id']
    assert [tuple(row) for row in db.db.execute('SELECT * FROM accounts')] == before
    db.close()


def test_held_account_does_not_boot_or_receive_device_input(tmp_path, monkeypatch):
    db = RuntimeStore(tmp_path)
    old, _ = closed_batch(db)
    new = create_batch(db, [(1, 'MyGO only'), (2, 'Mujica only')], 'test', 200, carry_from=old['id'])
    db.close()
    monkeypatch.setattr('ournotes.runtime.scheduler.RuntimeStore', lambda: RuntimeStore(tmp_path))
    runner = Mock()
    monkeypatch.setattr('ournotes.runtime.scheduler.Runner', runner)
    controller = MultiRunner(resume=True)
    controller.worker(new['lanes'][0])
    runner.return_value._initialize.assert_not_called()
    assert controller.updates.get()['status'] == 'paused'


def test_controller_never_schedules_held_lanes_or_closes_their_devices(tmp_path, monkeypatch):
    db = RuntimeStore(tmp_path)
    old, _ = closed_batch(db)
    new = create_batch(db, [(1, 'MyGO only'), (2, 'Mujica only')], 'test', 200, carry_from=old['id'])
    new['async_start'] = True
    db.save_batch(new)
    monkeypatch.setattr('ournotes.runtime.scheduler.RuntimeStore', lambda: db)
    monkeypatch.setattr('ournotes.runtime.scheduler.STATE', tmp_path / 'state')
    monkeypatch.setattr(db, 'repair_exports', Mock())
    monkeypatch.setattr('ournotes.runtime.scheduler.export_batch', Mock())
    shutdown = Mock()
    monkeypatch.setattr('ournotes.runtime.scheduler.shutdown_instances', shutdown)
    controller = MultiRunner(resume=True)
    controller.worker = Mock()
    controller.execute()
    controller.worker.assert_not_called()
    shutdown.assert_not_called()
    assert controller.batch['status'] == 'paused'
    assert controller.batch['shutdown']['status'] == 'kept_open'
