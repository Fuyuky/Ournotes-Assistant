"""Read-only public browser: matching, live snapshots, malformed input and privacy."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from ournotes.accounts.browser import create_app


@pytest.fixture
def folders(tmp_path):
    accounts = tmp_path / "accounts"
    accounts.mkdir()
    catalog = tmp_path / "catalog"
    (catalog / "images").mkdir(parents=True)
    cards = []
    for kind in ("member", "support"):
        for card_id, rarity in ((1, "SSR"), (2, "SSR"), (3, "SR")):
            filename = f"images/{kind}-{card_id}.png"
            (catalog / filename).write_bytes(b"image")
            cards.append(
                {
                    "kind": kind,
                    "id": card_id,
                    "rarity": rarity,
                    "name": f"name-{kind}-{card_id}",
                    "title": f"title-{card_id}",
                    "image": filename,
                }
            )
    (catalog / "catalog.json").write_text(json.dumps({"cards": cards}), "utf-8")
    (catalog / "raw").mkdir()
    (catalog / "raw/MasterMemberCard.json").write_text(
        json.dumps(
            {
                "_allData": [
                    {"_id": 1, "_characterID": 1},
                    {"_id": 2, "_characterID": 6},
                    {"_id": 3, "_characterID": 2},
                ]
            }
        )
    )
    return accounts, catalog


def record(index, holdings=(("member", 1, 1),), **updates):
    totals = {kind: {"SSR": 0, "SR": 0, "R": 20} for kind in ("member", "support")}
    cards = []
    for kind, card_id, count in holdings:
        rarity = "SR" if card_id == 3 else "SSR"
        cards.append({"card": {"kind": kind, "id": card_id, "rarity": rarity}, "count": count})
        totals[kind][rarity] += count
    return {
        "schema_version": 1,
        "alias_index": index,
        "alias": "private-sentinel@example.test",
        "status": "complete",
        "pull_count": 43,
        "context": {"secret": "private-context-sentinel"},
        "totals": totals,
        "cards": cards,
        **updates,
    }


def write(folder, data, name=None):
    path = folder / (name or f"{data['alias_index']}.json")
    path.write_text(json.dumps(data), "utf-8")
    return path


def test_selected_count_ties_prefer_more_selected_kinds_before_price(folders):
    accounts, catalog = folders
    write(accounts, record(1, [('member', 1, 3), ('member', 2, 8)]))
    write(accounts, record(2, [('member', 1, 2), ('support', 1, 1)]))
    write(accounts, record(3, [('support', 1, 4)]))
    with TestClient(create_app(accounts, catalog)) as client:
        rows = client.get('/api/accounts', params={
            'cards': 'member:1,support:1,member:1', 'order': 'selected_desc',
            'allow_missing': True, 'include_pending': True,
        }).json()['accounts']
    assert [(a['index'], a['selected_count']) for a in rows] == [(3, 4), (2, 3), (1, 3)]


def test_selected_count_modes_count_duplicates_and_keep_other_filters(folders):
    accounts, catalog = folders
    write(accounts, record(1, [('member', 1, 3)]))
    write(accounts, record(2, [('member', 1, 1), ('support', 1, 1)]))
    write(accounts, record(3, [('support', 1, 1), ('member', 2, 10)]))
    write(accounts, record(4, [('member', 2, 20)]))
    write(accounts, record(5, [('member', 1, 8)], used=True))
    with TestClient(create_app(accounts, catalog)) as client:
        query = {'cards': 'member:1,support:1', 'order': 'selected_desc'}
        rows = client.get('/api/accounts', params=query).json()['accounts']
        assert [(r['index'], r['selected_count']) for r in rows] == [(2, 2)]
        query['allow_missing'] = True
        rows = client.get('/api/accounts', params=query).json()['accounts']
        assert [(r['index'], r['selected_count']) for r in rows] == [(1, 3), (2, 2), (3, 1)]
        query['include_used'] = True
        assert client.get('/api/accounts', params=query).json()['accounts'][0]['index'] == 5
        assert client.get('/api/accounts', params={'order': 'selected_desc'}).status_code == 422
        assert client.get('/api/accounts', params={'order': 'selected_any_desc'}).status_code == 422
        query['cards'] = 'member:1,member:1'
        rows = client.get('/api/accounts', params=query).json()['accounts']
        assert rows[0]['selected_count'] == 8


@pytest.mark.parametrize('order', ['price_desc', 'price_asc', 'mygo_desc', 'mujica_desc', 'selected_desc', 'desc', 'asc'])
def test_allow_missing_is_independent_of_sort_and_preserves_other_filters(folders, order):
    accounts, catalog = folders
    write(accounts, record(1, [('member', 1, 3)]))
    write(accounts, record(2, [('member', 1, 1), ('support', 1, 1)]))
    write(accounts, record(3, [('support', 1, 1), ('member', 2, 2)], pull_count=62))
    write(accounts, record(4, [('member', 2, 1)]))
    write(accounts, record(5, [('member', 1, 8)], used=True))
    with TestClient(create_app(accounts, catalog)) as client:
        query = {'cards': 'member:1,support:1', 'order': order}

        def ids(**extra):
            response = client.get('/api/accounts', params={**query, **extra})
            assert response.status_code == 200
            return {a['index'] for a in response.json()['accounts']}

        assert ids() == {2}
        assert ids(allow_missing=True) == {1, 2, 3}
        assert ids(allow_missing=True, include_used=True) == {1, 2, 3, 5}
        assert ids(allow_missing=True, pull_count=43) == {1, 2}
        assert ids(allow_missing=True, index=3) == {3}
        assert ids(allow_missing=True, index=4) == set()
        if order != 'selected_desc':
            assert ids(allow_missing=True, cards='') == {1, 2, 3, 4}


def test_band_orders_count_distinct_ssr_characters_then_price_and_index(folders):
    accounts, catalog = folders
    data = json.loads((catalog / 'catalog.json').read_text('utf-8'))
    mapping = json.loads((catalog / 'raw/MasterMemberCard.json').read_text('utf-8'))
    for card_id, character in ((4, 1), (5, 2), (6, 7)):
        image = f'images/member-{card_id}.png'
        (catalog / image).write_bytes(b'image')
        data['cards'].append({'kind': 'member', 'id': card_id, 'rarity': 'SSR',
                              'name': f'character-{character}', 'title': f'variant-{card_id}', 'image': image})
        mapping['_allData'].append({'_id': card_id, '_characterID': character})
    (catalog / 'catalog.json').write_text(json.dumps(data), 'utf-8')
    (catalog / 'raw/MasterMemberCard.json').write_text(json.dumps(mapping), 'utf-8')
    write(accounts, record(1, [('member', 1, 3), ('member', 4, 2), ('support', 1, 8)]))
    write(accounts, record(2, [('member', 1, 1), ('member', 5, 1)]))
    write(accounts, record(3, [('member', 2, 1), ('member', 6, 1)]))
    write(accounts, record(4, [('member', 2, 3)]))
    write(accounts, record(5, [('member', 1, 1), ('member', 5, 1), ('member', 2, 1), ('support', 1, 8)]))
    write(accounts, record(6, [('support', 1, 10)]))
    write(accounts, record(7, [('member', 1, 1), ('member', 5, 1)]))
    with TestClient(create_app(accounts, catalog)) as client:
        mygo = client.get('/api/accounts', params={'order': 'mygo_desc'}).json()['accounts']
        mujica = client.get('/api/accounts', params={'order': 'mujica_desc'}).json()['accounts']
        assert [a['index'] for a in mygo] == [5, 2, 7, 1, 6, 3, 4]
        assert [a['index'] for a in mujica] == [3, 5, 4, 1, 6, 2, 7]
        assert next(a for a in mygo if a['index'] == 1)['mygo_characters'] == 1
        assert next(a for a in mygo if a['index'] == 6)['mygo_characters'] == 0


def test_matching_identity_duplicates_and_projection(folders):
    accounts, catalog = folders
    write(accounts, record(1, [("member", 1, 2), ("support", 1, 1), ("member", 3, 8)]))
    write(accounts, record(2, [("member", 1, 1), ("member", 1, 1), ("member", 2, 1)]))
    write(accounts, record(3, [("support", 1, 4)]))
    write(accounts, record(4, []))
    with TestClient(create_app(accounts, catalog)) as client:
        data = client.get("/api/accounts").json()
        assert [a["index"] for a in data["accounts"]] == [1, 2, 3, 4]
        assert data["meta"]["valid_accounts"] == 4
        assert set(data["accounts"][0]) == {
            'activity',
            "index",
            "used",
            "pull_count",
            "ssr_total",
            "sr_total",
            "cards",
            "mygo_characters",
            "mujica_characters",
            "crychic_characters",
            "dream_characters",
            "band_capacity",
            "badges",
            "price",
            "unique_characters",
            "dream_pickup_count",
            "pool_keys",
            "millsage_pickup_count",
            "millsage_characters",
            "dumb_characters",
        }
        assert "private-" not in json.dumps(data)
        assert [
            a["index"] for a in client.get("/api/accounts", params={"cards": "member:1"}).json()["accounts"]
        ] == [1, 2]
        both = client.get("/api/accounts", params={"cards": "member:1,support:1"}).json()
        assert [a["index"] for a in both["accounts"]] == [1]
        assert both["accounts"][0]["ssr_total"] == 3
        assert both["accounts"][0]["cards"] == [
            {"key": "member:1", "count": 2},
            {"key": "support:1", "count": 1},
        ]
        multiple = client.get("/api/accounts", params={"cards": "member:1,member:2"}).json()
        assert multiple["accounts"][0]["cards"][0]["count"] == 2
        empty = client.get("/api/accounts", params={"cards": "member:2,support:2"}).json()
        assert empty["total"] == 0 and empty["page"] == empty["pages"] == 1
        assert len(client.get("/api/cards").json()["cards"]) == 4


def test_price_orders_pending_first_and_preserve_ties(folders):
    accounts, catalog = folders
    for number, count in ((1, 15), (2, 12), (3, 3), (4, 2)):
        write(accounts, record(number, [("support", 1, count)], pull_count=62))
    with TestClient(create_app(accounts, catalog)) as client:
        assert [a["index"] for a in client.get("/api/accounts").json()["accounts"]] == [2, 3, 4]
        assert client.get("/api/accounts?index=1").json()["total"] == 1
        descending = client.get("/api/accounts?include_pending=true").json()["accounts"]
        ascending = client.get("/api/accounts?order=price_asc&include_pending=true").json()["accounts"]
        assert [a["index"] for a in descending] == [1, 2, 3, 4]
        assert [a["index"] for a in ascending] == [1, 3, 4, 2]
        assert descending[0]["price"]["status"] == "pending"
        assert [a["price"]["amount_cents"] for a in descending] == [None, 2000, 1000, 1000]
        assert client.get("/api/accounts?order=rarity_desc").status_code == 422


def test_pending_visibility_combines_with_used_cards_index_and_budget(folders):
    accounts, catalog = folders
    write(accounts, record(1, [("support", 1, 15)], used=True, pull_count=62))
    write(accounts, record(2, [("member", 1, 2)], pull_count=None))
    write(accounts, record(3, [("member", 1, 1)], pull_count=43))
    with TestClient(create_app(accounts, catalog)) as client:
        def ids(**params):
            data = client.get("/api/accounts", params=params).json()
            return [a["index"] for a in data["accounts"]]

        assert ids(include_used=True) == [1, 3]
        assert ids(include_pending=True) == [2, 3]
        assert ids(include_pending=True, include_used=True) == [1, 2, 3]
        assert ids(index=1) == [1]
        assert ids(index=2) == [2]
        assert ids(index=1, include_pending=True) == [1]
        assert ids(index=1, include_used=True) == [1]
        assert ids(index=1, include_pending=True, include_used=True) == [1]
        assert ids(include_pending=True, cards="member:1", pull_count=43) == [3]
        assert ids(include_pending=True, cards="member:1", order="selected_desc") == [2, 3]
        assert ids(include_pending=True, include_used=True, pull_count=62) == [1]
        assert ids(include_pending=False, include_used=True, pull_count=62) == [1]


@pytest.mark.parametrize("size", [10, 15, 20])
def test_paging_sorting_and_clamp(folders, size):
    accounts, catalog = folders
    for i in range(1, 33):
        write(accounts, record(i, [("member", 1, 1 + i % 4)]))
    with TestClient(create_app(accounts, catalog)) as client:
        for order in ("asc", "desc"):
            expected = sorted(range(1, 33), key=lambda i: ((1 if order == "asc" else -1) * (1 + i % 4), i))
            found = []
            for page in range(1, (32 + size - 1) // size + 1):
                data = client.get(
                    "/api/accounts", params={"page_size": size, "page": page, "order": order}
                ).json()
                assert data["page"] == page and data["total"] == 32
                found.extend(a["index"] for a in data["accounts"])
            assert found == expected
        data = client.get("/api/accounts", params={"page": 999, "page_size": size}).json()
        assert data["page"] == data["pages"] == (32 + size - 1) // size


@pytest.mark.parametrize(
    "issue",
    [
        "negative",
        "zero",
        "float",
        "bool",
        "totals",
        "sr_totals",
        "unknown",
        "partial",
        "cancelled",
        "skipped",
        "unknown_slots",
        "schema",
        "cards",
        "index",
        "root",
        "malformed",
    ],
)
def test_invalid_archives_excluded(folders, issue):
    accounts, catalog = folders
    data = record(1)
    if issue in ("negative", "zero", "float", "bool"):
        data["cards"][0]["count"] = {"negative": -1, "zero": 0, "float": 1.0, "bool": True}[issue]
    elif issue == "totals":
        data["totals"]["support"]["SSR"] = 1
    elif issue == "sr_totals":
        data["totals"]["member"]["SR"] = 1
    elif issue == "unknown":
        data["cards"][0]["card"]["id"] = 99
    elif issue in ("partial", "cancelled"):
        data["status"] = issue
    elif issue == "skipped":
        data["skipped"] = ["unresolved"]
    elif issue == "unknown_slots":
        data["unknown_slots"] = 1
    elif issue == "schema":
        data["schema_version"] = 99
    elif issue == "cards":
        data["cards"] = [None]
    elif issue == "index":
        data["alias_index"] = True
    elif issue == "root":
        data = []
    (accounts / "bad.json").write_text("{broken" if issue == "malformed" else json.dumps(data), "utf-8")
    write(accounts, record(2))
    with TestClient(create_app(accounts, catalog)) as client:
        data = client.get("/api/accounts").json()
        assert data["meta"]["excluded_files"] == 1
        assert [a["index"] for a in data["accounts"]] == [2]


def test_directory_changes_duplicates_and_recovery(folders):
    accounts, catalog = folders
    path = write(accounts, record(1))
    app = create_app(accounts, catalog)
    with TestClient(app) as client:
        initial = client.get("/api/accounts").json()["meta"]
        write(accounts, record(2))
        app.state.index.refresh()
        assert client.get("/api/accounts").json()["total"] == 2
        duplicate = write(accounts, record(1, status="partial"), "duplicate.json")
        app.state.index.refresh()
        data = client.get("/api/accounts").json()
        assert data["total"] == 1 and data["meta"]["excluded_files"] == 2
        assert data["accounts"][0]["index"] == 2
        duplicate.unlink()
        path.write_text("broken", "utf-8")
        app.state.index.refresh()
        assert client.get("/api/accounts").json()["meta"]["excluded_files"] == 1
        path.unlink()
        app.state.index.refresh()
        data = client.get("/api/accounts").json()
        assert data["total"] == 1 and data["meta"]["files"] == 1 and data["meta"]["excluded_files"] == 0
        assert (
            data["meta"]["revision"] > initial["revision"]
            and data["meta"]["loaded_at"] > initial["loaded_at"]
        )
        relocated = accounts.rename(accounts.with_name("unavailable"))
        app.state.index.refresh()
        data = client.get("/api/accounts").json()
        assert data["total"] == 0 and data["meta"]["available"] is False
        relocated.rename(accounts)
        app.state.index.refresh()
        assert client.get("/api/accounts").json()["total"] == 1


def test_automatic_reload_and_atomic_publish(folders, monkeypatch):
    accounts, catalog = folders
    write(accounts, record(1))
    app = create_app(accounts, catalog, refresh_seconds=0.025)
    with TestClient(app) as client:
        write(accounts, record(2))
        deadline = time.monotonic() + 3
        while client.get("/api/accounts").json()["total"] != 2 and time.monotonic() < deadline:
            time.sleep(0.025)
        assert client.get("/api/accounts").json()["total"] == 2
    index = app.state.index
    before = index.snapshot
    write(accounts, record(3))
    original = index.account

    def concurrent_change(data):
        (accounts / "2.json").unlink(missing_ok=True)
        return original(data)

    monkeypatch.setattr(index, "account", concurrent_change)
    index.refresh()
    assert index.snapshot is before  # Never publish a partially scanned directory.
    monkeypatch.setattr(index, "account", original)
    index.refresh()
    assert {a["index"] for a in index.snapshot.accounts} == {1, 3}


def test_public_routes_privacy_and_path_restrictions(folders, monkeypatch):
    accounts, catalog = folders
    write(accounts, record(1))
    app = create_app(accounts, catalog)
    with TestClient(app, raise_server_exceptions=False) as client:
        for path in ("/", "/static/app.js", "/static/style.css", "/api/cards", "/api/accounts"):
            response = client.get(path)
            assert response.status_code == 200
            assert "private-" not in response.text
            assert str(accounts) not in response.text
            assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert client.get("/art/member/1").status_code == 200
        for path in (
            "/art/member/3",
            "/static/1.json",
            "/results/archive/accounts/1.json",
            "/static/%2e%2e%2faccounts%2f1.json",
            "/art/member/%2e%2e%2f1.json",
            "/static/%2e%2e/%2e%2e/HANDOFF.md",
            "/api/accounts/1",
            "/openapi.json",
        ):
            response = client.get(path)
            assert response.status_code in (404, 422)
            assert "private-" not in response.text and str(accounts) not in response.text
        for params in (
            {"cards": "private-sentinel@example.test"},
            {"page": "private-sentinel@example.test"},
            {"page_size": 12},
            {"page_size": "private-sentinel@example.test"},
            {"page": 0},
            {"order": "private-sentinel@example.test"},
            {"cards": "member:3"},
        ):
            response = client.get("/api/accounts", params=params)
            assert response.status_code == 422 and "private-" not in response.text
        monkeypatch.setattr(app.state.index, "snapshot", None)
        response = client.get("/api/accounts")
        assert response.status_code == 500 and "private-" not in response.text
