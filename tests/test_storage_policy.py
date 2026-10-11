import gzip
import io
import json
import os
import subprocess
import uuid
from dataclasses import asdict

import pytest

from ournotes.common.files import atomic_json, file_hash
from ournotes.common.secrets import SecretStore
from ournotes.control.launcher import JobManager
from ournotes.control.recovery import lane_recovery
from ournotes.runtime.storage import (
    EVIDENCE_PRUNED,
    SchedulerOutput,
    StoragePolicy,
    _remove,
    compress_log,
    inside,
    maintain,
    retention_plan,
)
from ournotes.runtime.store import RuntimeStore



def legacy_export(db, identifier):
    """Build a pre-cloud-only archive fixture to exercise legacy maintenance."""
    from ournotes.accounts.archive import write_archive
    from ournotes.runtime.retention import cleanup_success
    from ournotes.runtime.storage import compact_success
    report = db.export(identifier)
    archive = write_archive(db.root, report)
    account = db.account(identifier)
    account['archive'] = archive.relative_to(db.root).as_posix()
    db.save_account(account)
    if StoragePolicy.load(db.root).compact_completed:
        cleanup_success(db, report)
        compact_success(db, report, archive)
    return db.report(identifier)


def batch(db, number, *, status='complete', account_status='running'):
    run = db.create_run('都抽', 'test', 1)
    identifier = uuid.uuid4().hex
    run.update(batch_id=identifier, status=status)
    db.save_run(run)
    value = {'id': identifier, 'created_at': number, 'finished_at': number + 1,
             'status': status, 'lanes': [{'instance': number, 'run_id': run['id']}]}
    db.save_batch(value)
    account = db.reserve(run, [{'pulls': 1}])
    account.update(status=account_status, batch_id=identifier, instance=number)
    db.save_account(account)
    folder = db.root / 'state/runs' / run['id']
    folder.mkdir(parents=True)
    (folder / 'run.log').write_text('same message\n' * 100, 'utf-8')
    return value, run, account, folder


def test_only_latest_debug_batch_keeps_detail_reports(tmp_path):
    db = RuntimeStore(tmp_path)
    entries = [batch(db, number) for number in range(1, 4)]
    paths = []
    for index, (value, run, _, folder) in enumerate(entries):
        value['debug_mode'] = index < 2
        db.save_batch(value)
        target = tmp_path / 'results/batches' / value['id'] / 'performance.json.gz'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'report')
        (folder / 'events.jsonl').write_text('{}\n')
        paths.append(target)
    plan = retention_plan(tmp_path, db.db, StoragePolicy())
    deleted = {tmp_path / p for p in plan['delete']}
    assert paths[0] in deleted and paths[2] in deleted and paths[1] not in deleted
    assert entries[0][3] / 'events.jsonl' in deleted
    assert entries[1][3] / 'events.jsonl' not in deleted
    db.close()


def test_catalog_cleanup_preserves_current_and_account_pins(tmp_path):
    db = RuntimeStore(tmp_path)
    _, _, account, _ = batch(db, 1)
    catalog = tmp_path / 'data/catalog'
    folders = [catalog / 'snapshots' / (letter * 32) for letter in ('a', 'b', 'c')]
    for folder in folders:
        atomic_json(folder / 'catalog.json', {'cards': []})
        atomic_json(folder / 'pools.json', {'assets': []})
    atomic_json(catalog / 'current.json', {'directory': str(folders[0].relative_to(catalog)),
                'sha256': {name: file_hash(folders[0] / name) for name in ('catalog.json', 'pools.json')}})
    account['catalog_snapshot'] = folders[1].relative_to(tmp_path).as_posix()
    db.save_account(account)
    plan = retention_plan(tmp_path, db.db, StoragePolicy())
    deleted = {tmp_path / p for p in plan['delete']}
    assert folders[2] / 'catalog.json' in deleted
    assert all(folder / 'catalog.json' not in deleted for folder in folders[:2])
    db.close()


