import copy
import json
import time
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from ournotes.accounts.used import reconcile_pending
from ournotes.rebind.service import Service


class Cloud:
    def __init__(self, used=False):
        self.item = {'result': {'index': 1, 'account_id': 'a', 'email': 'original@gmail.com', 'used': used},
                     'usage_status': 'used' if used else 'available', 'version': 1}
        self.writes = 0

    def request(self, *args):
        return {'enabled': True, 'protocol': 2}

    def get_result_by_index(self, index):
        return copy.deepcopy(self.item)

    def set_usage(self, account, target, version, request_id=None):
        assert version == self.item['version']
        self.writes += 1
        self.item['result']['used'] = target != 'available'
        self.item['usage_status'] = target
        self.item['version'] += 1
        return copy.deepcopy(self.item)


class Driver:
    def __init__(self, service, *args):
        self.service = service
        self.closed = False

    def run(self):
        self.service.wait_confirmation()
        self.service.wait_confirmation('邮箱链接尚未确认')

    def close(self):
        self.closed = True


def wait(service, state):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        value = service.status()['task']
        if (value or {}).get('state') == state:
            return value
        time.sleep(.01)
    raise AssertionError(service.status())


def make(tmp_path, **kwargs):
    kwargs.setdefault('shutdown', lambda instances: {'status': 'closed', 'instances': instances})
    return Service(tmp_path, lease=lambda instance: nullcontext(), idle_check=lambda *args: None, **kwargs)


@pytest.mark.parametrize('state', ['success', 'ending'])
def test_completed_task_end_has_normal_cleanup_state_and_is_idempotent(tmp_path, state):
    service = make(tmp_path)
    service.task = {'id': 'a' * 32, 'state': state}
    service.thread = SimpleNamespace(is_alive=lambda: True)
    result = service.stop(service.task['id'])
    assert result['task']['state'] == 'ending'
    assert result['task']['message'] == '换绑已完成，正在收尾并清除任务。'
    assert service.stop(service.task['id'])['task']['state'] == 'ending'
    assert service.stop_event.is_set()


def test_wait_retry_success_used_and_clear(tmp_path):
    cloud = Cloud()
    s = make(tmp_path, client=lambda: cloud, driver=Driver)
    first = s.start(1, 0, 'target@gmail.com', 'a'*32)
    task = wait(s, 'waiting_b')
    assert s.start(1, 0, 'target@gmail.com', 'a'*32)['task']['id'] == first['task']['id']
    assert len(s.secret(task['id'])['password']) == 16
    assert 'password' not in s.journal.read_text()
    s.confirmed(task['id'], 'b'*32)
    task = wait(s, 'waiting_b')
    assert '尚未确认' in task['message']
    s.confirmed(task['id'], 'b'*32)  # Duplicate old HTTP command cannot perform the next attempt.
    assert s.status()['task']['state'] == 'waiting_b'
    s.confirmed(task['id'], 'c'*32)
    wait(s, 'success')
    s.thread.join(2)
    assert cloud.writes == 1
    s.stop(task['id'])
    assert s.status()['task'] is None and s.password is None and not s.journal.exists()
    with pytest.raises(ValueError):
        s.confirmed(task['id'], 'd'*32)
    with pytest.raises(ValueError, match='已结束'):
        s.start(1, 0, 'target@gmail.com', 'a'*32)


def test_expired_google_grant_reports_flow_diagnostic_without_replaying_or_changing_used(tmp_path):
    from ournotes.integrations.cloud_client import CloudError

    cloud = Cloud()

    class ExpiredGrant(Driver):
        def run(self):
            raise CloudError('gmail_authorization_required', 'private-sentinel', 424)

    service = make(tmp_path, client=lambda: cloud, driver=ExpiredGrant)
    service.start(1, 0, 'target@gmail.com', 'a' * 32)
    task = wait(service, 'error')
    service.thread.join(2)
    assert '授权已失效' in task['message'] and task['error']['http_status'] == 424
    assert task['error']['code'] == 'gmail_authorization_required'
    assert 'private-sentinel' not in json.dumps(task) + service.journal.read_text()
    assert cloud.writes == 0 and task['state'] == 'error'
    service.close()



def test_booked_can_rebind_and_cancellation_during_flow_still_finishes_used(tmp_path):
    cloud = Cloud()
    cloud.item.update(usage_status='booked')
    cloud.item['result']['used'] = True
    class CancelDuringFlow(Driver):
        def run(self):
            cloud.set_usage('a', 'available', cloud.item['version'])
    s = make(tmp_path, client=lambda: cloud, driver=CancelDuringFlow)
    s.start(1, 0, 'target@gmail.com', 'a'*32)
    wait(s, 'success')
    s.thread.join(2)
    assert cloud.item['usage_status'] == 'used' and cloud.writes == 2


