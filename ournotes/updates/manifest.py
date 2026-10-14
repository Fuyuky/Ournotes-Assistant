"""Pinned Ed25519 trust and platform-bound client artifact manifests."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import PurePosixPath

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ournotes.platforms.system import SUPPORTED_PLATFORMS, current_platform


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("只接收正式稳定版号。")
    return tuple(int(part) for part in value.split("."))


def safe_member(name, version=None):
    if not isinstance(name, str):
        raise TypeError("发布包路径必须是文本。")
    path = PurePosixPath(name)
    if not name or "\\" in name or ":" in name or path.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("发布包包含无效路径。")
    if any(part.rstrip(' .') != part or part.split('.')[0].upper() in
           {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
           for part in path.parts):
        raise ValueError("发布包包含平台保留或有歧义的路径。")
    if version is not None:
        if len(path.parts) == 1 and name in (
            'current.json', 'update-public-key.txt', 'AGENTS.md', 'README.txt',
            '启动控制台.cmd', 'boot.ps1', 'boot_client.py', 'configure_client.py',
        ):
            return path
        if path.parts[:3] == ('user-data', 'data', 'catalog'):
            if path.parts[3:] == ('current.json',) or (
                len(path.parts) >= 6 and path.parts[3] == 'snapshots'
                and re.fullmatch(r'[a-f0-9]{32,64}', path.parts[4])
            ):
                return path
            raise ValueError('发布包素材路径无效。')
        if path.parts[:2] != ('versions', version):
            raise ValueError('发布包不能覆盖用户配置、状态或其他版本。')
        safe_member('/'.join(path.parts[2:]))
        return path
    if path.parts[0] not in ("app", "runtime"):
        raise ValueError("发布包不能覆盖用户数据或固定素材。")
    if (len(path.parts) > 1 and path.parts[0] == "app"
            and path.parts[1] in ("state", "results", "data", "ournotes_dev", "devtools", ".venv")):
        raise ValueError("客户端更新夹带用户数据、素材或开发接口。")
    if path.parts[:3] == ("app", "ournotes", "cloud"):
        raise ValueError("客户端发布包不能夹带服务端。")
    if path.parts[:3] == ("app", "web", "admin") or "marketplace" in path.parts:
        raise ValueError("客户端发布包不能夹带服务器管理后台或商品上架工具。")
    if path.name == "oauth_setup.py" or "reference" in path.parts or path.name == "client-connection.json":
        raise ValueError("客户端发布包不能夹带本地 OAuth、回归素材或节点凭据。")
    return path


def verify(envelope, public_key, expected_platform=None):
    if not isinstance(envelope, dict):
        raise TypeError("签名清单必须是对象。")
    if set(envelope) != {"manifest", "signature"}:
        raise ValueError("签名清单格式无效。")
    manifest = envelope["manifest"]
    if not isinstance(manifest, dict):
        raise TypeError("发布清单必须是对象。")
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True))
        key.verify(base64.b64decode(envelope["signature"], validate=True), canonical(manifest))
    except (ValueError, InvalidSignature) as error:
        raise ValueError("发布清单签名验证失败。") from error
    if (manifest.get("schema") not in (2, 3) or manifest.get("channel") != "stable"
            or manifest.get("product") != "ournotes-assistant" or "variant" in manifest):
        raise ValueError("只接收统一客户端的稳定更新。")
    target = manifest.get("platform")
    if target not in SUPPORTED_PLATFORMS or target != (expected_platform or current_platform()):
        raise ValueError("更新包平台与当前客户端不一致。")
    version_tuple(manifest["version"])
    artifact = manifest.get("artifact", {})
    portable = manifest['schema'] == 3
    required = {'name'} if portable else {'name', 'sha256', 'bytes'}
    if not isinstance(artifact, dict) or not required <= artifact.keys():
        raise ValueError("发布包身份不完整。")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.zip", artifact["name"]) or (
        not portable and not re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"])
    ):
        raise ValueError("发布包身份无效。")
    if not portable and (type(artifact["bytes"]) is not int or artifact["bytes"] <= 0):
        raise ValueError("发布包大小无效。")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("发布清单缺少文件。")
    for name, digest in files.items():
        safe_member(name, manifest['version'] if portable else None)
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("文件摘要无效。")
    if len({name.casefold() for name in manifest["files"]}) != len(manifest["files"]):
        raise ValueError("清单含大小写冲突路径。")
    executables = manifest.get("executables", [])
    if (not isinstance(executables, list) or any(not isinstance(name, str) for name in executables)
            or len(set(executables)) != len(executables)
            or any(name not in manifest["files"] for name in executables)):
        raise ValueError("清单可执行文件列表无效。")
    return manifest


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