def test_retention_keeps_three_and_prunes_old_unfinished_without_resetting_state(tmp_path):
    db = RuntimeStore(tmp_path)
    entries = [batch(db, number) for number in range(1, 5)]
    old_batch, old_run, account, folder = entries[0]
    evidence = folder / 'screens/original.png'
    evidence.parent.mkdir()
    evidence.write_bytes(b'original evidence')
    db.save_draw({'id': 'unresolved', 'account_id': account['id'], 'ordinal': 0,
                  'status': 'submitted', 'pulls': 1, 'screenshot': {'path': str(evidence.relative_to(tmp_path))}})
    counters, before = db.counters(), db.draws(account['id'])
    plan = maintain(tmp_path, dry_run=True)
    assert plan['keep_batches'] == sorted(value['id'] for value, _, _, _ in entries[1:])
    assert old_run['id'] in plan['expired_runs'] and evidence.exists()
    report = maintain(tmp_path)
    assert report['status'] == 'complete' and not evidence.exists()
    assert db.counters() == counters and db.draws(account['id']) == before
    assert db.account(account['id'])['status'] == 'running'
    assert db.account(account['id'])['evidence_pruned_at']
    capability = lane_recovery(old_batch, db.get_run(old_run['id']), db.account(account['id']), before)
    assert capability['retry']['reason'] == EVIDENCE_PRUNED
    for _, _, _, recent in entries[1:]:
        assert (recent / 'run.log.gz').is_file() and not (recent / 'run.log').exists()
    again = maintain(tmp_path)
    assert again['removed_bytes'] == again['compression_saved_bytes'] == 0
    db.close()


def test_expired_incomplete_account_drops_detail_but_keeps_transaction_identity(tmp_path):
    db = RuntimeStore(tmp_path)
    entries = [batch(db, n) for n in range(4)]
    _, _, account, _ = entries[0]
    account['context'] = {'large': 'x' * 1000}
    db.save_account(account)
    db.action(account['id'], {'label': 'old action', 'phase': 'draws'})
    db.save_draw({'id': 'uncertain', 'account_id': account['id'], 'ordinal': 0, 'status': 'needs_review',
                  'result': {'slots': [{'unknown': True}]}, 'screenshot': {'sha256': 'original-hash'}})
    detail = tmp_path / 'results/accounts' / (account['id'] + '.json')
    atomic_json(detail, account)
    before = db.counters()
    maintain(tmp_path)
    draw = db.draws(account['id'])[0]
    assert draw['id'] == 'uncertain' and draw['status'] == 'needs_review'
    assert draw['screenshot']['sha256'] == 'original-hash' and 'result' not in draw
    assert db.account(account['id'])['evidence_pruned_at'] and 'context' not in db.account(account['id'])
    assert db.db.execute('SELECT count(*) FROM actions WHERE account_id=?', (account['id'],)).fetchone()[0] == 0
    assert not detail.exists() and db.counters() == before
    db.close()


def test_success_keeps_archive_versions_stats_and_transaction_audit(tmp_path):
    db = RuntimeStore(tmp_path)
    _, run, account, folder = batch(db, 1, account_status='complete')
    account.update(active_seconds=12.5, pull_count=1, context={'large': 'x' * 1000})
    db.save_account(account)
    card = {'key': 'member:1', 'id': 1, 'kind': 'member', 'rarity': 'SSR', 'title': 'a', 'name': 'b'}
    db.save_draw({'id': 'draw', 'account_id': account['id'], 'ordinal': 0, 'status': 'complete', 'pulls': 1,
                  'result': {'slots': [{'status': 'matched', 'card': card}]}, 'reviews': [{'reason': 'verified'}]})
    db.action(account['id'], {'label': 'action', 'phase': 'draws'})
    cache = folder / 'recognition/draw.json'
    atomic_json(cache, {'draw_id': 'draw', 'result': {'large': 'x' * 1000}})
    report = legacy_export(db, account['id'])
    archive = tmp_path / report['archive']
    data = json.loads(archive.read_text('utf-8'))
    atomic_json(archive, {**data, 'used': True, 'listed': True, 'cloud_version': 5})
    fingerprint = file_hash(archive)
    result = db.report(account['id'])
    assert result['used'] and result['listed'] and result['cloud_version'] == 5
    assert result['active_seconds'] == 12.5 and result['planned_pull_count'] == 1
    assert result['summary'][0]['count'] == 1 and not cache.exists()
    saved = db.draws(account['id'])[0]
    assert saved['reviews'] == [{'reason': 'verified'}] and 'result' not in saved
    assert 'context' not in db.account(account['id'])
    assert db.db.execute('SELECT count(*) FROM actions WHERE account_id=?', (account['id'],)).fetchone()[0] == 0
    maintain(tmp_path)
    assert file_hash(archive) == fingerprint
    assert db.get_run(run['id'])['status'] == 'complete'
    db.close()


