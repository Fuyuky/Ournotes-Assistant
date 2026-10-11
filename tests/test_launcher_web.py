"""Real HTTP/application flow with synthetic state and fake subprocesses; never start a game."""

import hashlib
import io
import json
import sqlite3

import pytest


def test_manual_update_and_notification_routes(setup, monkeypatch):
    client, _, _, _, _ = setup
    launcher = client.app.state.launcher
    calls = []
    monkeypatch.setattr(launcher.game_update, 'start', lambda *args: calls.append(args) or {'status': 'running'})
    page = client.get('/').text
    assert 'game-update-form' in page and 'background-notifications' in page
    assert client.get('/static/maintenance.js').status_code == 200
    assert client.get('/api/launcher/maintenance').status_code == 200 and not calls
    request = {'instances': [1], 'request_id': 'a' * 32}
    assert client.post('/api/launcher/game-update', json=request, headers={'x-launcher-token': 'wrong'}).status_code == 403
    assert not calls
    assert client.post('/api/launcher/game-update', json={**request, 'instances': [True]}).status_code == 422
    assert client.post('/api/launcher/game-update', json=request).status_code == 200
    assert len(calls) == 1
    result = client.post('/api/launcher/notifications/settings', json={'enabled': False})
    assert result.status_code == 200 and result.json()['enabled'] is False
    assert client.post('/api/launcher/notifications/test', json={}).status_code == 409


def test_game_update_blocks_console_close_and_runtime_actions(setup, monkeypatch):
    client, _, _, _, _ = setup
    launcher = client.app.state.launcher
    monkeypatch.setattr(launcher.game_update, 'busy', lambda: True)
    client.app.state.shutdown = lambda: pytest.fail('must not shut down during install')
    assert client.post('/api/launcher/shutdown', json={}).status_code == 409
    assert client.post('/api/launcher/updates/install', json={}).status_code == 409
    with pytest.raises(ValueError, match='游戏更新'):
        launcher.dispatch('resume', {})
from fastapi.testclient import TestClient
from test_account_browser import folders as build_folders
from test_account_browser import record, write

from ournotes.control.launcher import JobManager, Launcher
from ournotes.control.web import create_app


class FakeProcess:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


def test_console_shutdown_requires_token_and_calls_server(setup):
    client, _, _, _, _ = setup
    calls = []
    client.app.state.shutdown = lambda: calls.append('shutdown')
    assert '关闭控制台' in client.get('/').text
    response = client.post('/api/launcher/shutdown', headers={'X-Launcher-Token': 'wrong'}, json={})
    assert response.status_code == 403 and calls == []
    response = client.post('/api/launcher/shutdown', json={})
    assert response.status_code == 200 and calls == ['shutdown']


def test_debug_report_requires_recorded_debug_batch(setup):
    client, _, root, _, _ = setup
    assert client.get('/api/launcher/debug-report').status_code == 404
    with sqlite3.connect(root / 'state/runtime.sqlite3') as db:
        value = json.loads(db.execute('SELECT data FROM batches').fetchone()[0])
        value['debug_mode'] = True
        db.execute('UPDATE batches SET data=? WHERE id=?', (json.dumps(value), value['id']))
    folder = root / 'results/batches' / value['id']
    folder.mkdir(parents=True, exist_ok=True)
    (folder / '完整分步骤用时.md').write_text('调试步骤', encoding='utf-8')
    assert client.get('/api/launcher/debug-report').text == '调试步骤'


@pytest.mark.parametrize('action', ['start', 'resume'])
def test_conflicting_rebind_instance_rejected_before_spawning_job(setup, action):
    from ournotes.common.devices import rebind_lease
    client, _, root, calls, _ = setup
    if action == 'start':
        with sqlite3.connect(root / 'state/runtime.sqlite3') as db:
            db.execute('DELETE FROM batches')
            db.execute('DELETE FROM runs')
        values = {'action': 'start', 'run': {'lanes': [{'instance': 0, 'preset': 'MyGO only'}], 'count': 1, 'preset_version': 0}}
    else:
        values = {'action': 'resume', 'batch_id': 'b' * 32}
    with rebind_lease(root, 0):
        response = client.post('/api/launcher/action', json=values)
    assert response.status_code == 409 and '实例 0' in response.text
    assert not calls


