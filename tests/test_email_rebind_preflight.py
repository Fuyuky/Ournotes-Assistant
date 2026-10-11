"""Startup failures remain actionable, secret-free and unable to reach game inputs."""
import json
import sqlite3
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from test_device_concurrency import batch_state
from test_email_rebind import Cloud, Driver, wait

from ournotes.common.files import atomic_json
from ournotes.common.secrets import SecretStore
from ournotes.rebind import preflight
from ournotes.rebind.preflight import PreflightError, idle_batch
from ournotes.rebind.service import Service


def test_busy_database_exposes_only_safe_metadata(tmp_path, monkeypatch):
    path = tmp_path / 'state/runtime.sqlite3'
    path.parent.mkdir()
    path.touch()
    failure = sqlite3.OperationalError('secret-mail@example.com password=private-sentinel')
    failure.sqlite_errorcode = sqlite3.SQLITE_BUSY
    monkeypatch.setattr(preflight.sqlite3, 'connect', Mock(side_effect=failure))
    with pytest.raises(PreflightError, match='正被占用') as caught:
        idle_batch(tmp_path, 0, 2439)
    assert caught.value.info['code'] == 'runtime_db_busy'
    assert caught.value.info['sqlite_errorcode'] == sqlite3.SQLITE_BUSY
    assert caught.value.info['source'] == 'runtime-db'
    assert 'private-sentinel' not in json.dumps(caught.value.info)
    assert 'example.com' not in json.dumps(caught.value.info)


def test_missing_schema_is_not_reported_as_temporary_busy(tmp_path):
    path = tmp_path / 'state/runtime.sqlite3'
    path.parent.mkdir()
    sqlite3.connect(path).close()
    with pytest.raises(PreflightError) as caught:
        idle_batch(tmp_path, 0, 2439)
    assert caught.value.info['code'] == 'runtime_db_schema'
    assert '版本' in caught.value.info['hint']


@pytest.mark.parametrize('raw', ['{broken-json', '[]', '{"lanes":[]}'])
def test_invalid_batch_record_is_identified_and_not_repaired(tmp_path, raw):
    db, batch = batch_state(tmp_path)
    try:
        db.db.execute('UPDATE batches SET data=? WHERE id=?', (raw, batch['id']))
        before = list(db.db.iterdump())
        with pytest.raises(PreflightError) as caught:
            idle_batch(tmp_path, 0, 2439)
        assert caught.value.info['code'] == 'runtime_record_invalid'
        assert list(db.db.iterdump()) == before
    finally:
        db.close()


@pytest.mark.parametrize('problem,code', [
    ('missing', 'runner_status_missing'), ('json', 'runner_status_invalid'),
    ('shape', 'runner_status_invalid'), ('permission', 'runner_status_access_denied'),
])
def test_runner_status_read_failures_are_specific(tmp_path, monkeypatch, problem, code):
    db, _ = batch_state(tmp_path)
    path = tmp_path / 'state/runner-status.json'
    try:
        if problem == 'missing':
            path.unlink()
        elif problem == 'json':
            path.write_text('{private-sentinel', encoding='utf-8')
        elif problem == 'shape':
            path.write_text('[]', encoding='utf-8')
        else:
            original = type(path).read_text
            def denied(self, *args, **kwargs):
                if self == path:
                    raise PermissionError(13, 'private-sentinel')
                return original(self, *args, **kwargs)
            monkeypatch.setattr(type(path), 'read_text', denied)
        with SecretStore(tmp_path / 'state').lock('runner'), pytest.raises(PreflightError) as caught:
            idle_batch(tmp_path, 0, 2439)
        assert caught.value.info['code'] == code
        assert caught.value.info['source'] == 'runner-status'
        assert 'private-sentinel' not in json.dumps(caught.value.info)
    finally:
        db.close()