def test_missing_success_archive_protects_old_run_and_does_not_recreate_archive(tmp_path):
    db = RuntimeStore(tmp_path)
    _, run, account, folder = batch(db, 1, account_status='complete')
    for number in range(2, 5):
        batch(db, number)
    evidence = folder / 'screens/evidence.png'
    evidence.parent.mkdir()
    evidence.write_bytes(b'keep')
    report = maintain(tmp_path)
    assert run['id'] in report['protected_runs'] and evidence.exists()
    assert report['skipped_accounts'][0]['account_id'] == account['id']
    assert not list((tmp_path / 'results/archive/accounts').glob('*.json'))
    db.close()


def test_existing_valid_archive_can_compact_when_duplicate_detail_is_already_missing(tmp_path):
    atomic_json(tmp_path / 'config/storage-policy.json', {**asdict(StoragePolicy()), 'compact_completed': False})
    db = RuntimeStore(tmp_path)
    _, _, account, _ = batch(db, 1, account_status='complete')
    card = {'key': 'member:1', 'id': 1, 'kind': 'member', 'rarity': 'SSR', 'title': 'a', 'name': 'b'}
    db.save_draw({'id': 'draw', 'account_id': account['id'], 'ordinal': 0, 'status': 'complete', 'pulls': 1,
                  'result': {'slots': [{'status': 'matched', 'card': card}]}})
    legacy_export(db, account['id'])
    archive = next((tmp_path / 'results/archive/accounts').glob('*.json'))
    before = file_hash(archive)
    (tmp_path / 'results/accounts' / (account['id'] + '.json')).unlink()
    atomic_json(tmp_path / 'config/storage-policy.json', asdict(StoragePolicy()))
    report = maintain(tmp_path)
    assert report['compacted_accounts'] == 1 and not report['skipped_accounts']
    assert db.account(account['id'])['storage_compacted'] == 1 and file_hash(archive) == before
    db.close()


def test_interrupted_file_removal_is_retried_after_metadata_was_compacted(tmp_path, monkeypatch):
    atomic_json(tmp_path / 'config/storage-policy.json', {**asdict(StoragePolicy()), 'compact_completed': False})
    db = RuntimeStore(tmp_path)
    _, _, account, folder = batch(db, 1, account_status='complete')
    card = {'key': 'member:1', 'id': 1, 'kind': 'member', 'rarity': 'SSR', 'title': 'a', 'name': 'b'}
    draw = {'id': 'draw', 'account_id': account['id'], 'ordinal': 0, 'status': 'complete', 'pulls': 1,
            'result': {'slots': [{'status': 'matched', 'card': card}]}}
    db.save_draw(draw)
    legacy_export(db, account['id'])
    image = folder / 'screens/success.png'
    image.parent.mkdir()
    image.write_bytes(b'pixels')
    draw['screenshot'] = {'path': image.relative_to(tmp_path).as_posix(), 'sha256': file_hash(image)}
    db.save_draw(draw)
    atomic_json(tmp_path / 'config/storage-policy.json', asdict(StoragePolicy()))
    with monkeypatch.context() as patch:
        patch.setattr('ournotes.runtime.storage._remove', lambda *a, **k: (_ for _ in ()).throw(OSError('interrupted')))
        with pytest.raises(OSError):
            maintain(tmp_path)
    assert db.account(account['id'])['storage_compacted'] == 1 and image.is_file()
    maintain(tmp_path)
    assert not image.exists() and db.draws(account['id'])[0]['status'] == 'complete'
    db.close()


def test_repeated_maintenance_does_not_rewrite_compact_batch_index(tmp_path, monkeypatch):
    db = RuntimeStore(tmp_path)
    value, _, account, _ = batch(db, 1, account_status='complete')
    card = {'key': 'member:1', 'id': 1, 'kind': 'member', 'rarity': 'SSR', 'title': 'a', 'name': 'b'}
    db.save_draw({'id': 'draw', 'account_id': account['id'], 'ordinal': 0, 'status': 'complete', 'pulls': 1,
                  'result': {'slots': [{'status': 'matched', 'card': card}]}})
    legacy_export(db, account['id'])
    path = tmp_path / 'results/batches' / value['id'] / 'results.json'
    atomic_json(path, {'accounts': [db.account(account['id'])], 'summary': ['obsolete']})
    maintain(tmp_path)
    assert 'summary' not in json.loads(path.read_text('utf-8'))
    original = atomic_json

    def write(destination, *args, **kwargs):
        assert destination != path, 'Unchanged batch index must not be rewritten'
        return original(destination, *args, **kwargs)

    monkeypatch.setattr('ournotes.runtime.storage.atomic_json', write)
    maintain(tmp_path)
    db.close()