def test_account_action_buttons_load_confirmation_assets(setup):
    client, _, _, _, _ = setup
    html = client.get('/').text
    assert '/static/account-actions.js' in html and 'id="account-action-progress"' in html
    response = client.get('/static/account-actions.js')
    assert response.status_code == 200
    assert 'createAccountActions' in response.text
    assert '>标记已使用</button>' in html and '>标记已拍下</button>' in html
    assert '>账号状态</button>' not in html
    assert client.get('/static/used-flow.js').status_code == 404
    headers = {'X-Launcher-Token': client.app.state.token}
    for path in ('used-preview', 'set-used'):
        assert client.post('/api/launcher/accounts/' + path, json={'index': 1}, headers=headers).status_code == 405


def test_completed_lane_does_not_display_old_pause_error(setup):
    client, _, root, _, _ = setup
    with sqlite3.connect(root / 'state/runtime.sqlite3') as db:
        run = json.loads(db.execute("SELECT data FROM runs WHERE id='r1'").fetchone()[0])
        run.update(status='complete', active_account=None, pause_reason='招募记录卡牌识别有未知项')
        db.execute("UPDATE runs SET data=? WHERE id='r1'", (json.dumps(run),))
    lane = client.get('/api/launcher/status').json()['lanes'][0]
    assert lane['status'] == 'complete' and lane['message'] == ''


@pytest.fixture
def setup(tmp_path, monkeypatch):
    class Cloud:
        def health(self):
            return {"ready": True}
        def get_result_by_index(self, index):
            return {'version': 3, 'usage_status': 'available', 'owner_username': 'administrator',
                    'result': {'index': index, 'account_id': 'cloud-account', 'email': 'owner@example.test',
                               'used': False, 'pull_count': 43, 'cards': []}}
        def presets(self):
            return {'version': 0, 'presets': {'mygo-only': {'name': 'MyGO only', 'total_pulls': 43, 'transactions': 7}}}
        def list_results(self):
            return {"revision": 7, "results": [], "publication": {"pending": True}}
        def sync_manifest(self, **kwargs):
            return {'revision': 7, 'accounts': [], 'states': [], 'publication': {'pending': True}}
        def publish(self, expected_revision, request_id=None):
            assert expected_revision == 7
            return {"revision": 7, "published_at": 10, "accounts": 0}
    monkeypatch.setattr("ournotes.integrations.cloud_client.CloudClient.from_root", lambda root: Cloud())
    monkeypatch.setattr('ournotes.integrations.cloud_events.CloudEvents.start', lambda self: None)
    monkeypatch.setattr('ournotes.runtime.storage.StorageWorker.start', lambda self: pytest.fail('Console startup must not organize storage'))
    accounts, catalog = build_folders.__wrapped__(tmp_path)
    root = tmp_path / "project"
    (root / "results/archive").mkdir(parents=True)
    (root / "data/catalog").mkdir(parents=True)
    accounts.rename(root / "results/archive/accounts")
    catalog.rename(root / "data/catalog/site")
    accounts = root / "results/archive/accounts"
    write(accounts, record(1), "1 1 SSR.json")
    write(accounts, record(2, status="partial"), "2 1 SSR.json")
    (root / "config").mkdir()
    (root / "config/presets.json").write_text(
        json.dumps({"presets": [{"name": "MyGO only", "total_pulls": 43, "transactions": 7}]})
    )
    (root / "state").mkdir()
    batch = {
        "id": "b" * 32,
        "status": "paused",
        "created_at": 1,
        "active_seconds": 90,
        "lanes": [{"instance": 0, "preset": "MyGO only", "run_id": "r1"}],
    }
    run = {
        "id": "r1",
        "batch_id": batch["id"],
        "status": "paused",
        "preset": "MyGO only",
        "count": 50,
        "completed": 2,
        "cloud_confirmed": 1,
        "channel": "test",
        "active_account": "a2",
        "pause_reason": "联系 private-sentinel@example.test",
    }
    with sqlite3.connect(root / "state/runtime.sqlite3") as db:
        db.executescript(
            "CREATE TABLE batches(id TEXT,created REAL,data TEXT); CREATE TABLE runs(id TEXT,data TEXT); "
            "CREATE TABLE accounts(id TEXT,alias_index INTEGER,status TEXT,phase TEXT,run_id TEXT); "
            "CREATE TABLE counters(channel TEXT,next_index INTEGER);"
        )
        db.execute("INSERT INTO batches VALUES(?,?,?)", (batch["id"], 1, json.dumps(batch)))
        db.execute("INSERT INTO runs VALUES(?,?)", ("r1", json.dumps(run)))
        db.executemany(
            "INSERT INTO accounts VALUES(?,?,?,?,?)",
            [("a1", 1, "complete", "finished", "r1"), ("a2", 2, "partial", "draws", "r1")],
        )
        db.execute("INSERT INTO counters VALUES('test',3)")
    calls, processes = [], []

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        process = FakeProcess()
        process.stdout = io.BytesIO(b"Synthetic output private-sentinel@example.test\n")
        processes.append(process)
        return process

    manager = JobManager(root, spawn)
    service = Launcher(root, manager=manager, busy_probe=lambda: False, presets=[{"name": "MyGO only", "total_pulls": 43, "transactions": 7}])
    app = create_app(root, service)
    with TestClient(app, base_url="http://127.0.0.1:8766") as client:
        client.headers["X-Launcher-Token"] = app.state.token
        yield client, service, root, calls, processes
        for process in processes:
            process.code = 0


