import json

import pytest
from PIL import Image

from ournotes.runtime.runlog import RunLog
from ournotes.runtime.store import RuntimeStore


def test_normal_frames_only_live_in_bounded_memory_and_incident_maps_them(tmp_path, monkeypatch):
    # Wall clock can repeat on Windows; frame references must remain unique.
    monkeypatch.setattr('ournotes.runtime.runlog.time.time_ns', lambda: 123456789)
    log = RunLog(tmp_path, 'a'*32)
    image = Image.new('RGB', (1100, 600), 'red')
    first = log.screenshot(image, 'before-type')
    for _ in range(160):
        log.screenshot(image, 'loading')
    assert len(log.frames) == 30
    assert not list(tmp_path.rglob('*.png'))
    last = log.screenshot(image, 'before-type')
    folder = log.incident('unexpected UI', {'account_id': 'account'})
    frames = json.loads((folder/'frames.json').read_text())['frames']
    assert len(frames) == 6
    assert first['frame_id'] not in {f['frame_id'] for f in frames}
    item = next(f for f in frames if f['frame_id'] == last['frame_id'])
    with Image.open(folder/item['file']) as screenshot:
        assert screenshot.tobytes() == image.tobytes()
    log.clear_frames()
    assert not log.frames
    log.close()



def acknowledge(db, account_id):
    from test_cloud_only_storage import ReceiptClient

    from ournotes.integrations.cloud_sync import CloudSync
    account = db.account(account_id)
    if account:
        run = db.get_run(account['run_id'])
        run['active_account'] = None
        db.save_run(run)
    CloudSync(db.root).flush(ReceiptClient())


def setup_account(tmp_path):
    db = RuntimeStore(tmp_path)
    run = db.create_run('MyGO only', 'test', 1)
    account = db.reserve(run, [])
    log = RunLog(tmp_path, run['id'])
    screenshot = log.screenshot(Image.new('RGB', (10, 10)), 'result-1', persist=True)
    card = {'key': 'member:1', 'id': 1, 'kind': 'member', 'rarity': 'SSR', 'title': 'title', 'name': 'name'}
    draw = {'id': 'draw', 'account_id': account['id'], 'ordinal': 0, 'status': 'complete',
            'screenshot': screenshot, 'result': {'slots': [{'status': 'matched', 'card': card}]}}
    db.save_draw(draw)
    return db, log, account, draw


def test_result_is_retained_until_success_and_deleted_after_durable_archive(tmp_path, monkeypatch):
    monkeypatch.setattr('ournotes.runtime.runlog.time.time_ns', lambda: 123456789)
    db, log, account, draw = setup_account(tmp_path)
    path = tmp_path/draw['screenshot']['path']
    db.export(account['id'])
    assert path.exists()
    log.screenshot(Image.new('RGB', (10, 10)), 'old-error')
    incident = log.incident('old failure', {'account_id': account['id']})
    other = log.incident('other account', {'account_id': 'different'})
    account['status'] = 'complete'
    db.save_account(account)
    report = db.export(account['id'])
    acknowledge(db, account['id'])
    assert not path.exists() and not list(incident.glob('*.png'))
    assert list(other.glob('*.png')) and not (incident/'state.json').exists()
    assert db.account(account['id']) is None
    assert report['draws'][0]['screenshot']['sha256'] == draw['screenshot']['sha256']
    assert not list((tmp_path/'results/archive/accounts').glob('*.json'))
    db.export(account['id'])  # Retry after a crash during cleanup is idempotent.
    log.close()
    db.close()


def test_maintenance_reads_incident_ownership_once_per_run(tmp_path, monkeypatch):
    from pathlib import Path

    from ournotes.runtime.retention import cleanup_success
    from ournotes.runtime.storage import ExistingStore
    db, log, account, draw = setup_account(tmp_path)
    other = db.reserve(db.get_run(account['run_id']), [])
    log.screenshot(Image.new('RGB', (10, 10)), 'failure')
    incident = log.incident('failure', {'account_id': account['id']})
    original, reads = Path.read_text, []

    def read(path, *args, **kwargs):
        if path == incident / 'state.json':
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    store = ExistingStore(tmp_path, db.db)
    removed = set()
    store.artifact_remover = lambda paths: removed.update(paths)
    cleanup_success(store, dict(other, status='complete', draws=[]))
    assert not removed
    cleanup_success(store, dict(account, status='complete', draws=[draw]))
    assert len(reads) == 1 and any(p.parent == incident for p in removed)
    assert all(p.exists() for p in removed)
    log.close()
    db.close()


def test_maintenance_inventory_does_not_follow_or_skip_linked_subdirectories(tmp_path, monkeypatch):
    import stat
    from contextlib import contextmanager
    from types import SimpleNamespace

    from ournotes.runtime.retention import existing_file
    folder = tmp_path / 'run'
    calls = []

    @contextmanager
    def scan(path):
        calls.append(path)
        assert path == folder, 'Inventory must never descend into a junction'
        yield iter([SimpleNamespace(name='linked', stat=lambda **_: SimpleNamespace(
            st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT))])

    monkeypatch.setattr('ournotes.runtime.retention.os.scandir', scan)
    store = SimpleNamespace(retention_files={})
    assert existing_file(store, folder / 'linked/evidence.png', folder)
    assert not existing_file(store, folder / 'missing.png', folder)
    assert calls == [folder]