def test_book_cancel_cas_conflict_after_game_success_retries_only_status(tmp_path):
    from ournotes.integrations.cloud_client import CloudError
    class ConflictOnce(Cloud):
        def set_usage(self, account, target, version, request_id=None):
            if self.writes == 0:
                super().set_usage(account, 'available', version)
                raise CloudError('usage_conflict', 'synthetic cancellation won', 409)
            return super().set_usage(account, target, version, request_id)
    cloud = ConflictOnce()
    cloud.item.update(usage_status='booked')
    cloud.item['result']['used'] = True
    games = []
    class Finished(Driver):
        def run(self):
            games.append(1)
    s = make(tmp_path, client=lambda: cloud, driver=Finished)
    s.start(1, 0, 'target@gmail.com', 'a'*32)
    wait(s, 'success')
    s.thread.join(2)
    assert games == [1] and cloud.item['usage_status'] == 'used'


@pytest.mark.parametrize('missing_when', ['before', 'after'])
def test_missing_real_status_never_clears_rebind_protection(tmp_path, missing_when):
    cloud = Cloud()
    games = []
    class IncompleteProtocol(Driver):
        def run(self):
            games.append(1)
            cloud.item.pop('usage_status')
            cloud.item['result']['used'] = True
    s = make(tmp_path, client=lambda: cloud, driver=IncompleteProtocol)
    if missing_when == 'before':
        cloud.item.pop('usage_status')
        cloud.item['result']['used'] = True
        s.outcome(1, 'a', 'binding_uncertain')
    s.start(1, 0, 'target@gmail.com', 'a'*32)
    wait(s, 'error')
    s.thread.join(2)
    records = json.loads(s.outcomes.read_text())
    assert records['1']['state'] == ('binding_uncertain' if missing_when == 'before' else 'used_pending')
    assert games == ([] if missing_when == 'before' else [1]) and cloud.writes == 0
    if missing_when == 'after':
        with pytest.raises(ValueError, match='协议不兼容'):
            reconcile_pending(tmp_path, cloud)
        assert json.loads(s.outcomes.read_text()) == records
    s.close()


def test_used_account_never_constructs_driver(tmp_path):
    s = make(tmp_path, client=lambda: Cloud(True), driver=lambda *args: pytest.fail('device created'))
    s.start(1, 0, 'target@gmail.com', 'a'*32)
    wait(s, 'error')
    assert '已使用' in s.status()['task']['message']
    s.close()


def test_stop_waiting_clears_without_used(tmp_path):
    cloud = Cloud()
    s = make(tmp_path, client=lambda: cloud, driver=Driver)
    s.start(1, 0, 'target@gmail.com', 'a'*32)
    task = wait(s, 'waiting_b')
    s.stop(task['id'])
    s.thread.join(2)
    assert s.status()['task'] is None and cloud.writes == 0


def test_registered_replacement_stays_in_same_task_and_stale_commands_do_not_wake_next_wait(tmp_path):
    cloud = Cloud()
    replacements = []

    class Registered(Driver):
        def run(self):
            replacements.append(self.service.wait_target_email())
            replacements.append(self.service.wait_target_email())
            self.service.wait_confirmation()

    service = make(tmp_path, client=lambda: cloud, driver=Registered)
    service.start(1, 0, 'registered@gmail.com', 'a' * 32)
    task = wait(service, 'waiting_email')
    password = service.secret(task['id'])['password']
    assert task['message'] == '邮箱已注册，请更换邮箱' and task['email_revision'] == 1
    for invalid in ('registered@gmail.com', 'ORIGINAL@gmail.com', 'other@example.com'):
        with pytest.raises(ValueError):
            service.retry_target(task['id'], 'b' * 32, invalid, 1)
    service.retry_target(task['id'], 'b' * 32, 'second@qq.com', 1)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        current = service.status()['task']
        if current['state'] == 'waiting_email' and current['email_revision'] == 2:
            break
        time.sleep(.01)
    else:
        raise AssertionError(service.status())
    assert service.retry_target(task['id'], 'b' * 32, 'second@qq.com', 1)['task']['state'] == 'waiting_email'
    with pytest.raises(ValueError, match='过期'):
        service.retry_target(task['id'], 'c' * 32, 'third@163.com', 1)
    assert replacements == [('second@qq.com', 1)] and not service.email_event.is_set()
    service.retry_target(task['id'], 'd' * 32, 'third@163.com', 2)
    wait(service, 'waiting_b')
    assert replacements == [('second@qq.com', 1), ('third@163.com', 2)]
    assert service.status()['task']['id'] == task['id'] and cloud.writes == 0
    assert service.secret(task['id'])['password'] == password
    assert all(private not in service.journal.read_text() for private in
               ('registered@gmail.com', 'second@qq.com', 'third@163.com', 'original@gmail.com', password))
    service.confirmed(task['id'], 'e' * 32)
    wait(service, 'success')
    service.thread.join(2)
    assert cloud.writes == 1
    service.close()


