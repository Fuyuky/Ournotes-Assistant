"""Independent updater process. Extract new version, verify, then switch pointer."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path

from ournotes.common.files import atomic_json
from ournotes.common.secrets import SecretStore
from ournotes.updates.manifest import file_hash, safe_member, verify, version_tuple


def prune_old_versions(root, data, keep):
    """Under update/client/runner leases; retain current and previous versions."""
    from ournotes.runtime.storage import inside
    removed = []
    for base in (root / 'versions', data / 'state/updates/downloads'):
        if not base.is_dir():
            continue
        owner = root if base == root / 'versions' else data
        for folder in base.iterdir():
            if not folder.is_dir() or folder.name in keep:
                continue
            try:
                version_tuple(folder.name)
            except ValueError:
                continue  # Unknown names are not update-owned versions.
            inside(owner, folder, base)
            for path in folder.rglob('*'):
                inside(owner, path, folder)
            shutil.rmtree(folder)
            removed.append(str(folder.relative_to(owner)))
    return removed


def install(install_root, data_root, package, envelope, public_key, lock_factory=None, *, platform=None):
    root, data = Path(install_root).resolve(), Path(data_root).resolve()
    package = Path(package).resolve()
    manifest = verify(envelope, public_key, platform)
    if package.stat().st_size != manifest["artifact"]["bytes"] or file_hash(package) != manifest["artifact"]["sha256"]:
        raise ValueError("发布包大小或SHA256校验失败。")
    versions = root / "versions"
    if versions.is_symlink():
        raise ValueError("版本目录不可为符号链接。")
    versions.mkdir(parents=True, exist_ok=True)
    factory = lock_factory or (lambda name: SecretStore(data / "state").lock(name))
    with factory("update"), factory("client"), factory("runner"), factory("email-rebind"):
        current = json.loads((root / "current.json").read_text("utf-8"))
        if current.get('platform', manifest['platform']) != manifest['platform']:
            raise ValueError('安装目录的平台指针与更新包不一致。')
        if version_tuple(manifest["version"]) <= version_tuple(current["version"]):
            raise ValueError("新版本必须高于当前版本。")
        target = versions / manifest["version"]
        if target.exists():
            raise ValueError("目标版本目录已存在；保留原目录供核查。")
        staging = versions / (".staging-" + uuid.uuid4().hex)
        staging.mkdir()
        try:
            seen = set()
            size = 0
            with zipfile.ZipFile(package) as archive:
                for item in archive.infolist():
                    if item.is_dir():
                        continue
                    member = safe_member(item.filename)
                    folded = item.filename.casefold()
                    if folded in seen or stat.S_ISLNK(item.external_attr >> 16):
                        raise ValueError("发布包含重复路径或符号链接。")
                    seen.add(folded)
                    if item.filename not in manifest["files"]:
                        raise ValueError("发布包包含清单外文件。")
                    size += item.file_size
                    if size > 2 * 1024**3:
                        raise ValueError("发布包解压大小超出限制。")
                    destination = staging.joinpath(*member.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(item) as source, destination.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    if file_hash(destination) != manifest["files"][item.filename]:
                        raise ValueError("文件摘要与签名清单不一致。")
                    if manifest['platform'] == 'macos-arm64' and item.filename in manifest.get('executables', []):
                        destination.chmod(0o755)
            if seen != {name.casefold() for name in manifest["files"]}:
                raise ValueError("发布包缺少清单文件。")
            runtime_entry = "runtime/pythonw.exe" if manifest['platform'] == 'windows-x64' else "runtime/bin/python3"
            for essential in (runtime_entry, "app/scripts/webui.py", "app/ournotes/control/web.py"):
                if essential not in manifest["files"]:
                    raise ValueError("发布包缺少启动必需文件。")
            atomic_json(staging / "release.json", envelope)
            os.replace(staging, target)
            atomic_json(root / "current.json", {"version": manifest["version"], "platform": manifest['platform']})
            result = {"installed_version": manifest["version"], "previous_version": current["version"], "restart_required": True}
            try:
                result['pruned'] = prune_old_versions(root, data, {manifest['version'], current['version']})
            except (OSError, ValueError) as error:
                result['cleanup_deferred'] = type(error).__name__
            return result
        finally:
            # Only our own freshly created staging directory is disposable.
            if staging.exists() and staging.resolve().parent == versions:
                shutil.rmtree(staging)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--wait-for-client", type=float, default=0)
    args = parser.parse_args()
    deadline = time.monotonic() + min(max(args.wait_for_client, 0), 60)
    while True:
        try:
            result = install(args.install_root, args.data_root, args.package, json.loads(args.manifest.read_text("utf-8")), args.public_key.read_text("utf-8").strip())
            break
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)  # Retry only lock handoff; never a game action.
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
