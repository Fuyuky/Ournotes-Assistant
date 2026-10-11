import json

import pytest
from fastapi.testclient import TestClient
from test_account_browser import folders as build_folders
from test_account_browser import record, write

from ournotes.accounts.browser import create_app
from ournotes.accounts.site import export_site


@pytest.fixture
def folders(tmp_path):
    return build_folders.__wrapped__(tmp_path)


def test_used_index_price_sort_and_static_projection(folders, tmp_path):
    directory, catalog = folders
    write(directory, record(1, [("member", 1, 1), ("member", 2, 1)], used=True))
    write(directory, record(2, [("support", 1, 8)]))
    write(directory, record(3, [("member", 1, 1)]))
    write(directory, record(4, [("member", 1, 1), ("member", 3, 2)]))
    with TestClient(create_app(directory, catalog)) as client:
        data = client.get("/api/accounts?order=price_desc").json()
        assert [a["index"] for a in data["accounts"]] == [2, 3, 4]
        assert data["meta"]["valid_accounts"] == 3 and data["meta"]["used_accounts"] == 1
        assert data["accounts"][0]["price"]["amount_cents"] == 1500
        all_data = client.get("/api/accounts?order=price_desc&include_used=true").json()
        assert [a["index"] for a in all_data["accounts"]] == [1, 2, 3, 4]
        assert client.get("/api/accounts?index=1").json()["total"] == 1
        found = client.get("/api/accounts?index=0001&include_used=true").json()
        assert found["total"] == 1 and found["accounts"][0]["used"]
        assert client.get("/api/accounts?index=4&cards=support:1").json()["total"] == 0
        for invalid in ("-1", "1.2", "abc", "9007199254740992", "１２"):
            assert client.get("/api/accounts", params={"index": invalid}).status_code == 422
        for url in ("/logos/../../state/runtime.sqlite3", "/logos/nope", "/logos/mygo.png"):
            assert client.get(url).status_code == 404
        for band in client.get("/api/cards").json()["bands"].values():
            if 'logo' in band:
                assert client.get(band["logo"]).status_code == 200
    out = tmp_path / "site"
    exported = export_site(directory, catalog, out)
    payload = json.loads((out / f"data/snapshot.{exported['revision']}.json").read_text("utf-8"))
    assert payload["meta"]["used_accounts"] == 1 and len(payload["accounts"]) == 4
    assert (
        payload["accounts"][0]["used"]
        and payload["accounts"][0]["price"] == all_data["accounts"][0]["price"]
    )
    assert all((out / b["logo"]).is_file() for b in payload["bands"].values() if 'logo' in b)
    assert "private-" not in json.dumps(payload)


def test_invalid_used_boolean_is_excluded_and_refresh_updates(folders):
    directory, catalog = folders
    write(directory, record(1, used="true"))
    app = create_app(directory, catalog)
    with TestClient(app) as client:
        assert client.get("/api/accounts").json()["total"] == 0
        write(directory, record(1, used=False))
        app.state.index.refresh()
        first = client.get("/api/accounts").json()
        assert first["total"] == 1
        write(directory, record(1, used=True))
        app.state.index.refresh()
        after = client.get("/api/accounts").json()
        assert after["total"] == 0 and after["meta"]["used_accounts"] == 1


def test_pull_metadata_is_typed_and_retired_private_flags_are_not_public(folders):
    directory, catalog = folders
    write(directory, record(508, used=True, owner_use=True, pull_count=43))
    write(directory, record(1, pull_count=True))
    write(directory, record(2, used="true"))
    with TestClient(create_app(directory, catalog)) as client:
        data = client.get("/api/accounts?include_used=true").json()
        assert data["total"] == 1 and data["meta"]["excluded_files"] == 2
        account = data["accounts"][0]
        assert account["index"] == 508 and account["pull_count"] == 43 and "owner_use" not in account


def test_pull_count_filter_combines_with_cards_used_index_and_pagination(folders):
    directory, catalog = folders
    for i in range(1, 15):
        write(directory, record(i, [('member', 1, 1)], pull_count=62, used=i == 1))
    write(directory, record(20, [('member', 1, 1)], pull_count=43))
    write(directory, record(21, [('member', 1, 1)], pull_count=None))
    with TestClient(create_app(directory, catalog)) as client:
        found = client.get('/api/accounts?pull_count=62&cards=member:1&page=2').json()
        assert found['total'] == 13 and len(found['accounts']) == 3
        assert client.get('/api/accounts?pull_count=62&index=20').json()['total'] == 0
        assert client.get('/api/accounts?pull_count=62&index=1&include_used=true').json()['total'] == 1
        assert client.get('/api/accounts?pull_count=43').json()['total'] == 1
        assert client.get('/api/accounts?pull_count=63').status_code == 422