def test_stop_during_registered_wait_releases_task_without_clearing_existing_protection(tmp_path):
    cloud = Cloud()
    closed = []

    class Registered(Driver):
        def run(self):
            self.service.wait_target_email()

        def close(self):
            closed.append(True)

    service = make(tmp_path, client=lambda: cloud, driver=Registered)
    service.start(1, 0, 'registered@gmail.com', 'a' * 32)
    task = wait(service, 'waiting_email')
    service.outcome(1, 'a', 'binding_uncertain')
    protection = service.outcomes.read_bytes()
    service.stop(task['id'])
    service.thread.join(2)
    assert closed == [True] and cloud.writes == 0
    assert service.status()['task'] is None and service.password is None
    assert service.outcomes.read_bytes() == protection


def test_restart_does_not_replay(tmp_path):
    cloud = Cloud()
    s = make(tmp_path, client=lambda: cloud, driver=Driver)
    s.start(1, 0, 'target@gmail.com', 'a'*32)
    task = wait(s, 'waiting_b')
    other = make(tmp_path, client=lambda: pytest.fail('must not connect'))
    assert other.status()['task']['state'] == 'interrupted'
    assert other.password is None
    with pytest.raises(ValueError):
        other.start(1, 0, 'target@gmail.com', 'b'*32)
    s.stop(task['id'])
    s.thread.join(2)


def test_successful_game_but_lost_used_update_keeps_business_guard(tmp_path):
    from ournotes.integrations.cloud_client import CloudError
    class OfflineUsed(Cloud):
        def set_usage(self, *args, **kwargs):
            raise CloudError('unavailable', 'offline')
    class Finished(Driver):
        def run(self):
            return
    cloud = OfflineUsed()
    s = make(tmp_path, client=lambda: cloud, driver=Finished)
    s.start(1, 0, 'target@gmail.com', 'a'*32)
    task = wait(s, 'error')
    s.thread.join(2)
    assert '已成功' in task['message']
    s.stop(task['id'])
    assert s.status()['task'] is None and s.password is None
    assert 'used_pending' in s.outcomes.read_text()
    s.start(1, 0, 'target@gmail.com', 'b'*32)
    task = wait(s, 'error')
    assert '待核查' in task['message']
    s.close()


def test_unsupported_domain_rejected_before_task_or_device(tmp_path):
    s = make(tmp_path, client=lambda: pytest.fail('must not connect'))
    with pytest.raises(ValueError, match='只支持'):
        s.start(1, 0, 'target@example.com', 'a'*32)
    assert s.status()['task'] is None


def test_success_closes_only_rebind_instance_before_releasing_lease(tmp_path):
    from contextlib import contextmanager
    events = []
    @contextmanager
    def lease(instance):
        events.append(('acquire', instance))
        yield
        events.append(('release', instance))
    class Finished(Driver):
        def run(self):
            return
        def close(self):
            events.append(('sdk_close',))
    def shutdown(instances):
        events.append(('shutdown', instances))
        return {'status': 'closed', 'instances': instances}
    s = Service(tmp_path, client=Cloud, driver=Finished, lease=lease,
                idle_check=lambda *args: None, shutdown=shutdown)
    s.start(1, 4, 'target@gmail.com', 'a'*32)
    wait(s, 'success')
    s.thread.join(2)
    assert events == [('acquire', 4), ('sdk_close',), ('shutdown', [4]), ('release', 4)]
    assert s.status()['task']['shutdown']['instances'] == [4]
    s.close()


def test_confirmed_rebind_closes_instance_even_if_used_sync_is_pending(tmp_path):
    calls = []
    class Offline(Cloud):
        def set_usage(self, *args, **kwargs):
            raise OSError('synthetic offline')
    class Finished(Driver):
        def run(self):
            return
    s = make(tmp_path, client=Offline, driver=Finished, shutdown=lambda instances: calls.append(instances))
    s.start(1, 4, 'target@gmail.com', 'a'*32)
    wait(s, 'error')
    s.thread.join(2)
    assert calls == [[4]]
    assert 'used_pending' in s.outcomes.read_text()
    s.close()
