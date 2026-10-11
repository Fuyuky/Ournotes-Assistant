"""Explicit subscription update. Imports nodes only and keeps previous config on failure."""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

NODE_FIELDS = {
    "name", "type", "server", "port", "cipher", "password", "uuid", "alterId", "udp",
    "tls", "servername", "sni", "network", "ws-opts", "grpc-opts", "http-opts", "h2-opts",
    "client-fingerprint", "fingerprint", "reality-opts", "flow", "alpn", "username",
    "auth-str", "up", "down", "obfs", "obfs-password", "skip-cert-verify", "ip-version",
}
NODE_TYPES = {"ss", "vmess", "vless", "trojan", "hysteria2", "tuic", "socks5", "http"}


def build_config(subscription):
    """Never inherit providers, scripts, listeners, controller or routing from a subscription."""
    if not isinstance(subscription, dict) or not isinstance(subscription.get("proxies"), list):
        raise TypeError("Clash node list required")
    nodes, names = [], set()
    for raw in subscription["proxies"]:
        if not isinstance(raw, dict) or raw.get("type") not in NODE_TYPES or set(raw) - NODE_FIELDS:
            raise ValueError("Unsupported node fields; review without printing credentials")
        if (not isinstance(raw.get("name"), str) or not raw["name"] or raw["name"] in names
                or not isinstance(raw.get("server"), str) or not raw["server"]
                or type(raw.get("port")) is not int or not 1 <= raw["port"] <= 65535
                or raw.get("skip-cert-verify", False)):
            raise ValueError("Invalid or TLS-unverified node")
        names.add(raw["name"])
        nodes.append(raw)
    if not nodes:
        raise ValueError("Subscription contains no supported nodes")
    return {"mixed-port": 7890, "bind-address": "127.0.0.1", "allow-lan": False,
            "mode": "rule", "log-level": "silent", "ipv6": False,
            "proxies": nodes, "proxy-groups": [{"name": "GOOGLE", "type": "url-test",
                "proxies": sorted(names), "url": "https://www.gstatic.com/generate_204",
                "interval": 300, "tolerance": 50, "lazy": False}],
            "rules": ["MATCH,GOOGLE"]}


def update(url_file, output, executable="/usr/local/bin/mihomo"):
    import requests
    import yaml
    url = Path(url_file).read_text("utf-8").strip()
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("HTTPS subscription required")
    response = requests.get(url, timeout=(5, 15), stream=True)
    response.raise_for_status()
    payload = bytearray()
    for chunk in response.iter_content(65536):
        payload.extend(chunk)
        if len(payload) > 2 * 1024 * 1024:
            raise ValueError("Subscription too large")
    config = build_config(yaml.safe_load(bytes(payload)))
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent,
                                     suffix=".yaml", delete=False) as handle:
        temporary = Path(handle.name)
        yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    try:
        checked = subprocess.run([executable, "-t", "-f", str(temporary)], capture_output=True,
                                 timeout=20, check=False)
        if checked.returncode:
            raise ValueError("Proxy configuration check failed")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscription-file", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mihomo", default="/usr/local/bin/mihomo")
    args = parser.parse_args()
    try:
        update(args.subscription_file, args.output, args.mihomo)
    except Exception:  # noqa: BLE001 - external parser/network exceptions may contain subscription secrets
        # Subscription URLs, node passwords and parser context must not reach logs.
        raise SystemExit("Proxy update failed; previous configuration retained") from None
    print("Proxy node configuration updated; restart the independent proxy service explicitly")


if __name__ == "__main__":
    main()
