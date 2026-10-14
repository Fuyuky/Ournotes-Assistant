"""Import an explicitly supplied private connection into Windows DPAPI."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def configure(install_root, data_root, config_type):
    seed = Path(install_root) / "client-connection.json"
    if not seed.is_file():
        return "no_seed"
    # An existing installation always keeps its own node identity.
    if config_type.load(data_root) is not None:
        seed.unlink()
        return "already_configured"
    value = json.loads(seed.read_text("utf-8"))
    if set(value) != {"service_url", "credential"}:
        raise ValueError("初装连接配置格式无效。")
    config_type.save(data_root, value["service_url"], value["credential"])
    saved = config_type.load(data_root)
    if saved is None or saved.credential != value["credential"]:
        raise ValueError("初装连接配置未能保存。")
    seed.unlink()
    return "imported"


def main():
    install_root = Path(__file__).resolve().parent
    pointer = json.loads((install_root / "current.json").read_text("utf-8"))
    import re
    if not re.fullmatch(r"\d+\.\d+\.\d+", pointer["version"]):
        raise ValueError("版本指针无效。")
    sys.path.insert(0, str(install_root / "versions" / pointer["version"] / "app"))
    from ournotes.integrations.cloud_client import CloudClientConfig
    configure(install_root, Path(os.environ["OURNOTES_DATA_ROOT"]), CloudClientConfig)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 -- never print credential-bearing library exceptions
        print("自动配置连接失败；请检查安装目录写入权限，或在控制台手动保存连接。", file=sys.stderr)
        sys.exit(1)
