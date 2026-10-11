"""Publication privacy and deterministic incremental export; no real archives modified."""

import json

import pytest
from test_account_browser import folders as build_folders
from test_account_browser import record, write

from ournotes.accounts.pricing import price_sort_key
from ournotes.accounts.site import export_site


@pytest.fixture
def folders(tmp_path):
    return build_folders.__wrapped__(tmp_path)


def snapshot(folder):
    revision = json.loads((folder / "data/current.json").read_text())["revision"]
    return json.loads((folder / f"data/snapshot.{revision}.json").read_text("utf-8"))


def test_announcement_version_ignores_accounts_but_tracks_content_and_art(folders, tmp_path):
    from PIL import Image

    accounts, catalog = folders
    output = tmp_path / 'site'
    write(accounts, record(1))
    export_site(accounts, catalog, output)
    initial = snapshot(output)['announcement']
    assert 'millsage' in initial['content']
    write(accounts, record(2))
    export_site(accounts, catalog, output)
    assert snapshot(output)['announcement'] == initial
    Image.new('RGB', (40, 40), 'orange').save(catalog / 'images/support-2.png')
    export_site(accounts, catalog, output)
    updated = snapshot(output)['announcement']
    assert updated['version'] != initial['version']
    export_site(accounts, catalog, output, announcement='新的公告')
    assert snapshot(output)['announcement']['version'] != updated['version']
    assert snapshot(output)['announcement']['content'] == '新的公告'
    export_site(accounts, catalog, output, announcement='')
    assert snapshot(output)['announcement']['content'] == ''


def test_export_privacy_and_identical_repeat(folders, tmp_path):
    accounts, catalog = folders
    write(accounts, record(1, [("member", 1, 2), ("support", 2, 1)]))
    write(accounts, record(2, status="partial"))
    output = tmp_path / "site"
    first = export_site(accounts, catalog, output)
    assert (first["accounts"], first["excluded"], first["images"]) == (1, 1, 4)
    data = snapshot(output)
    assert data["accounts"][0]["ssr_total"] == 3
    for name in first["files"]:
        assert "private-" not in (output / name).read_bytes().decode("utf-8", errors="ignore")
    assert "/api/" not in next(
        (output / name).read_text("utf-8") for name in first["files"] if "data-source." in name
    )
    second = export_site(accounts, catalog, output)
    assert second["written"] == second["deleted"] == 0
    assert (
        second["files"] == first["files"]
        and snapshot(output)["meta"]["loaded_at"] == data["meta"]["loaded_at"]
    )
    assert second["files"] == first["files"]


def test_add_remove_accounts_and_retire_artwork(folders, tmp_path):
    accounts, catalog = folders
    write(accounts, record(1))
    write(accounts, record(2, [("support", 2, 1)]))
    output = tmp_path / "site"
    first = export_site(accounts, catalog, output)
    (accounts / "1.json").unlink()
    write(accounts, record(3))
    second = export_site(accounts, catalog, output)
    changed = [name for name, sha in second["files"].items() if first["files"].get(name) != sha]
    assert len(changed) == 2  # Pointer and public snapshot only; all artwork reused.
    assert all(name.startswith("data/") for name in changed)
    assert [a["index"] for a in snapshot(output)["accounts"]] == [2, 3]
    data = json.loads((catalog / "catalog.json").read_text())
    data["cards"] = [c for c in data["cards"] if (c["kind"], c["id"]) != ("support", 2)]
    (catalog / "catalog.json").write_text(json.dumps(data))
    (catalog / "images/support-2.png").unlink()
    third = export_site(accounts, catalog, output)
    assert [a["index"] for a in snapshot(output)["accounts"]] == [3]
    assert third["excluded"] == 1 and third["images"] == 3
    removed = set(second["files"]) - set(third["files"])
    assert any(name.startswith("art/support-2.") for name in removed)
    assert all(not (output / name).exists() for name in removed)


def test_invalid_archive_duplicate_and_empty_guard(folders, tmp_path):
    accounts, catalog = folders
    write(accounts, record(1))
    output = tmp_path / "site"
    first = export_site(accounts, catalog, output)
    write(accounts, record(1), "duplicate.json")
    with pytest.raises(ValueError, match="No valid"):
        export_site(accounts, catalog, output)
    assert snapshot(output)["revision"] == first["revision"]
    empty = export_site(accounts, catalog, output, allow_empty=True)
    assert empty["accounts"] == 0 and empty["excluded"] == 2
    accounts.rename(tmp_path / "missing")
    with pytest.raises(ValueError, match="unavailable"):
        export_site(accounts, catalog, output, allow_empty=True)