def test_cloud_index_lookup_reads_one_complete_result_without_local_fallback(setup, monkeypatch):
    client, _, root, _, _ = setup
    calls = []
    class Cloud:
        def get_result_by_index(self, index):
            calls.append(('index', index))
            return {'result': {'index': index, 'account_id': 'cloud-account', 'used': False,
                               'pull_count': 43, 'cards': []}, 'usage_status': 'available', 'version': 3}
        def list_results(self):
            pytest.fail('A single index query must not download all cloud accounts')
    monkeypatch.setattr('ournotes.integrations.cloud_client.CloudClient.from_root', lambda _: Cloud())
    response = client.get('/api/launcher/accounts/3416')
    assert response.status_code == 200 and response.json()['index'] == 3416
    assert calls == [('index', 3416)]
    assert not list((root / 'results/archive/accounts').glob('3416*'))
    assert client.get('/api/launcher/cloud/results?index=3416').status_code == 404
    for invalid in (-1, 2**53):
        assert client.get(f'/api/launcher/accounts/{invalid}').status_code == 422
    assert calls == [('index', 3416)]


def test_startup_projects_cloud_confirmation_and_preserves_legacy_files(setup):
    client, _, root, calls, _ = setup
    path = root / "state/runtime.sqlite3"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    status = client.get("/api/launcher/status").json()
    assert "accounts" not in status and "email_rebind" in status and "clash" in status
    assert "maintenance" not in status["storage"]
    assert status["lanes"][0]["complete"] == 1 and status["lanes"][0]["partial"] == 1
    assert status["lanes"][0]["processed"] == 2 and not status["running"]
    assert "private-sentinel" not in json.dumps(status)
    assert client.get("/").status_code == 200
    assert client.get('/cards/').status_code == 404
    assert client.get('/cards/api/accounts').status_code == 404
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not calls and not (root / "state/pause-request.json").exists()


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil.example"},
        {"Origin": "http://evil.example"},
        {"Origin": "http://127.0.0.1:9999"},
        {"Sec-Fetch-Site": "cross-site"},
        {"X-Launcher-Token": "bad"},
    ],
)
def test_reject_foreign_or_unauthorized_actions(setup, headers):
    client, _, _, calls, _ = setup
    assert client.post("/api/launcher/action", json={"action": "export"}, headers=headers).status_code == 403
    assert not calls