def test_archive_cache_is_scoped_to_one_pass_and_does_not_hide_damage(tmp_path):
    db = RuntimeStore(tmp_path)
    _, run, account, folder = batch(db, 1, account_status='complete')
    card = {'key': 'member:1', 'id': 1, 'kind': 'member', 'rarity': 'SSR', 'title': 'a', 'name': 'b'}
    db.save_draw({'id': 'draw', 'account_id': account['id'], 'ordinal': 0, 'status': 'complete', 'pulls': 1,
                  'result': {'slots': [{'status': 'matched', 'card': card}]}})
    report = legacy_export(db, account['id'])
    maintain(tmp_path)
    (tmp_path / report['archive']).write_text('{}', 'utf-8')
    for n in range(2, 5):
        batch(db, n)
    evidence = folder / 'retained.png'
    evidence.write_bytes(b'keep')
    result = maintain(tmp_path)
    assert run['id'] in result['protected_runs'] and evidence.exists()
    assert any(a['account_id'] == account['id'] for a in result['skipped_accounts'])
    db.close()


def test_missing_caches_do_not_run_expensive_path_validation(tmp_path, monkeypatch):
    from ournotes.runtime.storage import ExistingStore, _compact_existing
    db = RuntimeStore(tmp_path)
    _, run, account, _ = batch(db, 1, account_status='complete')
    card = {'key': 'member:1', 'id': 1, 'kind': 'member', 'rarity': 'SSR', 'title': 'a', 'name': 'b'}
    db.save_draw({'id': 'draw', 'account_id': account['id'], 'ordinal': 0, 'status': 'complete', 'pulls': 1,
                  'result': {'slots': [{'status': 'matched', 'card': card}]}})
    legacy_export(db, account['id'])
    original = inside

    def validate(root, path, boundary):
        assert '/recognition/' not in path.as_posix()
        assert '/results/accounts/' not in path.as_posix()
        return original(root, path, boundary)

    monkeypatch.setattr('ournotes.runtime.storage.inside', validate)
    store = ExistingStore(tmp_path, db.db)
    store.artifact_remover = lambda paths: None
    assert _compact_existing(store, {run['id']: run}, lambda **_: None) == (0, [])
    db.close()


@pytest.mark.parametrize('reason', ['duplicate_index', 'unknown_card'])
def test_shared_archive_exclusions_are_not_compacted(tmp_path, reason):
    atomic_json(tmp_path / 'config/storage-policy.json', {**asdict(StoragePolicy()), 'compact_completed': False})
    db = RuntimeStore(tmp_path)
    _, _, account, _ = batch(db, 1, account_status='complete')
    card = {'key': 'member:1', 'id': 1, 'kind': 'member', 'rarity': 'SSR', 'title': 'a', 'name': 'b'}
    db.save_draw({'id': 'draw', 'account_id': account['id'], 'ordinal': 0, 'status': 'complete', 'pulls': 1,
                  'result': {'slots': [{'status': 'matched', 'card': card}]}})
    legacy_export(db, account['id'])
    archive = json.loads(next((tmp_path / 'results/archive/accounts').glob('*.json')).read_text('utf-8'))
    if reason == 'duplicate_index':
        atomic_json(tmp_path / 'results/archive/accounts/duplicate.json', {**archive, 'account_id': 'other'})
    else:
        atomic_json(tmp_path / 'data/catalog/site/catalog.json', {'cards': []})
    atomic_json(tmp_path / 'config/storage-policy.json', asdict(StoragePolicy()))
    result = maintain(tmp_path)
    assert result['compacted_accounts'] == 0 and result['skipped_accounts']
    assert not db.account(account['id']).get('storage_compacted')
    db.close()


