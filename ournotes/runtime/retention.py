"""Remove a successful account's temporary images after its archive is durable."""
from __future__ import annotations

import json
import os
import re
import stat

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp'}


def existing_file(db, path, folder):
    """One directory inventory per maintenance pass, never a deletion allowlist."""
    cache = getattr(db, 'retention_files', None)
    if cache is None:
        return path.is_file()
    if folder not in cache:
        files, links, pending = set(), [], [folder]
        while pending:
            directory = pending.pop()
            try:
                entries = os.scandir(directory)
            except FileNotFoundError:
                continue
            with entries as iterator:
                for entry in iterator:
                    candidate = directory / entry.name
                    attributes = entry.stat(follow_symlinks=False)
                    if (stat.S_ISLNK(attributes.st_mode)
                            or getattr(attributes, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                        # Do not follow links or mistake their contents for absent
                        # files. The normal path validator must inspect candidates.
                        links.append(candidate)
                    elif stat.S_ISDIR(attributes.st_mode):
                        pending.append(candidate)
                    else:
                        files.add(candidate)
        cache[folder] = files, links
    files, links = cache[folder]
    return path in files or any(path == link or path.is_relative_to(link) for link in links)


def evidence_folders(db, report):
    """Follow the same account's verified transfer chain, never arbitrary paths."""
    run_id, batch_id = report['run_id'], report.get('batch_id')
    run_ids = [run_id]
    for transfer in reversed(report.get('batch_transfers', [])):
        if (transfer.get('account_id') != report['id']
                or transfer.get('index') != report['alias_index']
                or transfer.get('to_run') != run_id or transfer.get('to_batch') != batch_id):
            raise ValueError('截图来源的账号迁移链不一致')
        source = db.get_run(transfer.get('from_run'))
        if (not source or source.get('batch_id') != transfer.get('from_batch')
                or transfer not in source.get('transferred_accounts', [])):
            raise ValueError('截图来源缺少原运行迁移审计')
        run_id, batch_id = source['id'], source['batch_id']
        run_ids.append(run_id)
    cache = getattr(db, 'retention_folders', None)
    base = db.root / 'state/runs'
    if cache is not None and base in cache:
        base = cache[base]
    else:
        resolved = base.resolve()
        if cache is not None:
            cache[base] = resolved
        base = resolved
    folders = []
    for run_id in run_ids:
        candidate = base / run_id
        folder = cache.get(candidate) if cache is not None else None
        if folder is None:
            folder = candidate.resolve()
        if not re.fullmatch(r'[a-f0-9]{32}', run_id) or folder.parent != base:
            raise ValueError('截图目录不在预期范围')
        if cache is not None:
            cache[candidate] = folder
        folders.append(folder)
    return folders


def cleanup_success(db, report):
    if report['status'] != 'complete' or report.get('unknown_slots', 0):
        return
    # Never purge an image that a background job might still need.
    if any(d['status'] not in ('complete', 'skipped') for d in report['draws']):
        raise ValueError('账号尚有未完成识别，不可删除截图')
    folders = evidence_folders(db, report)
    candidates, evidence = set(), []
    for draw in report['draws']:
        evidence += [e for key in ('screenshot', 'before') if (e := draw.get(key)) and e.get('path')]
        evidence += [e for key in ('history_baseline', 'history')
                     if (e := draw.get(key, {}).get('screenshot')) and e.get('path')]
    for row in db.db.execute('SELECT data FROM actions WHERE account_id=?', (report['id'],)):
        frame = json.loads(row[0]).get('frame', {})
        if frame.get('path'):
            evidence.append(frame)
    for item in evidence:
        candidate = (db.root/item['path']).absolute()
        if '..' in candidate.parts:
            raise ValueError('拒绝删除运行截图目录以外的文件')
        if (not any(candidate.is_relative_to(folder) for folder in folders)
                or candidate.suffix.lower() not in IMAGE_SUFFIXES):
            raise ValueError('拒绝删除运行截图目录以外的文件')
        owner_folder = next(folder for folder in folders if candidate.is_relative_to(folder))
        if not existing_file(db, candidate, owner_folder):
            continue
        candidate = candidate.resolve()
        if not any(candidate.is_relative_to(folder) for folder in folders):
            raise ValueError('拒绝删除运行截图目录以外的文件')
        candidates.add(candidate)
    # A recovered account may also have previously dumped incident buffers.
    for folder in folders:
        cache = getattr(db, 'retention_incidents', None)
        incidents = cache.get(folder) if cache is not None else None
        if incidents is None:
            incidents = {}
            for checkpoint in (folder/'incidents').glob('*/state.json'):
                incident = json.loads(checkpoint.read_text(encoding='utf-8'))
                owner = incident.get('snapshot', {}).get('account_id')
                incidents.setdefault(owner, []).append(checkpoint.parent)
            if cache is not None:
                cache[folder] = incidents
        for incident_folder in incidents.get(report['id'], []):
            for image in incident_folder.iterdir():
                if image.suffix.lower() in IMAGE_SUFFIXES:
                    candidate = image.resolve()
                    if not candidate.is_relative_to(folder):
                        raise ValueError('异常截图目录不在预期范围')
                    candidates.add(candidate)
    # Validate the whole set before deleting anything; unlink only individual files.
    if remover := getattr(db, 'artifact_remover', None):
        remover(candidates)
    else:
        for candidate in candidates:
            candidate.unlink(missing_ok=True)
    for draw in report['draws']:
        changed = False
        for key in ('screenshot', 'before'):
            if draw.get(key, {}).get('path') and draw[key].get('storage') != 'removed_after_success':
                draw[key]['storage'] = 'removed_after_success'
                changed = True
        for key in ('history_baseline', 'history'):
            if (draw.get(key, {}).get('screenshot', {}).get('path')
                    and draw[key]['screenshot'].get('storage') != 'removed_after_success'):
                draw[key]['screenshot']['storage'] = 'removed_after_success'
                changed = True
        if changed:
            db.save_draw(draw)
