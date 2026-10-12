import copy

import pytest

from ournotes.runtime.pools import make_plan
from ournotes.runtime.scheduler import create_batch, exclude_active_accounts
from ournotes.runtime.store import RuntimeStore


def setup_batch(tmp_path):
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(1, '都抽'), (2, 'millsage 一期活动')], 'test', 2)
    accounts = []
    for lane in batch['lanes']:
        run = db.get_run(lane['run_id'])
        account = db.reserve(run, make_plan(run['preset']))
        db.save_draw({'id': account['id'], 'account_id': account['id'], 'ordinal': 0,
                      'status': 'submitted', 'pulls': 10})
        run.update(status='paused', recovery_hold='missing result')
        db.save_run(run)
        accounts.append(account)
    batch['status'] = 'paused'
    db.save_batch(batch)
    return db, batch, accounts


def test_explicit_exclusion_releases_quota_without_reusing_identity_or_changing_draws(tmp_path):
    db, batch, accounts = setup_batch(tmp_path)
    before = copy.deepcopy(accounts[0])
    draws, counters = db.draws(before['id']), db.counters()
    run = db.get_run(before['run_id'])
    assert db.batch_quota_exhausted(run)
    exclude_active_accounts(db, batch['id'], [before['alias_index']], 'user excluded missing result')
    after = db.account(before['id'])
    assert after['status'] == 'cancelled'
    assert all(after[k] == v for k, v in before.items() if k != 'status')
    assert db.draws(before['id']) == draws and db.counters() == counters
    run = db.get_run(run['id'])
    assert run['active_account'] is None and run['completed'] == 0 and 'recovery_hold' not in run
    assert db.get_batch()['total_count'] == 2 and not db.batch_quota_exhausted(run)
    fresh = db.reserve(run, make_plan(run['preset']))
    assert fresh['alias_index'] == counters['test'] and fresh['id'] != before['id']
    assert db.batch_quota_exhausted(run)
    assert db.account(accounts[1]['id']) == accounts[1]
    db.close()


@pytest.mark.parametrize('problem', ['other_batch', 'running', 'missing', 'completed', 'duplicate'])
def test_exclusion_rejects_stale_or_invalid_requests_atomically(tmp_path, problem):
    db, batch, accounts = setup_batch(tmp_path)
    source, indices = batch['id'], [a['alias_index'] for a in accounts]
    if problem == 'other_batch':
        source = 'other'
    elif problem == 'running':
        run = db.get_run(accounts[1]['run_id'])
        run['status'] = 'running'
        db.save_run(run)
    elif problem == 'missing':
        indices.append(99999)
    elif problem == 'duplicate':
        indices.append(indices[0])
    else:
        accounts[1]['status'] = 'complete'
        db.save_account(accounts[1])
    before = [db.account(a['id']) for a in accounts]
    with pytest.raises(ValueError):
        exclude_active_accounts(db, source, indices, 'explicit user request')
    assert [db.account(a['id']) for a in accounts] == before
    assert not db.get_batch().get('excluded_accounts')
    db.close()


@pytest.mark.parametrize('status,batch_marker', [('cancelled', None), ('cancelled', 'other'), ('running', 'same')])
def test_unrelated_cancellation_or_marker_does_not_free_quota(tmp_path, status, batch_marker):
    db, batch, accounts = setup_batch(tmp_path)
    a = accounts[0]
    a['status'] = status
    if batch_marker:
        a['batch_exclusion'] = {'batch_id': batch['id'] if batch_marker == 'same' else batch_marker}
    db.save_account(a)
    assert db.batch_quota_exhausted(db.get_run(a['run_id']))
    db.close()
