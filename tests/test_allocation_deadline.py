import pytest

from ournotes.runtime.scheduler import MultiRunner, create_batch
from ournotes.runtime.store import AllocationClosed, RuntimeStore


def test_deadline_is_durable_and_does_not_consume_alias(tmp_path, monkeypatch):
    db = RuntimeStore(tmp_path)
    stale = db.create_run('都抽', 'test', None)
    db.save_run(dict(stale, allocation_deadline=100))
    monkeypatch.setattr('ournotes.runtime.store.time.time', lambda: 99)
    active = db.reserve(stale, [])
    monkeypatch.setattr('ournotes.runtime.store.time.time', lambda: 100)
    before = db.counters()
    with pytest.raises(AllocationClosed):
        db.reserve(stale, [])
    assert db.counters() == before
    assert db.account(active['id'])['status'] == 'running'
    assert db.get_run(stale['id'])['active_account'] == active['id']
    db.close()


@pytest.mark.parametrize('status,closes', [('paused', False), ('complete', True)])
def test_keep_open_on_error_still_closes_on_normal_completion(tmp_path, monkeypatch, status, closes):
    import ournotes.runtime.scheduler as module
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(0, '都抽')], 'test', None)
    batch['keep_open_on_error'] = True
    db.save_batch(batch)
    db.close()
    monkeypatch.setattr(module, 'STATE', tmp_path/'state')
    monkeypatch.setattr(module, 'RuntimeStore', lambda: RuntimeStore(tmp_path))
    calls = []
    monkeypatch.setattr(module, 'shutdown_instances', lambda instances: calls.append(True) or {'status': 'closed'})
    controller = MultiRunner(resume=True)
    def worker(lane):
        store = RuntimeStore(tmp_path)
        run = store.get_run(lane['run_id'])
        run['status'] = status
        store.save_run(run)
        store.close()
    monkeypatch.setattr(controller, 'worker', worker)
    controller.execute()
    assert bool(calls) == closes
    assert controller.batch['shutdown']['status'] == ('closed' if closes else 'kept_open')


def test_resume_only_completed_paused_worker_without_touching_peers(tmp_path, monkeypatch):
    from concurrent.futures import Future

    import ournotes.runtime.scheduler as module
    from ournotes.control.operations import OperationStore
    db = RuntimeStore(tmp_path)
    batch = create_batch(db, [(0, '都抽'), (1, 'MyGO only')], 'test', None)
    run = db.get_run(batch['lanes'][0]['run_id'])
    run['status'] = 'paused'
    db.save_run(run)
    monkeypatch.setattr(module, 'STATE', tmp_path/'state')
    controller = MultiRunner(resume=True)
    controller.batch = batch
    pending, states = [], {0: {'status': 'paused'}, 1: {'status': 'running'}}
    active = {0: Future(), 1: Future()}
    operations = OperationStore(tmp_path)
    operations.submit('resume-lane-1', 'resume_lanes', {'batch_id': batch['id'], 'instances': [0]}, lambda _: {})
    controller.resume_lanes(db, pending, active, states)
    assert not pending and operations.get('resume-lane-1')['status'] == 'accepted'
    active[0].set_result(None)
    controller.resume_lanes(db, pending, active, states)
    assert pending == [batch['lanes'][0]] and operations.get('resume-lane-1')['status'] == 'succeeded'
    assert states[1]['status'] == 'running' and not active[1].done()
    assert db.counters()['test'] == 8
    db.close()
