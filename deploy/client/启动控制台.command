#!/bin/bash
set -euo pipefail
if ! "$(cd -- "$(dirname -- "$0")" && pwd)/boot.command"; then
  printf '%s\n' '启动失败，请查看上方错误。按回车关闭。' >&2
  read -r _unused || true
  exit 1
fi
