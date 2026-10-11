"""Inspect or apply the shared storage policy; never starts devices or contacts cloud."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ournotes.paths import DATA_ROOT
from ournotes.runtime.storage import maintain


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=DATA_ROOT)
    parser.add_argument('--apply', action='store_true', help='Apply the persisted policy; default only inventories.')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    value = maintain(args.root, dry_run=not args.apply, powershell=True,
                     progress=lambda **v: print(json.dumps(v, ensure_ascii=False), flush=True))
    if args.report:
        from ournotes.common.files import atomic_json
        atomic_json(args.report, value)
    print(json.dumps({key: value[key] for key in ('status', 'keep_batches', 'delete_bytes',
                     'removed_bytes', 'compression_saved_bytes', 'compacted_accounts', 'reason') if key in value},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
