"""One signed portable ZIP for both first install and in-place upgrades."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import uuid
import zipfile
from pathlib import Path

from ournotes.common.files import atomic_json
from ournotes.common.secrets import SecretStore
from ournotes.updates.manifest import file_hash, safe_member, verify, version_tuple

MAX_PACKAGE_BYTES = 2 * 1024**3


def validate_package(package, public_key, platform=None, destination=None):
    with zipfile.ZipFile(package) as archive:
        items = archive.infolist()
        names = [item.filename for item in items]
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError('发布包含重复或大小写冲突路径。')
        if sum(item.file_size for item in items) > MAX_PACKAGE_BYTES:
            raise ValueError('发布包解压大小超出限制。')
        if 'release.json' not in names or archive.getinfo('release.json').file_size > 2 * 1024**2:
            raise ValueError('发布包缺少有效的内置签名清单。')
        envelope = json.loads(archive.read('release.json'))
        manifest = verify(envelope, public_key, platform)
        if manifest['schema'] != 3:
            raise ValueError('需要完整免安装发布包。')
        files = manifest['files']
        if set(names) != set(files) | {'release.json'}:
            raise ValueError('发布包包含清单外文件或缺少文件。')
        prefix = f"versions/{manifest['version']}/"
        entry = 'runtime/python.exe'
        required = {'current.json', 'update-public-key.txt', 'boot_client.py',
                    prefix + entry, prefix + 'app/scripts/webui.py',
                    prefix + 'app/ournotes/control/web.py', 'user-data/data/catalog/current.json'}
        required.update({'boot.ps1', '启动控制台.cmd'})
        if not required <= files.keys():
            raise ValueError('发布包缺少启动必需文件。')
        for item in items:
            if item.is_dir() or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError('发布包不接受目录条目或符号链接。')
            if item.filename == 'release.json':
                continue
            member = safe_member(item.filename, manifest['version'])
            import hashlib
            digest = hashlib.sha256()
            with archive.open(item) as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != files[item.filename]:
                raise ValueError('文件摘要与签名清单不一致。')
            if destination is not None:
                target = Path(destination).joinpath(*member.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
        pointer = json.loads(archive.read('current.json'))
        if pointer != {'version': manifest['version'], 'platform': manifest['platform']}:
            raise ValueError('版本指针与签名不一致。')
        if archive.read('update-public-key.txt').decode().strip() != public_key.strip():
            raise ValueError('发布包不能替换受信任的公钥。')
        catalog = json.loads(archive.read('user-data/data/catalog/current.json'))
        directory = catalog['directory'].replace('\\', '/')
        for name in ('catalog.json', 'pools.json'):
            path = 'user-data/data/catalog/' + directory + '/' + name
            safe_member(path, manifest['version'])
            if files.get(path) != catalog['sha256'][name]:
                raise ValueError('运行素材指针与签名不一致。')
        return envelope


def replace_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def guarded_path(root, relative):
    target = root / relative
    for path in (target, *target.parents):
        if path == root:
            break
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ValueError('安装目标不可包含链接或目录联接。')
    if not target.resolve().is_relative_to(root):
        raise ValueError('安装目标越界。')
    return target


def install_portable(root, data, package, envelope, public_key, lock_factory=None, platform=None):
    root, data = Path(root).resolve(), Path(data).resolve()
    if data != root / 'user-data':
        raise ValueError('免安装版数据目录必须为安装目录下的 user-data。')
    manifest = verify(envelope, public_key, platform)
    version = manifest['version']
    factory = lock_factory or (lambda name: SecretStore(data / 'state').lock(name))
    with factory('update'), factory('client'), factory('runner'), factory('email-rebind'):
        current = json.loads((root / 'current.json').read_text('utf-8'))
        if version_tuple(version) <= version_tuple(current['version']):
            raise ValueError('新版本必须高于当前版本。')
        if current.get('platform', manifest['platform']) != manifest['platform']:
            raise ValueError('更新包平台与安装目录不一致。')
        target = guarded_path(root, 'versions/' + version)
        if target.exists():
            raise ValueError('目标版本目录已存在，未覆盖。')
        stage = guarded_path(root, 'versions/.staging-' + uuid.uuid4().hex)
        stage.mkdir(parents=True)
        backups = {}
        moved = False
        try:
            actual = validate_package(package, public_key, platform, stage)
            if actual != envelope:
                raise ValueError('下载清单与包内清单不一致。')
            # Validate every destination before changing any installation files.
            for name in manifest['files']:
                guarded_path(root, name)
            for source in (stage / 'user-data/data/catalog/snapshots').rglob('*'):
                if not source.is_file():
                    continue
                relative = source.relative_to(stage)
                destination = guarded_path(root, relative)
                if destination.exists():
                    if file_hash(destination) != file_hash(source):
                        raise ValueError('同名固定素材已存在且内容不同，未覆盖。')
                else:
                    replace_file(destination, source.read_bytes())
            os.replace(stage / 'versions' / version, target)
            moved = True
            replacements = [name for name in manifest['files'] if '/' not in name
                            and name not in ('current.json', 'update-public-key.txt')]
            replacements += ['user-data/data/catalog/current.json', 'release.json']
            (stage / 'release.json').write_text(json.dumps(envelope, ensure_ascii=False), 'utf-8')
            for name in replacements:
                destination = guarded_path(root, name)
                backups[name] = destination.read_bytes() if destination.exists() else None
                replace_file(destination, (stage / name).read_bytes())
            atomic_json(root / 'current.json', {'version': version, 'platform': manifest['platform']})
        except Exception:  # Restore boot files before propagating any install failure.
            for name, content in reversed(list(backups.items())):
                destination = root / name
                if content is None:
                    destination.unlink(missing_ok=True)
                else:
                    replace_file(destination, content)
            if moved:
                shutil.rmtree(target)
            raise
        finally:
            shutil.rmtree(stage)
        from ournotes.updates.installer import prune_old_versions
        result = {'installed_version': version, 'previous_version': current['version'], 'restart_required': True}
        try:
            result['pruned'] = prune_old_versions(root, data, {version, current['version']})
        except (OSError, ValueError) as error:
            result['cleanup_deferred'] = type(error).__name__
        return result
