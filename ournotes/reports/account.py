from __future__ import annotations

import argparse
import json
from pathlib import Path


def format_account(report):
    totals = report["totals"]
    m, s = totals["member"], totals["support"]
    lines = [f"角色卡：SSR {m['SSR']}，SR {m['SR']}；支援卡（留影）：SSR {s['SSR']}，SR {s['SR']}。",
             f"预设：{report['preset']}｜邮箱：{report['alias']}",
             f"运行用时：{report.get('active_seconds', 0):.1f} 秒｜状态：{report['status']}", ""]
    for kind, label in (("member", "角色卡"), ("support", "支援卡（留影）")):
        lines += [label]
        entries = [c for c in report["summary"] if c["kind"] == kind and c["rarity"] != "R"]
        entries.sort(key=lambda c: (c["rarity"] != "SSR", c["id"]))
        lines += [f"{c['rarity']} 【{c['title']}】{c['name']} x{c['count']}" for c in entries] or ["无SSR/SR"]
        lines += [""]
    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("files", nargs="+", type=Path)
    args = p.parse_args()
    for path in args.files:
        print(format_account(json.loads(path.read_text(encoding="utf-8"))))


if __name__ == "__main__":
    main()
