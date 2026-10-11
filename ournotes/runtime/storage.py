"""Bounded runtime artifacts; no devices, cloud requests or store initialization."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from ournotes.common.files import atomic_json, file_hash
from ournotes.common.secrets import SecretStore
from ournotes.paths import ROOT

TERMINAL = {'complete', 'cancelled'}
LOG_NAMES = ('events.jsonl', 'timings.jsonl', 'run.log')
EVIDENCE_PRUNED = '证据已按保留策略清理，无法恢复或复核；禁止重新提交原交易。'
ACCOUNT_FIELDS = ('id', 'alias', 'alias_index', 'run_id', 'batch_id', 'channel', 'status', 'phase',
                  'preset', 'instance', 'created_at', 'started_at', 'finished_at', 'active_seconds',
                  'used', 'used_updated_at', 'pull_count', 'node_id', 'recognition_source',
                  'batch_transfers', 'planned_pull_count')


@dataclass(frozen=True)
class StoragePolicy:
    schema_version: int = 1
    keep_batches: int = 3
    keep_jobs: int = 40
    compact_completed: bool = True
    remove_expired_unfinished: bool = True
    compress_logs: bool = True
    gzip_level: int = 6

    @classmethod
    def load(cls, root):
        path = Path(root) / 'config/storage-policy.json'
        if not path.exists():
            path = ROOT / 'config/storage-policy.json'
        value = json.loads(path.read_text('utf-8')) if path.exists() else asdict(cls())
        policy = cls(**value)
        if (type(policy.schema_version) is not int or policy.schema_version != 1
                or type(policy.keep_batches) is not int or not 1 <= policy.keep_batches <= 100
                or type(policy.keep_jobs) is not int or not 0 <= policy.keep_jobs <= 10000
                or type(policy.gzip_level) is not int or not 1 <= policy.gzip_level <= 9
                or any(type(getattr(policy, field)) is not bool for field in
                       ('compact_completed', 'remove_expired_unfinished', 'compress_logs'))):
            raise ValueError('存储保留配置无效，未执行清理。')
        return policy


def inside(root, path, boundary):
    """Check every existing ancestor for junctions as well as ordinary symlinks."""
    root, boundary, path = Path(root).resolve(), Path(boundary).absolute(), Path(path).absolute()
    if path == root or not path.is_relative_to(boundary) or not boundary.is_relative_to(root):
        raise ValueError('存储清理路径越界。')
    resolved = path.resolve()
    if resolved != path or not resolved.is_relative_to(boundary.resolve()):
        raise ValueError('存储清理拒绝路径穿越或链接。')
    current = path
    while current != root:
        if current.exists() or current.is_symlink():
            attributes = current.lstat()
            if current.is_symlink() or getattr(attributes, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError('存储清理拒绝符号链接或 junction。')
        current = current.parent
        if not current.is_relative_to(root):
            raise ValueError('存储清理路径越界。')
    return path


def tree_files(root, folder):
    folder = inside(root, folder, Path(root) / 'state/runs')
    if not folder.exists():
        return []
    files = []
    for base, directories, names in os.walk(folder, followlinks=False):
        for name in directories:
            inside(root, Path(base) / name, folder)
        files.extend(inside(root, Path(base) / name, folder) for name in names)
    return files


def log_tail(path):
    """Bounded memory even for an unbounded single output line."""
    with Path(path).open('rb') as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - 12000))
        return '\n'.join(stream.read().decode('utf-8', errors='replace').splitlines()[-55:])


def compress_log(path, level=6, *, tail_cache=True):
    path = Path(path)
    target = path.with_name(path.name + '.gz')
    if not path.is_file():
        return 0
    before = path.stat()
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    digest, length = hashlib.sha256(), 0
    tail = log_tail(path) if tail_cache else None
    try:
        with path.open('rb') as source, temporary.open('xb') as output:
            with gzip.GzipFile(fileobj=output, mode='wb', compresslevel=level, mtime=0, filename='') as archive:
                while block := source.read(1024 * 1024):
                    archive.write(block)
                    digest.update(block)
                    length += len(block)
            output.flush()
            os.fsync(output.fileno())
        verified, count = hashlib.sha256(), 0
        with gzip.open(temporary, 'rb') as source:
            while block := source.read(1024 * 1024):
                verified.update(block)
                count += len(block)
        after = path.stat()
        if (count != length or verified.digest() != digest.digest()
                or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
            raise ValueError('日志压缩验证失败或文件仍在写入，原文件保留。')
        if temporary.stat().st_size >= before.st_size:
            return 0  # Very small logs need no larger gzip container.
        if target.exists() and file_hash(target) != file_hash(temporary):
            raise ValueError('已有压缩日志与原文件冲突，原文件保留。')
        if tail_cache:
            atomic_json(path.with_name(path.name + '.tail.json'), {'tail': tail, 'sha256': digest.hexdigest()}, compact=True)
        os.replace(temporary, target)
        path.unlink()
        return before.st_size - target.stat().st_size
    finally:
        temporary.unlink(missing_ok=True)


def read_lines(path):
    path = Path(path)
    compressed = path.with_name(path.name + '.gz')
    if not path.is_file() and not compressed.is_file():
        return []
    with (path.open('rt', encoding='utf-8') if path.is_file() else
          gzip.open(compressed, 'rt', encoding='utf-8')) as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _read_archive(root, path, cache=None):
    path = Path(path)
    if cache is not None and path in cache:
        return cache[path]
    directory = Path(root) / 'results/archive/accounts'
    if cache is not None and path.parent == directory:
        # All these flat archive files share the same ancestors. Validate those
        # once under runner/account-used locks; still reject links on each file.
        directory_key = ('directory', directory)
        if directory_key not in cache:
            inside(root, directory, directory)
            cache[directory_key] = True
        attributes = path.lstat()
        if (stat.S_ISLNK(attributes.st_mode)
                or getattr(attributes, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('存储清理拒绝符号链接或 junction。')
    else:
        path = inside(root, path, directory)
    value = json.loads(path.read_text('utf-8'))
    if cache is not None:
        cache[path] = value
    return value


def validated_archive(root, account, path, report=None, *, cache=None):
    value = _read_archive(root, path, cache)
    if (value.get('schema_version') != 1 or value.get('status') != 'complete'
            or value.get('account_id') != account['id'] or value.get('alias_index') != account['alias_index']
            or value.get('alias') != account['alias'] or value.get('unknown_slots', 0)
            or type(value.get('used', False)) is not bool):
        raise ValueError('成功档案身份或状态不一致，不可精简。')
    counts = Counter()
    for item in value['cards']:
        card, count = item['card'], item['count']
        if (card['kind'] not in ('member', 'support') or card['rarity'] not in ('SSR', 'SR')
                or type(card['id']) is not int or card['id'] < 1 or type(count) is not int or count <= 0):
            raise ValueError('成功档案卡牌无效，不可精简。')
        counts[card['kind'], card['rarity']] += count
    if any(value['totals'][kind][rarity] != counts[kind, rarity]
           for kind in ('member', 'support') for rarity in ('SSR', 'SR')):
        raise ValueError('成功档案汇总不一致，不可精简。')
    if report is not None:
        from ournotes.accounts.archive import compact_account
        expected = compact_account(report)
        if any(value[key] != expected[key] for key in ('cards', 'totals', 'alias_index', 'alias')):
            raise ValueError('成功档案与运行成果不一致，不可精简。')
    return value


def compact_success(db, report, archive):
    """Commit a small index and transaction ledger only after validating the archive."""
    account = db.account(report['id'])
    validated_archive(db.root, account, archive, report, cache=getattr(db, 'archive_cache', None))
    if any(draw['status'] not in ('complete', 'skipped') for draw in report['draws']):
        raise ValueError('成功账号尚有未决交易，不可精简。')
    from ournotes.runtime.retention import evidence_folders
    folders = evidence_folders(db, report)
    caches = [inside(db.root, folder / 'recognition' / (draw['id'] + '.json'), folder)
              for folder in folders for draw in report['draws']]
    index = {key: account[key] for key in ACCOUNT_FIELDS if key in account}
    if account.get('plan'):
        index['planned_pull_count'] = sum(draw['pulls'] for draw in account['plan'])
    index.update(archive=Path(archive).relative_to(db.root).as_posix(), storage_compacted=1)
    with db.transaction():
        db.save_account(index)
        for draw in report['draws']:
            ledger = {key: value for key, value in draw.items()
                      if isinstance(value, (str, int, float, bool)) or value is None or key == 'reviews'}
            for key in ('screenshot', 'before'):
                if key in draw:
                    ledger[key] = {name: draw[key][name] for name in ('path', 'sha256', 'storage') if name in draw[key]}
            ledger['storage_compacted'] = 1
            db.save_draw(ledger)
        db.db.execute('DELETE FROM actions WHERE account_id=?', (report['id'],))
        db.db.execute('UPDATE accounts SET export_pending=0 WHERE id=?', (report['id'],))
    files = [path for path in caches if path.is_file()]
    detail = inside(db.root, db.root / 'results/accounts' / (report['id'] + '.json'),
                    db.root / 'results/accounts')
    if detail.is_file():
        files.append(detail)
    if remover := getattr(db, 'artifact_remover', None):
        remover(files)
    else:
        for path in files:
            path.unlink(missing_ok=True)


def snapshot(db):
    batches = {i: json.loads(data) for i, data in db.execute('SELECT id,data FROM batches')}
    runs = {i: json.loads(data) for i, data in db.execute('SELECT id,data FROM runs')}
    accounts = {i: json.loads(data) for i, data in db.execute('SELECT id,data FROM accounts')}
    return batches, runs, accounts


def retention_plan(root, db, policy, *, archive_cache=None):
    root = Path(root).resolve()
    batches, runs, accounts = snapshot(db)
    ended = sorted((b for b in batches.values() if b['status'] in TERMINAL),
                   key=lambda b: (b.get('finished_at') or b.get('created_at', 0), b['id']), reverse=True)
    keep = {b['id'] for b in ended[:policy.keep_batches]}
    protected = {i for i, r in runs.items() if r['status'] not in TERMINAL
                 or r.get('batch_id') not in batches or batches[r['batch_id']]['status'] not in TERMINAL}
    # Do not discard the last evidence of a supposedly successful account whose
    # permanent archive is missing/corrupt. Never reconstruct missing archives.
    archive_paths = {}
    for path in (root / 'results/archive/accounts').glob('*.json'):
        match = re.fullmatch(r'(\d+) \d+ SSR\.json', path.name)
        if match:
            archive_paths.setdefault(int(match[1]), []).append(path)
    for account in accounts.values():
        if account['status'] != 'complete':
            continue
        paths = ([root / account['archive']] if account.get('archive') else
                 archive_paths.get(account['alias_index'], []))
        try:
            if len(paths) != 1:
                raise ValueError('missing_or_duplicate_archive')
            validated_archive(root, account, paths[0], cache=archive_cache)
        except (OSError, ValueError, KeyError, TypeError):
            protected.add(account['run_id'])
    # A transferred account may still read an earlier run's images and caches.
    for account in accounts.values():
        owner = runs.get(account['run_id'], {})
        if owner.get('batch_id') in keep or account['run_id'] in protected:
            protected.update(t['from_run'] for t in account.get('batch_transfers', []) if t.get('from_run'))
    unfinished_runs = {a['run_id'] for a in accounts.values() if a['status'] != 'complete'}
    expired = []
    for identifier, run in runs.items():
        if run.get('batch_id') in keep or identifier in protected or run['status'] not in TERMINAL:
            continue
        if not policy.remove_expired_unfinished and identifier in unfinished_runs:
            continue
        if not re.fullmatch('[a-f0-9]{32}', identifier):
            continue
        expired.append(identifier)
    deletes, compress = [], []
    catalog_root = root / 'data/catalog'
    if (catalog_root / 'current.json').is_file() and all(b['status'] in TERMINAL for b in batches.values()):
        from ournotes.catalogs.runtime import current_catalog, saved_snapshot
        current = current_catalog(catalog_root)
        referenced = {current.resolve()}
        referenced.update(saved_snapshot(root, a['catalog_snapshot']) for a in accounts.values()
                          if a.get('catalog_snapshot'))
        for folder in (catalog_root / 'snapshots').iterdir():
            if not re.fullmatch('[a-f0-9]{32}', folder.name) or folder.resolve() in referenced:
                continue
            inside(root, folder, catalog_root / 'snapshots')
            for path in folder.rglob('*'):
                inside(root, path, folder)
                if path.is_file():
                    deletes.append(path)
    debug_batches = [b for b in batches.values() if b.get('debug_mode')]
    latest_debug = max(debug_batches, key=lambda b: b.get('created_at', 0), default={}).get('id')
    detail_names = ('performance.json', 'performance.json.gz', '完整分步骤用时.md')
    for batch in batches.values():
        if batch['id'] == latest_debug or batch['status'] not in TERMINAL:
            continue
        folder = root / 'results/batches' / batch['id']
        for path in [*(folder / name for name in detail_names), *folder.glob('实例*-完整步骤.csv')]:
            if path.is_file():
                deletes.append(inside(root, path, root / 'results/batches'))
        for lane in batch.get('lanes', []):
            if lane['run_id'] in protected:
                continue
            folder = root / 'state/runs' / lane['run_id']
            detail_logs = ['timings.jsonl', 'timings.jsonl.gz', 'timings.jsonl.tail.json']
            if batch.get('debug_mode'):
                detail_logs += ['events.jsonl', 'events.jsonl.gz', 'events.jsonl.tail.json']
            for name in detail_logs:
                path = folder / name
                if path.is_file():
                    deletes.append(inside(root, path, root / 'state/runs'))
    for identifier in expired:
        deletes.extend(tree_files(root, root / 'state/runs' / identifier))
        for account in accounts.values():
            if account['run_id'] == identifier and account['status'] != 'complete':
                detail = root / 'results/accounts' / (account['id'] + '.json')
                if detail.is_file():
                    deletes.append(inside(root, detail, root / 'results/accounts'))
    if policy.compress_logs:
        for identifier, run in runs.items():
            if run.get('batch_id') in keep and identifier not in protected:
                folder = root / 'state/runs' / identifier
                for name in LOG_NAMES:
                    path = inside(root, folder / name, root / 'state/runs')
                    if path.is_file() and path not in deletes:
                        compress.append(path)
    job_folder = root / 'state/webui/jobs'
    jobs, skipped_jobs = [], []
    for path in job_folder.glob('*.json'):
        if not re.fullmatch('[a-f0-9]{32}', path.stem):
            continue
        inside(root, path, job_folder)
        try:
            value = json.loads(path.read_text('utf-8'))
            if not isinstance(value, dict):
                raise TypeError('invalid_job_receipt')
            if (value.get('id') == path.stem and type(value.get('finished_at')) in (int, float)
                    and value.get('status') in ('success', 'paused', 'failed', 'needs_review')):
                binding = job_folder / (path.stem + '-runtime.json')
                if binding.is_file():
                    inside(root, binding, job_folder)
                    value['batch_id'] = json.loads(binding.read_text('utf-8'))['batch_id']
                    if not isinstance(value['batch_id'], str):
                        raise ValueError('invalid_job_binding')
                jobs.append(value)
        except (OSError, ValueError, KeyError, TypeError):
            skipped_jobs.append(path.stem)
    jobs.sort(key=lambda job: job['finished_at'], reverse=True)
    unbound_keep = {job['id'] for job in jobs if not job.get('batch_id')}
    unbound_keep = {job['id'] for job in [j for j in jobs if j['id'] in unbound_keep][:policy.keep_jobs]}
    for job in jobs:
        keep_job = (job.get('batch_id') in keep or job['id'] in unbound_keep
                    or job.get('batch_id') and job['batch_id'] not in {b['id'] for b in ended})
        log = job_folder / (job['id'] + '.log')
        if keep_job:
            if policy.compress_logs and job.get('log_policy') != 'session' and log.is_file():
                compress.append(inside(root, log, job_folder))
        else:
            deletes.extend(inside(root, p, job_folder) for p in
                           (log, log.with_name(log.name + '.gz'), log.with_name(log.name + '.tail.json')) if p.is_file())
    return {'keep_batches': sorted(keep), 'protected_runs': sorted(protected), 'expired_runs': expired,
            'skipped_jobs': skipped_jobs,
            'delete': [str(path.relative_to(root)) for path in sorted(set(deletes))],
            'compress': [str(path.relative_to(root)) for path in sorted(set(compress))],
            'delete_bytes': sum(path.stat().st_size for path in set(deletes)), 'policy': asdict(policy)}


def _mark_expired(db, identifiers, stamp):
    for identifier in identifiers:
        run = db.get_run(identifier)
        if not run.get('storage_retention', {}).get('pruned_at'):
            run['storage_retention'] = {'pruned_at': stamp, 'reason': 'outside_recent_batches'}
            db.save_run(run)
        for raw, in db.db.execute('SELECT data FROM accounts WHERE run_id=?', (identifier,)).fetchall():
            account = json.loads(raw)
            if account['status'] != 'complete':
                account.setdefault('evidence_pruned_at', stamp)
                account.pop('context', None)
                # Do not enqueue/re-export an incomplete account just to record retention.
                db.db.execute('UPDATE accounts SET data=?,export_pending=0 WHERE id=?', (json.dumps(account), account['id']))
                db.db.execute('DELETE FROM actions WHERE account_id=?', (account['id'],))
                for draw_id, raw_draw in db.db.execute('SELECT id,data FROM draws WHERE account_id=?', (account['id'],)).fetchall():
                    draw = json.loads(raw_draw)
                    for key in ('result', 'slots', 'candidates', 'recognition'):
                        draw.pop(key, None)
                    db.db.execute('UPDATE draws SET data=? WHERE id=?', (json.dumps(draw), draw_id))


class ExistingStore:
    """Maintenance adapter over an existing database, with no schema or export repair."""
    def __init__(self, root, connection):
        self.root, self.db = Path(root).resolve(), connection
        # Only reuse reads during this maintenance pass, while writers are excluded.
        self.archive_cache = {}
        self.retention_incidents = {}
        self.retention_files = {}
        self.retention_folders = {}

    def account(self, identifier):
        row = self.db.execute('SELECT data FROM accounts WHERE id=?', (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def get_run(self, identifier):
        row = self.db.execute('SELECT data FROM runs WHERE id=?', (identifier,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_run(self, run):
        self.db.execute('UPDATE runs SET data=? WHERE id=?', (json.dumps(run), run['id']))

    def save_account(self, account):
        self.db.execute('UPDATE accounts SET data=?,export_pending=0 WHERE id=?', (json.dumps(account), account['id']))

    def save_draw(self, draw):
        self.db.execute('UPDATE draws SET data=? WHERE id=?', (json.dumps(draw), draw['id']))

    def transaction(self):
        from contextlib import contextmanager
        @contextmanager
        def transaction():
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
        return transaction()


def _compact_existing(store, runs, progress):
    from ournotes.runtime.retention import cleanup_success, existing_file
    index = None
    if (store.root / 'data/catalog/site/catalog.json').is_file():
        from ournotes.accounts.index import AccountIndex
        index = AccountIndex(store.root / 'results/archive/accounts', store.root / 'data/catalog/site')
    archives, indices = {}, Counter()

    def read(path):
        try:
            return path, _read_archive(store.root, path, store.archive_cache)
        except (OSError, ValueError):
            return path, None

    # Cold Windows file opens dominate startup. Bound I/O concurrency; validation,
    # SQLite changes and deletion remain on the owning thread under its locks.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix='storage-archive') as readers:
        for path, value in readers.map(read, (store.root / 'results/archive/accounts').glob('*.json')):
            try:
                from ournotes.accounts.index import integer
                indices[integer(value['alias_index'])] += 1
                archives.setdefault(value.get('account_id'), []).append(path)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    count, rejected = 0, []
    rows = store.db.execute("SELECT id,data FROM accounts WHERE status='complete'").fetchall()
    for identifier, raw in rows:
        account = json.loads(raw)
        run = runs.get(account['run_id'], {})
        if run.get('status') not in TERMINAL:
            continue
        paths = archives.get(identifier, [])
        if len(paths) != 1:
            rejected.append({'account_id': identifier, 'reason': 'missing_or_duplicate_archive'})
            continue
        try:
            archive = paths[0]
            value = validated_archive(store.root, account, archive, cache=store.archive_cache)
            if indices[account['alias_index']] != 1:
                raise ValueError('archive_excluded_by_shared_validation')
            if index is not None:
                index.account(value)  # Shared card/schema validation, without pricing/rendering the whole site.
            draws = [json.loads(data) for data, in store.db.execute(
                'SELECT data FROM draws WHERE account_id=? ORDER BY ordinal', (identifier,))]
            if account.get('storage_compacted'):
                from ournotes.runtime.retention import evidence_folders
                report = dict(account, draws=draws, unknown_slots=0)
                cleanup_success(store, report)
                files = [inside(store.root, path, folder)
                         for folder in evidence_folders(store, report) for d in draws
                         if existing_file(store, path := folder / 'recognition' / (d['id'] + '.json'), folder)]
                detail = store.root / 'results/accounts' / (identifier + '.json')
                if existing_file(store, detail, detail.parent):
                    files.append(inside(store.root, detail, store.root / 'results/accounts'))
                store.artifact_remover(files)
                continue
            detail = inside(store.root, store.root / 'results/accounts' / (identifier + '.json'),
                            store.root / 'results/accounts')
            if account.get('archive') or not detail.is_file():
                report = dict(account, draws=draws, unknown_slots=0)
                report.update(totals=value['totals'], summary=[dict(c['card'], count=c['count']) for c in value['cards']])
            else:
                report = json.loads(detail.read_text('utf-8'))
                if report.get('id') != identifier or report.get('status') != 'complete':
                    raise ValueError('inconsistent_report')
                report['draws'] = draws
            validated_archive(store.root, account, archive, report, cache=store.archive_cache)
            cleanup_success(store, report)
            compact_success(store, report, archive)
            count += 1
            if count % 100 == 0:
                progress(stage='compact', completed=count)
        except (OSError, ValueError, KeyError, TypeError) as error:
            rejected.append({'account_id': identifier, 'reason': type(error).__name__})
    return count, rejected


def _remove(root, paths, powershell=False):
    if not paths:
        return []
    if powershell and os.name == 'nt':
        result = subprocess.run([shutil.which('pwsh.exe') or 'powershell.exe', '-NoProfile', '-NonInteractive', '-File',
                                 str(ROOT / 'scripts/remove_storage_files.ps1'), '-Root', str(root)],
                                input=json.dumps(paths), capture_output=True, text=True, encoding='utf-8', check=True)
        return json.loads(result.stdout)['removed']
    removed = []
    for relative in paths:
        category = relative.replace('\\', '/').split('/')[0]
        if category not in ('results', 'state', 'data'):
            raise ValueError('Unexpected cleanup category')
        boundary = Path(root) / category
        path = inside(root, Path(root) / relative, boundary)
        if path.is_file():
            path.unlink()
            removed.append(relative)
    return removed


def _rewrite_batch_indexes(store, batches):
    # alias_index is not indexed in older databases. Do one read instead of a
    # full table scan for every account in every batch export.
    accounts, duplicates = {}, set()
    for index, raw in store.db.execute('SELECT alias_index,data FROM accounts'):
        if index in accounts:
            duplicates.add(index)
        accounts[index] = json.loads(raw)
    for identifier in batches:
        path = store.root / 'results/batches' / identifier / 'results.json'
        if not path.is_file():
            continue
        value = json.loads(path.read_text('utf-8'))
        changed = False
        for position, account in enumerate(value.get('accounts', [])):
            index = account.get('alias_index')
            saved = accounts.get(index) if index not in duplicates else None
            if saved and saved.get('storage_compacted') and account.get('status') == 'complete':
                replacement = {key: saved[key] for key in ACCOUNT_FIELDS + ('archive',) if key in saved}
                if account != replacement:
                    value['accounts'][position] = replacement
                    changed = True
            elif saved and saved.get('evidence_pruned_at'):
                replacement = {key: saved[key] for key in ACCOUNT_FIELDS + ('evidence_pruned_at',) if key in saved}
                if account != replacement:
                    value['accounts'][position] = replacement
                    changed = True
        if value.get('accounts') and all(a.get('archive') for a in value['accounts']) and 'summary' in value:
            value.pop('summary')
            changed = True
        if changed:
            atomic_json(path, value, compact=True)


def maintain(root, *, connection=None, dry_run=False, powershell=False, progress=None):
    root = Path(root).resolve()
    policy = StoragePolicy.load(root)
    progress = progress or (lambda **_: None)
    path = root / 'state/runtime.sqlite3'
    if not path.is_file():
        return {'status': 'no_database', 'policy': asdict(policy)}
    if dry_run:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            return retention_plan(root, db, policy)
    locks = SecretStore(root / 'state')
    # External maintenance takes the same lock as a controller. A controller calls
    # only after all workers have exited, while it still owns its runner lock.
    from contextlib import nullcontext
    catalog_root = root / 'data/catalog'
    catalog_lock = SecretStore(catalog_root).lock('sync') if catalog_root.is_dir() else nullcontext()
    with locks.lock('storage-maintenance'), (nullcontext() if connection is not None else locks.lock('runner')), catalog_lock, (nullcontext(connection) if connection is not None else closing(sqlite3.connect(path, isolation_level=None, timeout=30))) as db:
        store = ExistingStore(root, db)
        batches, runs, _ = snapshot(db)
        db.execute('CREATE INDEX IF NOT EXISTS actions_account_id ON actions(account_id)')
        db.execute('CREATE INDEX IF NOT EXISTS accounts_run_id ON accounts(run_id)')
        config = root / 'config/storage-policy.json'
        if not config.exists():
            atomic_json(config, asdict(policy))
        report = {'status': 'running', 'started_at': time.time(), 'policy': asdict(policy)}
        target = root / 'state/storage/latest.json'
        atomic_json(target, report)
        successful_files = set()
        store.artifact_remover = lambda paths: successful_files.update(Path(p) for p in paths if Path(p).is_file())
        with locks.lock('account-used', wait=True):
            count, rejected = _compact_existing(store, runs, progress) if policy.compact_completed else (0, [])
            _rewrite_batch_indexes(store, batches)
            plan = retention_plan(root, db, policy, archive_cache=store.archive_cache)
        merged = {root / relative for relative in plan['delete']} | successful_files
        plan['delete'] = [str(p.relative_to(root)).replace('\\', '/') for p in sorted(merged)]
        plan['delete_bytes'] = sum(p.stat().st_size for p in merged if p.is_file())
        report.update(plan, compacted_accounts=count, skipped_accounts=rejected)
        atomic_json(target, report)
        progress(stage='delete', files=len(plan['delete']))
        removed = _remove(root, plan['delete'], powershell=powershell)
        # Persist the evidence-loss marker without changing account/draw states.
        with store.transaction():
            _mark_expired(store, plan['expired_runs'], time.time())
        _rewrite_batch_indexes(store, batches)
        saved, compression_errors = 0, []
        for number, relative in enumerate(plan['compress'], 1):
            path = inside(root, root / relative, root / 'state')
            try:
                saved += compress_log(path, policy.gzip_level)
            except (OSError, ValueError, EOFError) as error:
                compression_errors.append({'path': relative, 'reason': type(error).__name__})
            progress(stage='compress', completed=number, total=len(plan['compress']))
        report.update(status='complete', finished_at=time.time(), deleted_files=len(removed),
                      compression_saved_bytes=saved, compression_errors=compression_errors,
                      removed_bytes=plan['delete_bytes'])
        atomic_json(target, report)
        return report


class SchedulerOutput:
    """Keep the default CLI protocol; launcher jobs receive compact, safe progress."""
    def __init__(self, root, job_id=None, clock=time.monotonic, emit=print):
        self.root, self.job_id, self.clock, self.emit = Path(root), job_id, clock, emit
        self.previous, self.last_at, self.bound = None, float('-inf'), False
        if job_id and not re.fullmatch('[a-f0-9]{32}', job_id):
            raise ValueError('启动任务标识无效。')

    def __call__(self, value):
        if not self.job_id:
            self.emit(json.dumps(value, ensure_ascii=False), flush=True)
            return
        if not self.bound:
            atomic_json(self.root / 'state/webui/jobs' / (self.job_id + '-runtime.json'),
                        {'batch_id': value['batch_id'], 'pid': os.getpid(), 'started_at': time.time()}, compact=True)
            self.bound = True
        lanes = [{key: lane[key] for key in ('instance', 'status', 'completed', 'phase',
                  'transactions', 'waiting_for', 'message') if key in lane} for lane in value['lanes']]
        for lane in lanes:
            if 'message' in lane:
                from ournotes.control.launcher import redact
                lane['message'] = redact(lane['message'])
        brief = {key: value[key] for key in ('batch_id', 'status', 'completed', 'total_count') if key in value}
        brief['lanes'] = lanes
        now = self.clock()
        if brief != self.previous or now - self.last_at >= 30:
            self.emit(json.dumps(brief, ensure_ascii=False), flush=True)
            self.previous, self.last_at = brief, now


class StorageWorker:
    def __init__(self, root):
        self.root, self.thread = Path(root), None
        self.state = {'status': 'idle'}

    def start(self):
        def work():
            try:
                self.state = {'status': 'running'}
                result = maintain(self.root, powershell=True, progress=lambda **v: self.state.update(v))
                self.state = {key: result[key] for key in ('status', 'finished_at', 'removed_bytes',
                              'compression_saved_bytes', 'compacted_accounts', 'reason') if key in result}
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error, subprocess.SubprocessError):
                self.state = {'status': 'deferred', 'reason': 'storage_busy_or_unavailable'}
        self.thread = threading.Thread(target=work, name='storage-maintenance', daemon=True)
        self.thread.start()

    def close(self):
        if self.thread:
            self.thread.join(timeout=30)