def test_archive_directory_validation_is_reused_but_each_file_rejects_reparse_points(tmp_path, monkeypatch):
    import stat
    from pathlib import Path
    from types import SimpleNamespace

    from ournotes.runtime.storage import _read_archive
    folder = tmp_path / 'results/archive/accounts'
    first, linked = folder / 'first.json', folder / 'linked.json'
    atomic_json(first, {'id': 'first'})
    atomic_json(linked, {'id': 'must-not-read'})
    original, calls = inside, []

    def validate(*args):
        calls.append(args[1])
        return original(*args)

    monkeypatch.setattr('ournotes.runtime.storage.inside', validate)
    cache = {}
    assert _read_archive(tmp_path, first, cache) == {'id': 'first'}
    lstat = Path.lstat

    def attributes(path):
        if path == linked:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return lstat(path)

    monkeypatch.setattr(Path, 'lstat', attributes)
    with pytest.raises(ValueError, match='junction'):
        _read_archive(tmp_path, linked, cache)
    assert calls == [folder]
    assert linked not in cache


def test_archive_cache_never_allows_paths_outside_archive_directory(tmp_path):
    from ournotes.runtime.storage import _read_archive
    allowed = tmp_path / 'results/archive/accounts/first.json'
    outside = tmp_path / 'outside.json'
    atomic_json(allowed, {'id': 'first'})
    atomic_json(outside, {'id': 'outside'})
    cache = {}
    _read_archive(tmp_path, allowed, cache)
    with pytest.raises(ValueError, match='越界'):
        _read_archive(tmp_path, outside, cache)


def test_transfer_protects_source_evidence(tmp_path):
    db = RuntimeStore(tmp_path)
    _, old, _, folder = batch(db, 1)
    recent = [batch(db, i) for i in range(2, 5)]
    account = recent[-1][2]
    account['batch_transfers'] = [{'from_run': old['id']}]
    db.save_account(account)
    plan = retention_plan(tmp_path, db.db, StoragePolicy())
    assert old['id'] in plan['protected_runs'] and not plan['delete']
    assert folder.exists()
    db.close()


def test_active_lock_prevents_maintenance(tmp_path):
    db = RuntimeStore(tmp_path)
    batch(db, 1)
    with SecretStore(tmp_path / 'state').lock('runner'), pytest.raises(RuntimeError):
        maintain(tmp_path)
    db.close()


