"""Prepare a private, reviewable historical import package without networking."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter
from contextlib import closing
from pathlib import Path

from ournotes.accounts.index import AccountIndex
from ournotes.cloud.models import HighWater, Historical, Result
from ournotes.common.files import atomic_json, file_hash
from ournotes.common.identity import FORMAL_START, TEST_START, alias_at, mailbox_identity
from ournotes.integrations.cloud_sync import result_projection
from ournotes.paths import DATA_ROOT


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def prepare(root, node_id, *, catalog_dir=None):
    """Read one SQLite snapshot and existing files; never initialize RuntimeStore."""
    root = Path(root).resolve()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', node_id):
        raise ValueError('节点标识无效。')
    directory = root / 'results/archive/accounts'
    catalog_dir = Path(catalog_dir or root / 'data/catalog/site').resolve()
    index = AccountIndex(directory, catalog_dir)
    signature = index.scan()
    catalog_hash = file_hash(catalog_dir / 'catalog.json')
    database = root / 'state/runtime.sqlite3'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        counters = dict(db.execute('SELECT channel,next_index FROM counters'))
        rows = db.execute('SELECT id,alias,channel,alias_index,status FROM accounts ORDER BY id').fetchall()
    if set(counters) != {'test', 'formal'}:
        raise ValueError('计数器清单不完整。')
    high_water = HighWater(test_next_index=counters['test'], formal_next_index=counters['formal'],
                          evidence='prepared-local-snapshot').model_dump()
    owners, allocated = {}, set()
    for account_id, alias, channel, number, status in rows:
        if (channel not in counters or type(number) is not int or number < (TEST_START if channel == 'test' else FORMAL_START)
                or number >= counters[channel] or alias != alias_at(number, mailbox_identity(alias)) or number in allocated):
            raise ValueError('运行库编号、别名或高水位冲突。')
        allocated.add(number)
        owners[account_id] = {'account_id': account_id, 'alias': alias, 'channel': channel,
                              'index': number, 'status': status}
    index.refresh()
    valid_indices = {item['index'] for item in index.snapshot.accounts}
    if not index.snapshot.available or index.signature != signature:
        raise ValueError('生成期间档案集合已变化。')
    imports, sources, excluded, parsed = [], [], [], []
    identities, numbers = Counter(), Counter()
    for name, _, _ in signature:
        path = directory / name
        if path.is_symlink() or path.resolve().parent != directory.resolve():
            raise ValueError('档案路径越界。')
        raw = path.read_bytes()
        source = {'path': path.relative_to(root).as_posix(), 'sha256': hashlib.sha256(raw).hexdigest()}
        sources.append(source)
        try:
            archive = json.loads(raw)
            if not isinstance(archive, dict):
                raise TypeError('invalid_schema')
        except (ValueError, UnicodeError, TypeError):
            excluded.append({**source, 'reason': 'unreadable_json'})
            continue
        account_id, number = archive.get('account_id'), archive.get('alias_index')
        if isinstance(account_id, str):
            identities[account_id] += 1
        if type(number) is int:
            numbers[number] += 1
        parsed.append((archive, source))
    if any(count > 1 for count in identities.values()) or any(count > 1 for count in numbers.values()):
        raise ValueError('现存档案包含重复UUID或编号，拒绝污染导入。')
    for archive, source in parsed:
        account_id, number = archive.get('account_id'), archive.get('alias_index')
        if account_id is not None and not isinstance(account_id, str):
            raise ValueError('档案UUID无效。')
        owner = owners.get(account_id)
        if account_id is not None:
            uuid.UUID(account_id)
        if ((account_id is not None and owner is None)
                or (owner is not None and (number != owner['index'] or archive.get('alias') != owner['alias']))
                or (type(number) is int and archive.get('alias') != alias_at(number, mailbox_identity(archive['alias'])))):
            raise ValueError('档案原UUID、归属、完成状态或编号不一致。')
        if type(number) is not int or number not in valid_indices:
            try:
                index.account(archive)
            except (ValueError, KeyError, TypeError) as error:
                reason = str(error) if type(error) is ValueError and re.fullmatch('[a-z_]+', str(error)) else 'invalid_schema'
                excluded.append({**source, 'reason': reason})
                continue
            raise ValueError('共享成果集合在生成期间变化。')
        if owner is None:
            excluded.append({**source, 'reason': 'missing_original_uuid'})
            continue
        if owner['status'] != 'complete':
            raise ValueError('成果与运行库完成状态冲突。')
        try:
            result = Result.model_validate(result_projection(archive)).model_dump()
        except (ValueError, KeyError, TypeError):
            excluded.append({**source, 'reason': 'invalid_final_result'})
            continue
        body = {'node_id': node_id, 'channel': owner['channel'], 'result': result}
        body['request_id'] = 'historical-' + digest(body)
        imports.append(Historical.model_validate(body).model_dump())
        source.update(account_id=account_id, index=owner['index'])
    if not imports:
        raise ValueError('没有可导入的现存成功档案。')
    if index.scan() != signature or file_hash(catalog_dir / 'catalog.json') != catalog_hash:
        raise ValueError('生成期间档案或素材目录已变化，请暂停写入后重试。')
    for source in sources:
        if file_hash(root / source['path']) != source['sha256']:
            raise ValueError('生成期间档案内容已变化。')
    evidence = {'runtime_snapshot_sha256': digest({'counters': counters, 'accounts': rows}),
                'catalog_sha256': catalog_hash, 'archives': sources}
    high_water['evidence'] = 'bootstrap-evidence-sha256:' + digest(evidence)
    package = {'schema_version': 1, 'kind': 'private-historical-import', 'node_id': node_id,
               'high_water': high_water, 'evidence': evidence, 'imports': imports, 'excluded': excluded}
    package['sha256'] = digest(package)
    destination = root / 'results/maintenance' / ('cloud-bootstrap-' + package['sha256'] + '.json')
    atomic_json(destination, package)
    return destination, package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare'])
    parser.add_argument('--root', type=Path, default=DATA_ROOT)
    parser.add_argument('--node-id', required=True, help='拟定目标节点标识，非凭据；prepare不登记节点')
    parser.add_argument('--catalog-dir', type=Path)
    args = parser.parse_args()
    path, package = prepare(args.root, args.node_id, catalog_dir=args.catalog_dir)
    print(json.dumps({'path': str(path), 'sha256': package['sha256'], 'accounts': len(package['imports']),
                      'excluded': len(package['excluded'])},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
