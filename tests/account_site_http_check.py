"""Verify an installed nginx site via HTTP, using only its public manifest."""

import hashlib
import http.client
import json
import sys
from pathlib import Path

from ournotes.accounts.pricing import MODEL


def run(manifest_path, connect_host="127.0.0.1"):
    manifest = json.loads(Path(manifest_path).read_text())
    connection = http.client.HTTPConnection(connect_host, 80, timeout=10)

    def get(path):
        connection.request("GET", path, headers={"Host": "39.106.140.66"})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    snapshot = None
    for name, entry in manifest.items():
        status, headers, body = get("/" + name)
        assert status == 200, (name, status)
        assert hashlib.sha256(body).hexdigest() == entry["sha256"], name
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "default-src 'self'" in headers["Content-Security-Policy"]
        if name == "data/current.json":
            assert headers["Cache-Control"] == "no-store"
        elif name.startswith("data/snapshot."):
            snapshot = json.loads(body)
            assert "immutable" in headers["Cache-Control"]
    assert snapshot is not None
    for account in snapshot["accounts"]:
        assert set(account) == {
            "index",
            "cards",
            "ssr_total",
            "sr_total",
            "used",
            "pull_count",
            "mygo_characters",
            "mujica_characters",
            "crychic_characters",
            "dream_characters",
            "band_capacity",
            "badges",
            "price",
            "unique_characters",
            "dream_pickup_count",
            "activity",
        }
        assert type(account["used"]) is bool
        price = account['price']
        assert set(price) == {'model', 'currency', 'status', 'amount_cents', 'reason'}
        assert price['currency'] == 'CNY' and price['model'] == MODEL
        activity = account['activity']
        if activity is not None:
            assert set(activity) == {'id', 'label', 'up_count', 'up_total'}
            assert type(activity['up_total']) is int and activity['up_total'] > 0
            assert type(activity['up_count']) is int and 0 <= activity['up_count'] <= activity['up_total']
        if price['status'] == 'priced':
            assert type(price['amount_cents']) is int and price['amount_cents'] >= 1000
        else:
            assert price['status'] == 'pending' and price['amount_cents'] is None
        assert sum(card["count"] for card in account["cards"]) == account["ssr_total"]
        for card in account["cards"]:
            assert set(card) == {"key", "count"}
    assert snapshot["meta"]["valid_accounts"] == sum(not a["used"] for a in snapshot["accounts"])
    assert snapshot["meta"]["used_accounts"] == sum(a["used"] for a in snapshot["accounts"])
    if snapshot['meta'].get('public_account_limit'):
        assert len(snapshot['accounts']) <= snapshot['meta']['public_account_limit']
    for card in snapshot["cards"]:
        assert set(card) == {"key", "id", "kind", "rarity", "name", "title", "image"}
        assert card["rarity"] == "SSR"
    assert "@" not in json.dumps(snapshot, ensure_ascii=False)
    denied = [
        "/.ournotes-release",
        "/.ournotes-static-site",
        "/releases/",
        "/current/",
        "/results/archive/accounts/1.json",
        "/accounts/1.json",
        "/api/accounts",
        "/data/",
        "/art/",
        "/brand/",
        "/raw/MasterMemberCard.json",
        "/data/runtime.sqlite3",
        "/brand/../../etc/passwd",
        "/static/app.js",
        "/etc/passwd",
        "/../etc/passwd",
        "/%2e%2e/etc/passwd",
        "/art/../../etc/passwd",
        "/art/%2e%2e/%2e%2e/etc/passwd",
        "/art/%2e%2e%2f%2e%2e%2fetc/passwd",
    ]
    for path in denied:
        status, _, _ = get(path)
        assert status in (400, 404), (path, status)
    connection.close()
    print(
        json.dumps(
            {
                "connect_host": connect_host,
                "public_files_verified": len(manifest),
                "accounts": len(snapshot["accounts"]),
                "ssr_artwork": len(snapshot["cards"]),
                "denied_paths_verified": len(denied),
                "privacy": "explicit public fields only",
                "headers": "passed",
            }
        )
    )


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1")
