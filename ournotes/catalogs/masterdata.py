"""Read one atomic upstream Git tree, explicitly restricted to the HK/TW/MO region."""
from __future__ import annotations

import hashlib
import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path

from ournotes.common.files import atomic_json

REGION = 'hk-tw-mo'
REPOSITORY = 'https://github.com/StarMoe-org/moenotes-masterdata'
ARCHIVE = 'https://codeload.github.com/StarMoe-org/moenotes-masterdata/zip/refs/heads/main'
MAX_ARCHIVE = 64 * 1024 * 1024


def read_archive(data, names):
    """Do not extract upstream paths. Validate all requested tables before writing."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        commit = archive.comment.decode('ascii')
        if not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise ValueError('主数据归档缺少固定 Git 提交。')
        prefix = 'moenotes-masterdata-main/'

        def read(name):
            info = archive.getinfo(prefix + name)
            if info.file_size > MAX_ARCHIVE:
                raise ValueError('主数据表过大。')
            return archive.read(info)

        version = json.loads(read('current_version.json'))['regions'][REGION]
        if version['data_path'] != REGION or version['snapshot_path'] != REGION:
            raise ValueError('主数据服务器与 hk-tw-mo 不符。')
        manifest = read(f'{REGION}/MasterManifest.json')
        if (hashlib.sha256(manifest).hexdigest() != version['manifest_sha256']
                or json.loads(manifest)['version'] != version['version']):
            raise ValueError('主数据版本清单校验失败。')
        tables = {}
        for name in names:
            payload = json.loads(read(f'{REGION}/{name}.json'))
            rows = payload if isinstance(payload, list) else payload['_allData']
            ids = [row.get('id', row.get('_id')) for row in rows]
            if not rows or None in ids or len(ids) != len(set(ids)):
                raise ValueError(f'主数据表为空或身份重复：{name}')
            tables[name] = payload
        source = {'region': REGION, 'repository': REPOSITORY, 'commit': commit,
                  'version': version['version'], 'resource_version': version['resource_version'],
                  'client_version': version['client_version'],
                  'manifest_sha256': version['manifest_sha256']}
        return source, tables


def download_tables(directory, names):
    request = urllib.request.Request(ARCHIVE, headers={'User-Agent': 'OurNotesAssistant/0.1'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError('主数据归档过大。')
    source, tables = read_archive(data, names)
    directory = Path(directory)
    for name, payload in tables.items():
        atomic_json(directory / f'{name}.json', payload)
    atomic_json(directory / 'source.json', source)
    return source
