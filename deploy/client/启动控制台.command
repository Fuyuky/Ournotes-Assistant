#!/bin/bash
set -euo pipefail
exec "$(cd -- "$(dirname -- "$0")" && pwd)/boot.command"