def test_refuse_unmanaged_output_and_source_overlap(folders, tmp_path):
    accounts, catalog = folders
    write(accounts, record(1))
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep.txt").write_text("keep")
    with pytest.raises(ValueError, match="unmanaged"):
        export_site(accounts, catalog, output)
    assert (output / "keep.txt").read_text() == "keep"
    for directory in (accounts, catalog, tmp_path):
        with pytest.raises(ValueError, match="overlap"):
            export_site(accounts, catalog, directory)




def test_mapping_change_during_export_is_rejected(folders, tmp_path, monkeypatch):
    accounts, catalog = folders
    write(accounts, record(1))
    from ournotes.accounts.index import AccountIndex

    original = AccountIndex.refresh

    def change_mapping(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        mapping = catalog / "raw/MasterMemberCard.json"
        mapping.write_text(mapping.read_text() + "\n")
        return result

    monkeypatch.setattr(AccountIndex, "refresh", change_mapping)
    output = tmp_path / "site"
    with pytest.raises(ValueError, match="Source changed"):
        export_site(accounts, catalog, output)
    assert not output.exists()


def test_public_limit_filters_before_ranking_and_preserves_local(folders, tmp_path):
    accounts, catalog = folders
    # A used high-price account cannot consume a slot; prices tied at the cutoff
    # must use ascending index, just like the page's price sort.
    for number in range(1, 306):
        write(accounts, record(number, [("member", 1, 1)], used=number == 1))
    write(accounts, record(900, [("member", 1, 9)], used=True))
    write(accounts, record(800, [("member", 1, 12)]))
    write(accounts, record(700, [("member", 1, 20)], pull_count=None))
    local, public = tmp_path / "local", tmp_path / "public"
    export_site(accounts, catalog, local)
    local_data = snapshot(local)
    expected = sorted(
        (a for a in local_data["accounts"] if not a["used"]),
        key=price_sort_key,
    )[:300]
    first = export_site(accounts, catalog, public, public_account_limit=300)
    data = snapshot(public)
    assert first["accounts"] == len(data["accounts"]) == 300
    assert {a["index"] for a in data["accounts"]} == {a["index"] for a in expected}
    assert {a["index"] for a in data["accounts"]} == {*range(2, 300), 700, 800}
    assert all(not a["used"] for a in data["accounts"])
    assert all(set(a["price"]) == {"model", "currency", "status", "amount_cents", "reason"} for a in data["accounts"])
    assert data["meta"]["public_account_limit"] == 300
    assert data["meta"]["used_accounts"] == 0
    assert data["meta"]["valid_accounts"] == data["meta"]["total_valid_accounts"] == 300
    assert '<body class="public-catalog">' in (public / "index.html").read_text("utf-8")
    assert '<body>' in (local / "index.html").read_text("utf-8")
    assert len(local_data["accounts"]) == 308
    assert "public_account_limit" not in local_data["meta"]
    assert local_data["meta"]["used_accounts"] == 2
    assert all("rarity" not in a for a in local_data["accounts"])
    assert export_site(accounts, catalog, public, public_account_limit=300)["written"] == 0

    # Marking a displayed account used replaces it with the next eligible account.
    write(accounts, record(800, [("member", 1, 12)], used=True))
    second = export_site(accounts, catalog, public, public_account_limit=300)
    assert {a["index"] for a in snapshot(public)["accounts"]} == {*range(2, 301), 700}
    assert all(name.startswith("data/") for name, sha in second["files"].items() if sha != first["files"].get(name))


def test_public_limit_sold_out_and_custom_limit(folders, tmp_path):
    accounts, catalog = folders
    write(accounts, record(1, used=True))
    output = tmp_path / "site"
    # A validated inventory that is sold out must remove the last public account.
    assert export_site(accounts, catalog, output, public_account_limit=300)["accounts"] == 0
    assert snapshot(output)["meta"]["valid_accounts"] == 0
    write(accounts, record(2))
    write(accounts, record(3))
    assert export_site(accounts, catalog, output, public_account_limit=1)["accounts"] == 1
    assert snapshot(output)["accounts"][0]["index"] == 2
    for value in (0, -1, True, "300"):
        with pytest.raises(ValueError, match="positive integer"):
            export_site(accounts, catalog, output, public_account_limit=value)


def test_public_all_accounts_includes_used_and_unknown_score_without_private_fields(folders, tmp_path):
    accounts, catalog = folders
    for number in range(1, 305):
        write(accounts, record(number, used=number == 1))
    write(accounts, record(900, pull_count=None, used=True))
    output = tmp_path / 'all'
    exported = export_site(accounts, catalog, output, public_catalog=True)
    data = snapshot(output)
    assert exported['accounts'] == 305 and data['meta']['used_accounts'] == 2
    assert data['meta']['public_catalog'] and data['meta']['public_account_limit'] is None
    assert data['accounts'][-1]['price']['status'] == 'pending'
    raw = json.dumps(data)
    for secret in ('alias', 'account_id', 'node_id', 'context', 'private-', 'local_preset', 'used_pending'):
        assert secret not in raw