def test_server_validation_unknown_shell_duplicate_lanes_and_no_overwrite(setup):
    client, _, root, calls, _ = setup
    assert client.post("/api/launcher/action", json={"action": "shell", "command": "calc"}).status_code == 422
    valid = {"action": "start", "run": {"lanes": [{"instance": 0, "preset": "MyGO only"}], "count": 1, "preset_version": 0}}
    assert client.post("/api/launcher/action", json=valid).status_code == 409
    with sqlite3.connect(root / "state/runtime.sqlite3") as db:
        db.execute("DELETE FROM batches")
        db.execute("DELETE FROM runs")
    valid["run"]["lanes"].append({"instance": 0, "preset": "MyGO only"})
    assert client.post("/api/launcher/action", json=valid).status_code == 409
    valid["run"]["lanes"].pop()
    valid["run"]["count"] = -1
    assert client.post("/api/launcher/action", json=valid).status_code == 422
    assert not calls


def test_start_argv_and_duplicate_process_guard(setup):
    client, _, root, calls, processes = setup
    with sqlite3.connect(root / "state/runtime.sqlite3") as db:
        db.execute("DELETE FROM batches")
        db.execute("DELETE FROM runs")
    body = {
        "action": "start",
        "run": {
            "lanes": [{"instance": 2, "preset": "MyGO only"}],
            "count": 0,
            "async_start": True,
            "channel": "test",
            "preset_version": 0,
        },
    }
    assert client.post("/api/launcher/action", json=body).status_code == 200
    argv, kwargs = calls[0]
    assert argv[3:] == [
        "-m",
        "ournotes.runtime.scheduler",
        "--channel",
        "test",
        "--count",
        "0",
        "--preset-version",
        "0",
        "--async-start",
        "--lane",
        "2=MyGO only",
    ]
    assert not kwargs.get("shell")
    assert client.post("/api/launcher/action", json=body).status_code == 409
    assert len(calls) == 1
    processes[0].code = 2
    assert client.get("/api/launcher/status").json()["jobs"][0]["status"] == "paused"


def test_resume_uses_durable_config_and_controls_bound_to_current_batch(setup):
    client, _, root, calls, processes = setup
    assert (
        client.post("/api/launcher/action", json={"action": "resume", "batch_id": "a" * 32}).status_code
        == 409
    )
    assert (
        client.post("/api/launcher/action", json={"action": "resume", "batch_id": "b" * 32}).status_code
        == 200
    )
    assert calls[0][0][3:] == ["-m", "ournotes.runtime.scheduler", "--resume"]
    assert (
        client.post("/api/launcher/action", json={"action": "pause", "batch_id": "a" * 32}).status_code == 409
    )
    for action in ("pause", "finish"):
        assert (
            client.post("/api/launcher/action", json={"action": action, "batch_id": "b" * 32}).status_code
            == 200
        )
        from ournotes.control.operations import OperationStore
        assert any(r["action"] == action for r in OperationStore(root).pending("b" * 32))
        assert not (root / f"state/{action}-request.json").exists()
    processes[0].code = 2
    assert (
        client.post("/api/launcher/action", json={"action": "pause", "batch_id": "b" * 32}).status_code == 409
    )