def test_durable_frames_with_same_clock_and_label_do_not_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr('ournotes.runtime.runlog.time.time_ns', lambda: 123456789)
    log = RunLog(tmp_path, 'a' * 32)
    first = log.screenshot(Image.new('RGB', (2, 2), 'red'), 'result', persist=True)
    second = log.screenshot(Image.new('RGB', (2, 2), 'blue'), 'result', persist=True)
    assert first['path'] != second['path']
    with Image.open(tmp_path / first['path']) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0)
    log.close()


@pytest.mark.parametrize('status', ['running', 'partial', 'cancelled'])
def test_non_success_keeps_result_evidence(tmp_path, status):
    db, log, account, draw = setup_account(tmp_path)
    account['status'] = status
    db.save_account(account)
    db.export(account['id'])
    assert (tmp_path/draw['screenshot']['path']).exists()
    log.close()
    db.close()


def test_history_baseline_and_receipt_follow_success_retention(tmp_path):
    db, log, account, draw = setup_account(tmp_path)
    for key in ('history_baseline', 'history'):
        draw[key] = {'screenshot': log.screenshot(Image.new('RGB', (10, 10)), key, persist=True)}
    db.save_draw(draw)
    db.export(account['id'])
    assert all((tmp_path / draw[k]['screenshot']['path']).exists() for k in ('history_baseline', 'history'))
    account['status'] = 'complete'
    db.save_account(account)
    db.export(account['id'])
    acknowledge(db, account['id'])
    for key in ('history_baseline', 'history'):
        assert not (tmp_path / draw[key]['screenshot']['path']).exists()
        assert db.account(account['id']) is None
    log.close()
    db.close()


def test_archive_failure_cannot_delete_result(tmp_path, monkeypatch):
    db, log, account, draw = setup_account(tmp_path)
    account['status'] = 'complete'
    db.save_account(account)
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr('ournotes.integrations.cloud_sync.CloudSync.enqueue', fail)
    with pytest.raises(OSError):
        db.export(account['id'])
    assert (tmp_path/draw['screenshot']['path']).exists()
    log.close()
    db.close()


def test_pending_recognition_and_outside_paths_are_never_purged(tmp_path):
    db, log, account, draw = setup_account(tmp_path)
    account['status'] = 'complete'
    db.save_account(account)
    draw['status'] = 'captured'
    db.save_draw(draw)
    with pytest.raises(ValueError, match='未完成识别'):
        db.export(account['id'])
    assert (tmp_path/draw['screenshot']['path']).exists()
    outside = tmp_path/'keep.png'
    outside.write_bytes(b'keep')
    draw.update(status='complete', before={'path': 'keep.png'})
    db.save_draw(draw)
    db.export(account['id'])
    with pytest.raises(ValueError, match='以外'):
        acknowledge(db, account['id'])
    assert outside.exists() and (tmp_path/draw['screenshot']['path']).exists()
    log.close()
    db.close()


@pytest.mark.parametrize('problem', [None, 'account', 'destination', 'missing_source_audit', 'outside'])
def test_transferred_account_retention_requires_matching_audit(tmp_path, problem):
    db, log, account, draw = setup_account(tmp_path)
    old_run = db.get_run(account['run_id'])
    new_run = db.create_run('MyGO only', 'test', 1)
    audit = {'account_id': account['id'], 'index': account['alias_index'],
             'from_run': old_run['id'], 'from_batch': 'old',
             'to_run': new_run['id'], 'to_batch': 'new'}
    old_run.update(batch_id='old', transferred_accounts=[audit.copy()])
    if problem == 'missing_source_audit':
        old_run['transferred_accounts'] = []
    db.save_run(old_run)
    account.update(status='complete', run_id=new_run['id'], batch_id='new', batch_transfers=[audit])
    if problem == 'account':
        audit['account_id'] = 'different'
    elif problem == 'destination':
        audit['to_run'] = 'f' * 32
    db.save_account(account)
    log.screenshot(Image.new('RGB', (10, 10)), 'old-error')
    incident = log.incident('recovered', {'account_id': account['id']})
    other = log.incident('unresolved other account', {'account_id': 'different'})
    path = tmp_path / draw['screenshot']['path']
    if problem == 'outside':
        foreign = tmp_path / 'state/runs' / ('f' * 32) / 'screens/result.png'
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(b'keep')
        draw['before'] = {'path': foreign.relative_to(tmp_path).as_posix()}
        db.save_draw(draw)
    db.export(account['id'])
    if problem:
        with pytest.raises(ValueError):
            acknowledge(db, account['id'])
        assert path.exists() and list(incident.glob('*.png'))
    else:
        acknowledge(db, account['id'])
        assert not path.exists() and not list(incident.glob('*.png'))
        db.export(account['id'])  # Already-cleaned old evidence remains idempotent.
    assert list(other.glob('*.png'))
    log.close()
    db.close()
