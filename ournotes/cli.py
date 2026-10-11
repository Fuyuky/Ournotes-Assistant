from __future__ import annotations

import argparse
import json
from pathlib import Path

from ournotes.paths import DATA_ROOT


def safe_error(error: Exception) -> str:
    # Third-party OAuth/HTTP exceptions can embed callback URLs or message data.
    if type(error) in (ValueError, RuntimeError, TimeoutError):
        return str(error)
    from ournotes.common.otp import NeedsReview
    if isinstance(error, NeedsReview):
        return str(error)
    return f"操作失败（{type(error).__name__}）；未记录令牌、回调地址或邮件正文。"


def main():
    parser = argparse.ArgumentParser(description="OurNotes 内部工具；日常操作请使用图形控制台")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="只读运行状态")
    p = sub.add_parser("mumu-info")
    p.add_argument("--root", type=Path, default=Path("D:/Tools/MuMuPlayer"))
    p.add_argument("--instance", type=int, default=0)
    p = sub.add_parser("cards-fetch", help="一次性下载bdon卡片信息与识别素材，可断点继续")
    p.add_argument("--output", type=Path, default=Path("data/catalog/site"))
    p = sub.add_parser("recognize", help="离线识别招募结算截图，不操作游戏")
    p.add_argument("images", type=Path, nargs="+", help="截图文件，或仅包含结算截图的目录")
    p.add_argument("--catalog", type=Path, default=Path("data/catalog/site"))
    p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "status":
            from ournotes.control.query import RuntimeQuery
            print(json.dumps(RuntimeQuery(DATA_ROOT).runtime(), ensure_ascii=False))
        elif args.command == "mumu-info":
            from ournotes.integrations.mumu import instance_info
            info = instance_info(args.root, args.instance)
            print(json.dumps({key: info.get(key) for key in ("index", "name", "is_android_started", "android_version")}, ensure_ascii=False))
        elif args.command == "cards-fetch":
            from ournotes.catalogs.cards import build_catalog
            catalog = build_catalog(args.output)
            print(f"已保存{len(catalog['cards'])}张卡片的信息与素材：{args.output.resolve()}")
        elif args.command == "recognize":
            from ournotes.vision.recognition import CardRecognizer, recognize_files, summary_text
            paths = []
            for path in args.images:
                if path.is_dir():
                    paths.extend(sorted(p for p in path.iterdir()
                                        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")))
                else:
                    paths.append(path)
            if not paths:
                raise ValueError("没有找到结算截图。")
            result = recognize_files(CardRecognizer(args.catalog), paths, args.output)
            print(f"已识别{result['matched_pulls']}抽；状态：{result['status']}")
            print(summary_text(result["summary_without_r"]))
            print(f"详细记录：{(args.output / 'recognition.json').resolve()}")
            if result["status"] != "complete":
                raise SystemExit(2)
    except KeyboardInterrupt:
        print("已停止当前离线工具。")
        raise SystemExit(130)
    except Exception as error:  # noqa: BLE001 - redact external exceptions at the CLI boundary
        print(safe_error(error))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