def test_external_runner_and_mail_task_interlocks(setup):
    client, service, _, calls, _ = setup
    service.busy_probe = lambda: True
    for action in ("resume", "gmail_check"):
        assert (
            client.post("/api/launcher/action", json={"action": action, "batch_id": "b" * 32}).status_code
            == 409
        )
    service.busy_probe = lambda: False
    assert client.post("/api/launcher/action", json={"action": "gmail_check"}).status_code == 200
    assert not calls  # cloud health never spawns a local Gmail process


@pytest.mark.parametrize('action', ['sync', 'sync_diff', 'export', 'report', 'preview'])
def test_removed_tasks_cannot_spawn_or_recreate_local_results(setup, action):
    client, _, root, calls, _ = setup
    assert client.post('/api/launcher/action', json={'action': action, 'index': 1}).status_code == 422
    assert calls == [] and not (root / 'results/previews').exists()
    for path in ('/api/launcher/cloud/results', '/api/launcher/statistics', '/static/report.js'):
        assert client.get(path).status_code == 404
    html = client.get('/').text
    assert '网站发布' not in html and '商品上架' not in html and '打开卡牌筛选' not in html
    assert 'id="server-admin"' in html and '账号管理' in html
    for route in ('client', 'authorize', 'migrate', 'forget'):
        assert client.post('/api/launcher/oauth/' + route, json={}).status_code == 404


def test_cloud_settings_and_email_require_session_token(setup, monkeypatch):
    client, _, _, calls, _ = setup
    assert client.post("/api/launcher/settings", json={}).status_code == 404
    monkeypatch.setattr("ournotes.integrations.cloud_client.CloudClientConfig.save", lambda root, service_url, credential: {"service_url": service_url, "configured": True})
    assert client.post("/api/launcher/cloud", json={"service_url": "https://example.test", "credential": "a" * 32}).json() == {"service_url": "https://example.test", "configured": True}
    monkeypatch.setattr("ournotes.accounts.email.find_email", lambda *args, **kwargs: pytest.fail("Account management must only read the cloud"))
    assert client.post("/api/launcher/email", json={"index": 1}).json()["email"] == "owner@example.test"
    assert client.post("/api/launcher/email", json={"index": 1}, headers={"X-Launcher-Token": ""}).status_code == 403
    assert not calls


def test_database_failure_blocks_actions_and_wrong_batch_status_is_ignored(setup):
    client, _, root, calls, _ = setup
    (root / "state/runner-status.json").write_text(
        json.dumps({"batch_id": "a" * 32, "lanes": [{"run_id": "r1", "phase": "WRONG"}]})
    )
    assert client.get("/api/launcher/status").json()["lanes"][0]["phase"] == "draws"
    (root / "state/runtime.sqlite3").write_bytes(b"corrupt database")
    assert client.get("/api/launcher/status").json()["error"]
    assert (
        client.post("/api/launcher/action", json={"action": "resume", "batch_id": "b" * 32}).status_code
        == 409
    )
    assert not calls


def test_close_batch_preserves_records_and_enables_next_start(setup):
    client, _, root, calls, processes = setup
    database = root / "state/runtime.sqlite3"
    with sqlite3.connect(database) as db:
        before_accounts = db.execute("SELECT * FROM accounts").fetchall()
        before_counters = db.execute("SELECT * FROM counters").fetchall()
    body = {"action": "cancel", "batch_id": "b" * 32}
    assert client.post("/api/launcher/action", json=body, headers={"X-Launcher-Token": ""}).status_code == 403
    assert client.post("/api/launcher/action", json={**body, "batch_id": "a" * 32}).status_code == 409
    assert client.post("/api/launcher/action", json=body).status_code == 200
    state = client.get("/api/launcher/status").json()
    assert state["batch"]["status"] == state["lanes"][0]["status"] == "cancelled"
    assert state["lanes"][0]["processed"] == 2 and state["lanes"][0]["target"] == 50
    assert state["lanes"][0]["index"] == 2 and not calls
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT * FROM accounts").fetchall() == before_accounts
        assert db.execute("SELECT * FROM counters").fetchall() == before_counters
    assert client.post("/api/launcher/action", json=body).status_code == 409
    assert client.post("/api/launcher/action", json={**body, "action": "resume"}).status_code == 409
    new = {"action": "start", "run": {"lanes": [{"instance": 0, "preset": "MyGO only"}], "count": 50, "preset_version": 0}}
    assert client.post("/api/launcher/action", json=new).status_code == 200
    assert len(calls) == 1
    processes[0].code = 0


