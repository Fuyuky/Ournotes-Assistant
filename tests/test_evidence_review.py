import json
from unittest.mock import Mock

import pytest

from devtools.review import review
from ournotes.common.files import atomic_json, file_hash
from ournotes.common.secrets import SecretStore
from ournotes.runtime.recognition_queue import RecognitionQueue
from ournotes.runtime.runner import Runner
from ournotes.runtime.store import RuntimeStore


@pytest.fixture
def evidence(tmp_path):
    db = RuntimeStore(tmp_path)
    run = db.create_run('test', 'test', 1)
    account = db.reserve(run, [{'pulls': 1}])
    run['status'] = 'paused'
    db.save_run(run)
    snapshot = tmp_path / 'data/catalog/snapshots/fixed'
    snapshot.mkdir(parents=True)
    card = {'kind': 'member', 'id': 1, 'key': 'member:1', 'rarity': 'SSR', 'name': 'test'}
    atomic_json(snapshot / 'catalog.json', {'cards': [card]})
    atomic_json(snapshot / 'pools.json', {})
    account.update(catalog_snapshot=snapshot.relative_to(tmp_path).as_posix(),
                   catalog_fingerprints={name: file_hash(snapshot / name) for name in ('catalog.json', 'pools.json')})
    db.save_account(account)
    source = tmp_path / 'evidence.png'
    source.write_bytes(b'offline-evidence')
    draw = {'id': 'draw', 'account_id': account['id'], 'ordinal': 0, 'status': 'needs_review',
            'pulls': 1, 'submitted_at': 10, 'completed_at': 22, 'result_exit_confirmed': True,
            'screenshot': {'path': 'evidence.png', 'sha256': file_hash(source)}}
    db.save_draw(draw)
    cache = tmp_path / 'state/runs' / run['id'] / 'recognition/draw.json'
    cached = {'draw_id': 'draw', 'screenshot_sha256': file_hash(source), 'status': 'needs_review',
              'queued_at': 11, 'started_at': 12, 'finished_at': 22, 'recognition_seconds': 9,
              'worker_seconds': 10, 'result': {'status': 'needs_review', 'layout': 1, 'slots': [],
                                            'catalog_sha256': file_hash(snapshot / 'catalog.json')}}
    atomic_json(cache, cached)
    args = {'account_id': account['id'], 'draw_id': 'draw', 'screenshot_sha256': file_hash(source),
            'cards': [{'kind': 'member', 'id': 1}], 'reason': '原图核实', 'reviewer': 'offline-test'}
    yield tmp_path, db, run, account, draw, cache, cached, args
    db.close()


def test_review_preserves_timing_and_uses_normal_completion(evidence):
    root, db, run, account, _draw, cache, original, args = evidence
    assert review(root, **args)['status'] == 'awaiting_normal_completion'
    revised = json.loads(cache.read_text('utf-8'))
    for key in ('queued_at', 'started_at', 'finished_at', 'recognition_seconds', 'worker_seconds'):
        assert revised[key] == original[key]
    saved = db.draws(account['id'])[0]
    assert saved['submitted_at'] == 10 and saved['completed_at'] == 22
    assert saved['status'] == 'captured' and saved['result_exit_confirmed']
    assert db.get_run(run['id'])['completed'] == 0
    assert db.account(account['id'])['status'] == 'running'
    assert revised['reviews'][0]['previous_status'] == 'needs_review'
    assert revised['reviews'][0]['previous_result'] == original['result']
    worker = RecognitionQueue(root, run['id'], factory=lambda: pytest.fail('must reuse reviewed cache'))
    worker.submit(saved)
    worker.close()
    r = Runner()
    r.db, r.account, r.run, r.recognition, r.log = db, account, run, worker, Mock()
    r.db.export = Mock(return_value={'id': account['id'], 'status': 'complete',
                                     'totals': {}, 'draws': [{'status': 'complete'}]})
    r.save = lambda: db.save_account(r.account)
    r.finish_account()
    assert db.draws(account['id'])[0]['status'] == 'complete'
    assert db.get_run(run['id'])['completed'] == 1
    assert db.account(account['id'])['status'] == 'complete'


def test_review_requires_exclusive_runner_lock(evidence):
    root, _, _, _, _, cache, _, args = evidence
    before = cache.read_bytes()
    with SecretStore(root / 'state').lock('runner'), pytest.raises(RuntimeError, match='资源锁.*正被占用'):
        review(root, **args)
    assert cache.read_bytes() == before


def test_review_recovers_cache_commit_before_draw_staging(evidence, monkeypatch):
    root, db, run, account, _draw, cache, _, args = evidence
    with monkeypatch.context() as context:
        context.setattr(RuntimeStore, 'save_draw', Mock(side_effect=OSError('synthetic crash')))
        with pytest.raises(OSError, match='synthetic crash'):
            review(root, **args)
    committed = cache.read_bytes()
    assert db.draws(account['id'])[0]['status'] == 'needs_review'
    assert review(root, **args)['status'] == 'awaiting_normal_completion'
    assert cache.read_bytes() == committed
    assert len(db.draws(account['id'])[0]['reviews']) == 1
    assert db.get_run(run['id'])['completed'] == 0


@pytest.mark.parametrize('change', ['running', 'inactive', 'submitted', 'foreign-account', 'image',
                                  'snapshot-missing', 'snapshot-path', 'catalog', 'pools', 'manifest',
                                  'cache-hash', 'cache-draw', 'cache-complete', 'cache-catalog',
                                  'slots', 'unknown-card', 'bool-id', 'extra-card-field', 'unsafe-id'])
def test_review_rejects_invalid_evidence_without_writing(evidence, change):
    root, db, run, account, draw, cache, cached, args = evidence
    if change == 'running':
        run['status'] = 'running'
    elif change == 'inactive':
        run['active_account'] = None
    elif change == 'submitted':
        draw['status'] = 'submitted'
    elif change == 'foreign-account':
        # SQL ownership and serialized ownership must both match.
        raw = dict(draw, account_id='another')
        db.db.execute('UPDATE draws SET data=? WHERE id=?', (json.dumps(raw), draw['id']))
    elif change == 'image':
        (root / 'evidence.png').write_bytes(b'changed')
    elif change == 'snapshot-missing':
        account.pop('catalog_snapshot')
    elif change == 'snapshot-path':
        account['catalog_snapshot'] = 'state'
    elif change in ('catalog', 'pools'):
        (root / account['catalog_snapshot'] / (change + '.json')).write_bytes(b'changed')
    elif change == 'manifest':
        account['catalog_fingerprints'].pop('pools.json')
    elif change.startswith('cache-'):
        if change == 'cache-hash':
            cached['screenshot_sha256'] = '0' * 64
        elif change == 'cache-draw':
            cached['draw_id'] = 'another'
        elif change == 'cache-complete':
            cached['status'] = 'complete'
        else:
            cached['result']['catalog_sha256'] = '0' * 64
        atomic_json(cache, cached)
    elif change == 'slots':
        args['cards'] = []
    elif change == 'unknown-card':
        args['cards'][0]['kind'] = 'support'
    elif change == 'bool-id':
        args['cards'][0]['id'] = True
    elif change == 'extra-card-field':
        args['cards'][0]['rarity'] = 'SSR'
    else:
        args['draw_id'] = '../draw'
    db.save_run(run)
    db.save_account(account)
    if change != 'foreign-account':
        db.save_draw(draw)
    before = cache.read_bytes()
    with pytest.raises(ValueError):
        review(root, **args)
    assert cache.read_bytes() == before
    assert db.get_run(run['id'])['completed'] == 0
