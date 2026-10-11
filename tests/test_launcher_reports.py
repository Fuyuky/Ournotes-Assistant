"""The console delegates account statistics to the authoritative cloud portal."""
import hashlib

import pytest
from test_launcher_web import setup as launcher_setup

setup_fixture = pytest.fixture(name='setup')(launcher_setup.__wrapped__)


def test_statistics_entry_opens_server_and_local_generation_is_removed(setup):
    client, _, root, spawned, _ = setup
    path = root / 'state/runtime.sqlite3'
    before = hashlib.sha256(path.read_bytes()).digest()
    page = client.get('/').text
    assert 'data-server-page="statistics"' in page
    assert 'view-statistics' not in page and '/static/report.js' not in page
    assert client.get('/api/launcher/statistics?scope=all').status_code == 404
    assert client.post('/api/launcher/action', json={'action': 'report'}).status_code == 422
    assert not spawned and hashlib.sha256(path.read_bytes()).digest() == before


def test_confirmation_and_processed_counts_remain_distinct(setup):
    client, _, _, _, _ = setup
    lane = client.get('/api/launcher/status').json()['lanes'][0]
    assert lane['complete'] == 1 and lane['processed'] == 2
    assert lane['legacy_recorded_complete'] == 1