def test_close_refuses_active_runner_and_shared_lock(setup):
    from ournotes.common.secrets import SecretStore

    client, service, root, calls, _ = setup
    body = {"action": "cancel", "batch_id": "b" * 32}
    service.busy_probe = lambda: True
    assert client.post("/api/launcher/action", json=body).status_code == 409
    service.busy_probe = lambda: False
    with SecretStore(root / "state").lock("runner"):
        assert client.post("/api/launcher/action", json=body).status_code == 409
    assert client.get("/api/launcher/status").json()["batch"]["status"] == "paused"
    assert not calls


def test_close_rolls_back_all_lanes_if_one_is_missing(setup):
    client, _, root, _, _ = setup
    with sqlite3.connect(root / "state/runtime.sqlite3") as db:
        batch = json.loads(db.execute("SELECT data FROM batches").fetchone()[0])
        batch["lanes"].append({"run_id": "missing", "instance": 1, "preset": "MyGO only"})
        db.execute("UPDATE batches SET data=?", (json.dumps(batch),))
    body = {"action": "cancel", "batch_id": "b" * 32}
    assert client.post("/api/launcher/action", json=body).status_code == 409
    with sqlite3.connect(root / "state/runtime.sqlite3") as db:
        assert json.loads(db.execute("SELECT data FROM runs").fetchone()[0])["status"] == "paused"
        assert json.loads(db.execute("SELECT data FROM batches").fetchone()[0])["status"] == "paused"


def test_close_real_store_preserves_draw_evidence_and_continuous_indices(tmp_path):
    from ournotes.runtime.pools import make_plan
    from ournotes.runtime.scheduler import create_batch
    from ournotes.runtime.store import RuntimeStore, alias_at
    class Allocator:
        counter = 8
        def allocate(self, request_id, account_id, channel):
            index = self.counter
            self.counter += 1
            return {"account_id": account_id, "index": index, "alias": alias_at(index), "node_id": "test-node"}
    db = RuntimeStore(tmp_path, allocator=Allocator())
    try:
        batch = create_batch(db, [(0, "MyGO only"), (1, "Mujica only")], "test", 1)
        account = db.reserve(db.get_run(batch["lanes"][0]["run_id"]), make_plan("MyGO only"))
        db.save_draw({"id": "unresolved", "account_id": account["id"], "ordinal": 0, "status": "submitted"})
        completed = db.get_run(batch["lanes"][1]["run_id"])
        completed.update(status="complete", completed=1)
        db.save_run(completed)
        before = {table: [tuple(row) for row in db.db.execute(f"SELECT * FROM {table}")]
                  for table in ("accounts", "draws", "counters")}
        archive = tmp_path / "results/archive/accounts/sentinel.json"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"preserved archive")
        Launcher(tmp_path, busy_probe=lambda: False).dispatch("cancel", {"batch_id": batch["id"]})
        for table, rows in before.items():
            assert [tuple(row) for row in db.db.execute(f"SELECT * FROM {table}")] == rows
        assert archive.read_bytes() == b"preserved archive"
        assert db.get_run(completed["id"]) == completed
        assert db.get_batch()["status"] == "cancelled"
        next_batch = create_batch(db, [(0, "MyGO only")], "test", 2)
        next_account = db.reserve(db.get_run(next_batch["lanes"][0]["run_id"]), make_plan("MyGO only"))
        assert next_account["alias_index"] == account["alias_index"] + 1
        assert db.account(account["id"]) == account
    finally:
        db.close()
