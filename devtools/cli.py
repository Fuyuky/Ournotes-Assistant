"""Thin JSON HTTP adapter. Credentials are supplied through the environment."""
import argparse
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("capabilities", "status", "operation", "submit"))
    parser.add_argument("--url", default="http://127.0.0.1:8766/agent")
    parser.add_argument("--id")
    parser.add_argument("--file", help="JSON command file")
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("机器接口仅支持本机loopback地址")
    credential = os.environ.get("OURNOTES_AGENT_TOKEN", "")
    if not credential:
        parser.error("OURNOTES_AGENT_TOKEN is required")
    endpoint = args.command
    body = None
    if args.command == "operation":
        if not args.id:
            parser.error("--id is required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", args.id):
            parser.error("--id format is invalid")
        endpoint = "operations/" + args.id
    elif args.command == "submit":
        if not args.file:
            parser.error("--file is required")
        from pathlib import Path
        body = json.dumps(json.loads(Path(args.file).read_text("utf-8"))).encode()
        endpoint = "commands"
    request = urllib.request.Request(args.url.rstrip("/") + "/api/v1/" + endpoint, data=body, headers={"Authorization": "Bearer " + credential, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            print(json.dumps(json.load(response), ensure_ascii=False))
    except urllib.error.HTTPError as error:
        print(json.dumps({"reason": "http_error", "status": error.code}))
        raise SystemExit(2) from None
    except OSError:
        print(json.dumps({"reason": "service_unavailable"}))
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