@pytest.mark.parametrize('problem,code', [
    ('maintenance', 'storage_maintenance'), ('finishing', 'runner_finishing'),
    ('paused', 'runner_state_mismatch'), ('batch', 'runner_batch_mismatch'),
    ('protocol', 'runner_protocol_old'), ('leases', 'runner_leases_missing'),
])
def test_controller_conditions_have_distinct_actions(tmp_path, problem, code):
    db, batch = batch_state(tmp_path)
    status = {'batch_id': batch['id'], 'status': 'running', 'device_lease_protocol': 1}
    try:
        if problem in ('finishing', 'paused'):
            batch['status'] = 'complete' if problem == 'finishing' else 'paused'
            db.save_batch(batch)
        elif problem == 'batch':
            status['batch_id'] = 'different-batch'
        elif problem == 'protocol':
            status['device_lease_protocol'] = 0
        atomic_json(tmp_path / 'state/runner-status.json', status)
        maintenance = SecretStore(tmp_path / 'state').lock('storage-maintenance') if problem == 'maintenance' else nullcontext()
        with SecretStore(tmp_path / 'state').lock('runner'), maintenance, pytest.raises(PreflightError) as caught:
            idle_batch(tmp_path, 0, 2439)
        assert caught.value.info['code'] == code
        assert caught.value.info['hint']
    finally:
        db.close()


def test_preflight_diagnostic_survives_restart_and_clear_without_replay(tmp_path):
    cloud, driver = Mock(), Mock()
    original = OSError(13, 'password=private-sentinel target@example.com')
    def reject(*args):
        raise PreflightError('runner_status_read_failed', '调度状态文件读取失败', '核对后清除任务再试。',
                             source='runner-status', cause=original)
    service = Service(tmp_path, client=cloud, driver=driver, idle_check=reject, lease=lambda _: nullcontext())
    service.start(2439, 0, 'target@qq.com', 'a' * 32)
    task = wait(service, 'error')
    service.thread.join(3)
    password = service.password
    assert task['error']['code'] == 'runner_status_read_failed'
    assert task['error']['diagnostic_saved']
    record = tmp_path / 'results/maintenance/diagnostics/email-rebind' / task['id'] / 'failure.json'
    for text in (record.read_text('utf-8'), service.journal.read_text('utf-8')):
        assert 'private-sentinel' not in text and 'target@' not in text and password not in text
    restarted = Service(tmp_path, client=cloud, driver=driver)
    assert restarted.status()['task']['state'] == 'error'
    assert restarted.status()['task']['message'] == task['message']
    assert restarted.status()['task']['error'] == task['error']
    assert restarted.password is None
    atomic_json(service.outcomes, {'2439': {'account_id': 'a', 'state': 'used_pending'}})
    service.stop(task['id'])
    assert record.exists() and service.outcomes.exists()
    cloud.assert_not_called()
    driver.assert_not_called()


def test_failure_after_entering_flow_still_requires_review(tmp_path):
    class Failed(Driver):
        def run(self):
            raise ValueError('换绑结果待核查')
    service = Service(tmp_path, client=Cloud, driver=Failed, idle_check=lambda *args: None,
                      lease=lambda _: nullcontext())
    service.start(1, 0, 'target@qq.com', 'a' * 32)
    task = wait(service, 'error')
    service.thread.join(3)
    assert '待核查' in task['message']
    assert 'error' not in task
    service.close()


def test_cloud_preflight_exception_details_are_never_echoed(tmp_path):
    from ournotes.integrations.cloud_client import CloudError
    cloud, driver = Mock(side_effect=CloudError('offline', 'private-sentinel')), Mock()
    service = Service(tmp_path, client=cloud, driver=driver, idle_check=lambda *args: None,
                      lease=lambda _: nullcontext())
    service.start(1, 0, 'target@qq.com', 'a' * 32)
    task = wait(service, 'error')
    service.thread.join(3)
    assert task['error']['code'] == 'cloud_preflight_failed'
    assert 'private-sentinel' not in json.dumps(task)
    assert 'private-sentinel' not in service.journal.read_text('utf-8')
    driver.assert_not_called()
    service.close()
