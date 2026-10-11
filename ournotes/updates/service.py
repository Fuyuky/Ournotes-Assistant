"""Check on startup; download only after an explicit user command."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from ournotes.platforms.system import current_platform
from ournotes.updates.github import PUBLIC_REPOSITORY, GitHubReleases
from ournotes.updates.installer import atomic_json
from ournotes.updates.manifest import verify, version_tuple


class UpdateService:
    def __init__(self, data_root, install_root=None, client_factory=GitHubReleases, platform=None):
        self.data_root = Path(data_root).resolve()
        self.install_root = Path(install_root).resolve() if install_root else None
        self.platform = platform or current_platform()
        self.client_factory = client_factory
        self.lock = threading.RLock()
        self.action_lock = threading.Lock()
        self.release = None
        self.state = {"status": "unchecked" if self.install_root else "source",
                      "available": False, "downloaded": False, "repository": PUBLIC_REPOSITORY,
                      "platform": self.platform}

    def status(self):
        with self.lock:
            return dict(self.state)

    def check(self):
        with self.action_lock:
            try:
                if not self.install_root:
                    self.state.update(status="source", available=False, downloaded=False)
                    return dict(self.state)
                client = self.client_factory(PUBLIC_REPOSITORY)
                release = client.latest()
                version = release["tag_name"].removeprefix("v")
                current = json.loads((self.install_root / "current.json").read_text("utf-8"))["version"]
                available = version_tuple(version) > version_tuple(current)
                if available and not any(asset.get('name') == f'ournotes-manifest-{self.platform}.json'
                                         for asset in release['assets']):
                    raise ValueError('稳定发行版尚无当前平台的签名清单。')
                self.release = release if available else None
                self.state = {"status": "available" if available else "up_to_date", "available": available,
                              "version": version, "downloaded": False, "repository": PUBLIC_REPOSITORY,
                              "platform": self.platform}
            except (ValueError, OSError, KeyError, TypeError):
                self.release = None
                self.state.update(status="unavailable", available=False, downloaded=False,
                                  message="公开更新检查暂不可用，不影响本地批次。")
            return dict(self.state)

    def download(self):
        with self.action_lock:
            if not self.release or not self.install_root:
                raise ValueError("请先检查可用的稳定更新。")
            client = self.client_factory(PUBLIC_REPOSITORY)
            assets = {asset["name"]: asset for asset in self.release["assets"]}
            manifest_asset = assets.get(f"ournotes-manifest-{self.platform}.json")
            if not manifest_asset:
                raise ValueError("发行版缺少签名清单。")
            with client.asset(manifest_asset["id"]) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("发行版清单过大。")
            try:
                envelope = json.loads(raw)
            except (ValueError, UnicodeError) as error:
                raise ValueError('发行版清单不是有效的JSON。') from error
            public_key = (self.install_root / "update-public-key.txt").read_text("utf-8").strip()
            try:
                manifest = verify(envelope, public_key, self.platform)
            except (TypeError, KeyError) as error:
                raise ValueError('发行版清单格式无效。') from error
            if manifest["version"] != self.release["tag_name"].removeprefix("v"):
                raise ValueError("签名版本与发行版不一致。")
            asset = assets.get(manifest["artifact"]["name"])
            if not asset:
                raise ValueError("发行版缺少发布包。")
            folder = self.data_root / "state/updates/downloads" / manifest["version"]
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / manifest["artifact"]["name"]
            temporary = target.with_suffix(".partial")
            digest, size = hashlib.sha256(), 0
            try:
                with client.asset(asset["id"]) as response, temporary.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > manifest["artifact"]["bytes"]:
                            raise ValueError("下载超过签名包大小。")
                        digest.update(chunk)
                        output.write(chunk)
                if size != manifest["artifact"]["bytes"] or digest.hexdigest() != manifest["artifact"]["sha256"]:
                    raise ValueError("下载包摘要验证失败。")
                os.replace(temporary, target)
                atomic_json(folder / "manifest.json", envelope)
            finally:
                temporary.unlink(missing_ok=True)
            self.state.update(status="downloaded", downloaded=True, package=str(target), manifest=str(folder / "manifest.json"))
            return dict(self.state)

    def start_install(self):
        with self.action_lock:
            if not self.install_root or not self.state.get("downloaded"):
                raise ValueError("请先下载并验证更新包。")
            folder = self.data_root / "state/updates"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "installer.log").open("wb") as log:
                subprocess.Popen([sys.executable, "-X", "utf8", "-m", "ournotes.updates.installer", "--install-root", str(self.install_root), "--data-root", str(self.data_root), "--package", self.state["package"], "--manifest", self.state["manifest"], "--public-key", str(self.install_root / "update-public-key.txt"), "--wait-for-client", "60"], stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, start_new_session=os.name != "nt")
            return {"message": "控制台将退出，独立更新器等待写入者停止后安装。完成后双击启动控制台；失败保留原版本。"}