def test_gzip_verifies_content_and_keeps_source_on_validation_failure(tmp_path, monkeypatch):
    path = tmp_path / 'events.jsonl'
    original = b'{"event":"hello"}\n' * 1000
    path.write_bytes(original)
    saved = compress_log(path)
    assert saved > 0 and not path.exists()
    assert gzip.decompress(path.with_name(path.name + '.gz').read_bytes()) == original
    assert compress_log(path) == 0
    other = tmp_path / 'other.log'
    other.write_bytes(original)
    monkeypatch.setattr('ournotes.runtime.storage.atomic_json', lambda *a, **k: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        compress_log(other)
    assert other.read_bytes() == original and not list(tmp_path.glob('*.tmp'))


def test_job_tail_works_after_compression_without_decompressing_archive(tmp_path, monkeypatch):
    identifier = 'a' * 32
    folder = tmp_path / 'state/webui/jobs'
    atomic_json(folder / (identifier + '.json'), {'id': identifier, 'action': 'start',
                'status': 'success', 'finished_at': 1, 'resources': []})
    log = folder / (identifier + '.log')
    log.write_text('old\n' * 10000 + 'tail marker\n', 'utf-8')
    compress_log(log)
    manager = JobManager(tmp_path)
    monkeypatch.setattr(gzip, 'open', lambda *a, **k: pytest.fail('UI must use bounded saved tail'))
    assert 'tail marker' in manager.detail(identifier)['log']


def test_failed_decompression_verification_keeps_original(tmp_path, monkeypatch):
    path = tmp_path / 'run.log'
    original = b'original\n' * 1000
    path.write_bytes(original)
    monkeypatch.setattr(gzip, 'open', lambda *a, **k: io.BytesIO(b'incorrect decompressed bytes'))
    with pytest.raises(ValueError, match='验证失败'):
        compress_log(path)
    assert path.read_bytes() == original and not path.with_name(path.name + '.gz').exists()


def test_unfinished_batch_is_protected_while_older_batches_can_be_cleaned(tmp_path):
    db = RuntimeStore(tmp_path)
    entries = [batch(db, n) for n in range(1, 5)]
    _, active_run, _, active = batch(db, 5, status='paused')
    report = maintain(tmp_path)
    assert active_run['id'] in report['protected_runs']
    assert (active / 'run.log').is_file() and not (entries[0][3] / 'run.log').exists()
    db.close()


def test_one_failed_compression_keeps_source_and_other_logs_still_finish(tmp_path, monkeypatch):
    db = RuntimeStore(tmp_path)
    _, _, _, folder = batch(db, 1)
    log = folder / 'events.jsonl'
    log.write_text('event\n' * 100, 'utf-8')
    original = compress_log

    def compress(path, level):
        if path == log:
            raise OSError('compression interrupted')
        return original(path, level)

    monkeypatch.setattr('ournotes.runtime.storage.compress_log', compress)
    report = maintain(tmp_path)
    assert report['status'] == 'complete' and log.is_file()
    assert report['compression_errors'][0]['reason'] == 'OSError'
    assert (folder / 'run.log.gz').is_file()
    db.close()


def test_policy_persists_and_output_only_emits_changes_and_heartbeat(tmp_path):
    policy = {**asdict(StoragePolicy()), 'keep_batches': 5}
    atomic_json(tmp_path / 'config/storage-policy.json', policy)
    assert StoragePolicy.load(tmp_path).keep_batches == 5
    ticks, emitted = [0], []
    output = SchedulerOutput(tmp_path, 'b' * 32, clock=lambda: ticks[0], emit=lambda text, **_: emitted.append(json.loads(text)))
    state = {'batch_id': 'batch', 'status': 'running', 'completed': 0, 'total_count': 3,
             'lanes': [{'instance': 1, 'status': 'running', 'completed': 0, 'alias': 'private@example.com'}]}
    output(state)
    ticks[0] = 10
    output(state)
    ticks[0] = 30
    output(state)
    assert len(emitted) == 2 and 'alias' not in emitted[0]['lanes'][0]
    assert json.loads((tmp_path / 'state/webui/jobs' / ('b' * 32 + '-runtime.json')).read_text('utf-8'))['batch_id'] == 'batch'


def test_path_traversal_is_rejected_before_deletion(tmp_path):
    with pytest.raises(ValueError):
        inside(tmp_path, tmp_path / 'state/runs/../../../../keep', tmp_path / 'state/runs')


def test_corrupt_job_receipt_is_protected_and_does_not_block_run_cleanup(tmp_path):
    db = RuntimeStore(tmp_path)
    entries = [batch(db, number) for number in range(1, 5)]
    folder = tmp_path / 'state/webui/jobs'
    folder.mkdir(parents=True)
    identifier = 'e' * 32
    (folder / (identifier + '.json')).write_text('', 'utf-8')
    log = folder / (identifier + '.log')
    log.write_text('preserve unknown writer', 'utf-8')
    result = maintain(tmp_path)
    assert result['skipped_jobs'] == [identifier] and log.is_file()
    assert not (entries[0][3] / 'run.log').exists()
    db.close()


@pytest.mark.skipif(os.name != 'nt', reason='Windows literal-path cleanup adapter')
def test_powershell_removes_each_explicit_file_and_rejects_escape_before_deleting(tmp_path):
    root = tmp_path / 'workspace with spaces'
    folder = root / 'state/runs' / ('d' * 32)
    folder.mkdir(parents=True)
    paths = []
    for name in ('a.log', '截图 [1].png', 'cache.json'):
        path = folder / name
        path.write_bytes(b'file')
        paths.append(path.relative_to(root).as_posix())
    assert _remove(root, paths, powershell=True) == paths
    assert not list(folder.iterdir())
    assert _remove(root, paths, powershell=True) == []
    safe = folder / 'keep.log'
    safe.write_bytes(b'keep')
    invalid = 'state/runs/' + 'd' * 32 + '/../../../../escape.log'
    with pytest.raises(subprocess.CalledProcessError):
        _remove(root, [safe.relative_to(root).as_posix(), invalid], powershell=True)
    assert safe.read_bytes() == b'keep'


@pytest.mark.parametrize('alive', [True, False])
def test_restarted_job_persists_end_only_when_writer_is_confirmed_gone(tmp_path, monkeypatch, alive):
    identifier = 'f' * 32
    path = tmp_path/'state/webui/jobs'/f'{identifier}.json'
    atomic_json(path, {'id': identifier, 'action': 'resume', 'status': 'running', 'resources': ['runner'], 'pid': 1234})
    before = path.read_bytes()
    monkeypatch.setattr('ournotes.control.launcher.process_alive', lambda pid: alive)
    manager = JobManager(tmp_path)
    assert manager.jobs[identifier]['status'] == 'needs_review'
    if alive:
        assert path.read_bytes() == before
    else:
        saved = json.loads(path.read_text('utf8'))
        assert saved['status'] == 'needs_review' and saved['finished_at']
