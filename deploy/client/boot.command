#!/bin/bash
set -euo pipefail
install_root="$(cd -- "$(dirname -- "$0")" && pwd)"
if [[ "$(/usr/bin/uname -m)" != "arm64" ]]; then
  printf '%s\n' '此客户端只支持 Apple Silicon；不支持 Intel Mac 或 Rosetta 启动。' >&2
  exit 1
fi
version="$(/usr/bin/plutil -extract version raw -o - "$install_root/current.json")"
target_platform="$(/usr/bin/plutil -extract platform raw -o - "$install_root/current.json")"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ || "$target_platform" != "macos-arm64" ]]; then
  printf '%s\n' '客户端版本指针或平台无效。' >&2
  exit 1
fi
version_root="$install_root/versions/$version"
python_path="$version_root/runtime/bin/python3"
program_root="$version_root/app"
export OURNOTES_INSTALL_ROOT="$install_root"
export OURNOTES_DATA_ROOT="$install_root/user-data"
if [[ ! -x "$python_path" || ! -f "$program_root/scripts/webui.py" ]]; then
  printf '%s\n' '当前版本缺少可执行的 Python 运行时或控制台入口。' >&2
  exit 1
fi
"$python_path" -X utf8 "$install_root/boot_client.py"
