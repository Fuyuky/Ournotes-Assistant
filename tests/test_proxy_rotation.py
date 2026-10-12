"""Rolling quota and task boundaries with fake clocks/controllers/inputs only."""
import multiprocessing as mp
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

import pytest

from ournotes.proxy.rotation import ProxyCoordinator, ProxyQuotaError, TaskProxyGate
from ournotes.proxy.ui.status import rotation_status
from ournotes.proxy.usage import WINDOW, usage_status


def test_busy_pipe_waits_only_before_connect(monkeypatch):
    from unittest.mock import Mock

    from ournotes.proxy.rotation import ClashController
    controller = ClashController.__new__(ClashController)
    controller.pipe = 'test-pipe'
    stream = object()
    opener = Mock(side_effect=[OSError(22, 'busy'), stream])
    monkeypatch.setattr('builtins.open', opener)
    sleep = Mock()
    monkeypatch.setattr('ournotes.proxy.rotation.time.sleep', sleep)
    from ournotes.platforms.proxy import open_pipe
    assert open_pipe(controller.pipe) is stream
    assert opener.call_count == 2
    sleep.assert_called_once_with(.05)


def test_busy_pipe_has_bounded_wait(monkeypatch):
    from unittest.mock import Mock

    from ournotes.proxy.rotation import ClashController
    controller = ClashController.__new__(ClashController)
    controller.pipe = 'test-pipe'
    opener = Mock(side_effect=OSError(22, 'busy'))
    monkeypatch.setattr('builtins.open', opener)
    monkeypatch.setattr('ournotes.proxy.rotation.time.monotonic', Mock(side_effect=[0, 5]))
    with pytest.raises(ValueError, match='持续繁忙'):
        from ournotes.platforms.proxy import open_pipe
        open_pipe(controller.pipe)
    assert opener.call_count == 1


def test_pipe_sent_request_is_never_replayed(monkeypatch):
    from unittest.mock import MagicMock, Mock

    from ournotes.proxy.rotation import ClashController
    controller = ClashController.__new__(ClashController)
    controller.pipe, controller.secret = r'\\.\pipe\test', 'test'
    stream = MagicMock()
    stream.__enter__.return_value = stream
    stream.write.side_effect = OSError(22, 'uncertain write')
    opener = Mock(return_value=stream)
    monkeypatch.setattr('ournotes.platforms.proxy.open_pipe', opener)
    with pytest.raises(OSError):
        controller.request('/proxies/Proxies', {'name': 'B'})
    assert opener.call_count == stream.write.call_count == 1


class Controller:
    def __init__(self):
        self.nodes = ['A', 'B', 'C']
        self.selected = 'A'
        self.group = 'Proxies'
        self.switches = []
        self.fail = None

    def selection(self):
        return self.group, list(self.nodes), self.selected

    def switch(self, group, target):
        self.switches.append((group, target))
        if self.fail == 'before':
            raise ValueError('Clash Verge 节点切换未确认。')
        self.selected = target
        if self.fail == 'after':
            raise ValueError('Clash Verge 节点切换未确认。')


def test_coordinator_shares_cache_and_refreshes_before_send(tmp_path):
    from unittest.mock import Mock

    raw = Controller()
    raw.selection = Mock(wraps=raw.selection)
    first = ProxyCoordinator(tmp_path, controller=raw)
    second = ProxyCoordinator(tmp_path, controller=raw)
    assert first.selection()[2] == second.selection()[2] == 'A'
    assert raw.selection.call_count == 1
    raw.selected = 'B'
    assert second.selection(fresh=True)[2] == 'B'
    assert first.selection()[2] == 'B'
    raw.fail = 'after'
    with pytest.raises(ValueError):
        first.switch('Proxies', 'C')
    assert not first.cache.exists()
    assert second.selection()[2] == 'C'
    assert raw.switches == [('Proxies', 'C')]


def coordinator_probe(root, events):
    import time

    class SlowController(Controller):
        def selection(self):
            events.put(('start', os.getpid()))
            time.sleep(.1)
            events.put(('end', os.getpid()))
            return super().selection()
    ProxyCoordinator(root, controller=SlowController()).selection(fresh=True)


def test_coordinator_serializes_separate_processes(tmp_path):
    context = mp.get_context('spawn')
    events = context.Queue()
    processes = [context.Process(target=coordinator_probe, args=(tmp_path, events)) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        observed = [events.get(timeout=15) for _ in range(6)]
        assert [event[0] for event in observed] == ['start', 'end'] * 3
        assert all(observed[i][1] == observed[i + 1][1] for i in (0, 2, 4))
    finally:
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
        events.close()
    assert all(process.exitcode == 0 for process in processes)


@pytest.fixture
def setup(tmp_path):
    clock = [100_000.0]
    controller = Controller()
    gate = TaskProxyGate(tmp_path, controller=controller, clock=lambda: clock[0])
    permits = []

    def begin(name='task', budget=2, kind='account', **kwargs):
        permit = gate.begin(name, budget, kind, **kwargs)
        if permit is not None:
            permits.append(permit)
        return permit

    def seed(node, count, created=None):
        with closing(gate.store.connect()) as db:
            offset = db.execute('SELECT count(*) FROM sends').fetchone()[0]
            db.executemany('INSERT INTO sends VALUES(?,?,?,?,?,?)',
                           [(f'seed-{offset+i}', 'old', node, 'login',
                             clock[0] if created is None else created, 'submitted') for i in range(count)])

    def status():
        return usage_status(tmp_path, {'state': 'connected', 'nodes': controller.nodes,
                                     'node': controller.selected, 'group': controller.group}, now=clock[0])

    value = SimpleNamespace(root=tmp_path, clock=clock, controller=controller, gate=gate,
                            begin=begin, seed=seed, status=status)
    yield value
    for permit in permits:
        permit.close()


def send(permit, request_id='otp-1', purpose='login'):
    return permit.send(request_id, purpose, lambda commit: (commit(), True)[1])


def test_explicit_rebind_replacement_reserves_extra_mail_without_resetting_previous_sends(setup):
    permit = setup.begin('rebind', 3, 'rebind')
    for i in range(3):
        send(permit, f'initial-{i}', 'rebind')
    with closing(setup.gate.store.connect()) as db:
        before = db.execute('SELECT * FROM sends ORDER BY id').fetchall()
    permit.reserve_rebind_mail()
    with pytest.raises(ValueError, match='预留'):
        permit.reserve_rebind_mail()
    send(permit, 'replacement-1', 'rebind')
    with closing(setup.gate.store.connect()) as db:
        after = db.execute('SELECT * FROM sends WHERE id LIKE ? ORDER BY id', ('initial-%',)).fetchall()
    assert after == before and setup.status()['used'] == 4
    permit.close()
    with pytest.raises(ValueError, match='已结束'):
        permit.reserve_rebind_mail()


def test_rebind_extra_mail_accounts_for_peer_reservations_and_never_switches_live_tasks(setup):
    permit = setup.begin('rebind', 1, 'rebind')
    send(permit, 'rejected-target', 'rebind')
    setup.begin('peer', 2)
    setup.seed('A', 197)
    with pytest.raises(ProxyQuotaError, match='额度不足'):
        permit.reserve_rebind_mail()
    assert not setup.controller.switches and setup.status()['used'] == 198


def test_normal_account_cannot_reserve_rebind_extra_mail(setup):
    permit = setup.begin('account', 1)
    send(permit)
    with pytest.raises(ValueError, match='预留'):
        permit.reserve_rebind_mail()


def test_rebind_can_start_while_batch_drains_at_rotation_threshold(setup):
    s = setup
    batch = s.begin('batch', 2)
    s.seed('A', 150)
    assert s.begin('next-account') is None
    rebind = s.begin('rebind', 3, 'rebind')
    assert rebind is not None
    for i in range(3):
        send(rebind, f'rebind-{i}', 'rebind')
    assert s.status()['used'] == 153
    assert not s.controller.switches
    rebind.close()
    batch.close()
    assert s.begin('next-account') is not None
    assert s.controller.switches == [('Proxies', 'B')]


def test_rebind_respects_all_outstanding_reservations(setup):
    s = setup
    batch = s.begin('batch', 2)
    s.seed('A', 196)
    assert s.begin('rebind', 3, 'rebind') is None
    assert not s.controller.switches
    batch.close()
    assert s.begin('rebind', 3, 'rebind') is not None
    assert not s.controller.switches  # 196 + 3 fits without disturbing another node.


def process_admission(root, release, results):
    gate = TaskProxyGate(root, controller=Controller(), clock=lambda: 100_000.0)
    permit = gate.begin('process-' + str(os.getpid()), 2)
    results.put(permit is not None)
    if permit is not None:
        try:
            release.wait(15)
        finally:
            permit.close()


def test_cross_process_admissions_and_os_task_leases(setup):
    s = setup
    s.seed('A', 148)
    context = mp.get_context('spawn')
    release, results = context.Event(), context.Queue()
    processes = [context.Process(target=process_admission, args=(s.root, release, results)) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        assert sum(results.get(timeout=15) for _ in processes) == 1
        assert s.status()['reserved'] == 2
    finally:
        release.set()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
        results.close()
    assert all(process.exitcode == 0 for process in processes)
    assert s.status()['reserved'] == 0


def test_preflight_and_dashboard_do_not_create_ledger_or_switch(setup):
    s = setup
    assert s.status()['used'] == 0
    assert s.status()['next_node'] == 'B'
    assert rotation_status(s.root)['processed'] == 0
    assert not (s.root / 'state').exists()
    assert not s.controller.switches


def test_status_routes_are_read_only_and_publish_new_protocol_without_secrets(setup):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ournotes.proxy.ui.web import Extension

    s = setup
    reads = []

    def connection(*, force=False):
        reads.append(force)
        return {'state': 'connected', 'node': 'B', 'nodes': ['A', 'B', 'C'], 'group': 'Proxies'}

    extension = Extension(s.root)
    extension.connection = SimpleNamespace(read=connection)
    app = FastAPI()
    extension.mount(app)
    with TestClient(app) as client:
        for method, path in (('GET', '/api/launcher/clash/status'), ('POST', '/api/launcher/clash/check')):
            response = client.request(method, path)
            assert response.status_code == 200
            value = response.json()
            assert value['rotation']['protocol'] == 2
            assert value['rotation']['next_node'] == 'C'
            assert value['rotation']['used'] == 0
            assert not any(key in str(value) for key in ('Authorization', 'secret', 'password'))
    assert reads == [False, True]
    assert not (s.root / 'state').exists()


def test_only_actual_inputs_count_and_reservations_are_separate(setup):
    s = setup
    p = s.begin()
    assert s.status()['used'] == 0 and s.status()['reserved'] == 2
    assert p.send('guarded-out', 'login', lambda commit: False) is False
    assert s.status()['used'] == 0 and s.status()['reserved'] == 2
    send(p)
    assert s.status()['used'] == 1 and s.status()['reserved'] == 1
    send(p, 'otp-retry', 'login_retry')
    assert s.status()['used'] == 2 and s.status()['reserved'] == 0
    with pytest.raises(ValueError, match='预算'):
        send(p, 'illegal-third')
    p.close()
    assert s.status()['reserved'] == 0


@pytest.mark.parametrize('outcome', ['sent', 'changed', 'uncertain'])
def test_real_login_input_guard_commits_usage_and_intent_only_at_sdk_boundary(setup, outcome):
    from unittest.mock import MagicMock

    from PIL import Image
    from preset_fixtures import historical_plan

    from ournotes.runtime.model import PauseRun
    from ournotes.runtime.runner import Runner
    from ournotes.runtime.store import RuntimeStore
    from ournotes.vision.ocr import Observation, TextBox

    s = setup
    r = Runner()
    r.db = RuntimeStore(s.root)
    r.run = r.db.create_run('MyGO only', 'test', 1)
    r.account = r.db.reserve(r.run, historical_plan('MyGO only'))
    r.account['phase'] = 'login'
    r.ctx.update(email_focus=True, email_filled=True, agreement=True, mail_ready=True,
                 otp={'ticket': 'ticket', 'expires_at': 200_000})
    r.log = MagicMock()
    r.log.screenshot.return_value = 'memory'
    r.device = MagicMock()
    r.observation = Observation(Image.new('RGB', (1600, 900), 'white'),
                                [TextBox('发送验证码', 1, (872, 438, 991, 474))], 'email_form', 'login')
    changed = outcome == 'changed'
    r.device.capture.return_value = (Image.new('RGB', (1600, 900), 'black' if changed else 'white'), 0)
    if outcome == 'uncertain':
        r.device.tap.side_effect = OSError('uncertain device input')
    r.resumed_login_account = None
    r.login_flow.mail_submit = MagicMock()
    r.login_flow.check_login_feedback = MagicMock()
    r.task_permit = s.begin()
    try:
        if outcome == 'uncertain':
            with pytest.raises(PauseRun, match='禁止自动重发'):
                r.login_flow.login()
            assert r.stop.is_set() and r.ctx['login_network_uncertain']
            assert r.db.account(r.account['id'])['context']['login_network_uncertain']
            r.login_flow.mail_submit.assert_not_called()
        else:
            r.login_flow.login()
        assert s.status()['used'] == (0 if changed else 1)
        assert bool(r.ctx.get('send_intent')) is (not changed)
        assert r.device.tap.call_count == (0 if changed else 1)
        if outcome == 'sent':
            assert r.db.account(r.account['id'])['context']['send_intent']
            r.login_flow.mail_submit.assert_called_once_with('mark_sent', 'ticket')
    finally:
        r.release_proxy_task()
        r.db.close()


def test_finish_request_during_admission_wait_cannot_allocate_another_account(setup):
    from unittest.mock import Mock

    from ournotes.runtime.runner import Runner
    from ournotes.runtime.store import RuntimeStore

    s = setup
    r = Runner()
    r.db = RuntimeStore(s.root)
    r.run = r.db.create_run('MyGO only', 'test', 3)
    r.account = None
    r.publish = Mock()

    def waiting(*args, **kwargs):
        saved = r.db.get_run(r.run['id'])
        saved.update(finish_requested=True, count=0)
        r.db.save_run(saved)

    r.task_gate = SimpleNamespace(begin=Mock(side_effect=waiting))
    try:
        assert r.acquire_proxy_task() is False
        r.complete_without_new_task()
        r.task_gate.begin.assert_called_once()
        assert r.db.get_run(r.run['id'])['status'] == 'complete'
        assert r.db.db.execute('SELECT count(*) FROM allocation_intents').fetchone()[0] == 0
        assert r.db.db.execute('SELECT count(*) FROM accounts').fetchone()[0] == 0
    finally:
        r.db.close()


def test_resumed_legacy_account_reserves_only_future_retry_without_backfill(setup):
    from unittest.mock import Mock

    from preset_fixtures import historical_plan

    from ournotes.runtime.runner import Runner
    from ournotes.runtime.store import RuntimeStore

    s = setup
    r = Runner()
    r.db = RuntimeStore(s.root)
    r.run = r.db.create_run('MyGO only', 'test', 1)
    r.account = r.db.reserve(r.run, historical_plan('MyGO only'))
    r.account['phase'] = 'login'
    r.ctx.update(send_intent=True, otp={'ticket': 'historical', 'expires_at': 200_000})
    r.task_gate = s.gate
    r.log = Mock()
    try:
        assert r.acquire_proxy_task() is True
        assert s.status()['used'] == 0 and s.status()['reserved'] == 1
        assert 'proxy_task_id' in r.db.account(r.account['id'])['context']
    finally:
        r.release_proxy_task()
        r.db.close()


def test_rebind_send_boundary_counts_all_three_steps_but_not_confirmation(setup):
    from unittest.mock import Mock

    from ournotes.rebind.flow import Flow

    s = setup
    flow = Flow.__new__(Flow)
    flow.task = {'id': 'rebind'}
    flow.service = SimpleNamespace(stage=Mock())
    flow.arm = Mock()
    flow.proxy_permit = s.begin('rebind', 3, 'rebind')
    inputs = []

    def tap(expected, x, y, *, before_input=None):
        before_input()
        inputs.append(expected)

    flow.tap = tap
    for stage, page, ticket in [('login_send_intent', 'email_form', 'login-ticket'),
                                ('a_send_intent', 'verify_a', 'identity-ticket'),
                                ('b_send_intent', 'bind_form', None)]:
        flow.send_mail(stage, 'sending', page, 800, 500, ticket=ticket)
    assert s.status()['used'] == 3
    assert inputs == ['email_form', 'verify_a', 'bind_form']
    assert flow.arm.call_count == 2


def test_rebind_finishes_three_sends_before_switch_at_task_boundary(setup):
    s = setup
    s.seed('A', 149)
    p = s.begin('rebind', 3, 'rebind')
    assert s.begin('next') is None
    for stage in ('login', 'identity', 'target'):
        send(p, 'rebind-' + stage, stage)
    assert s.status()['used'] == 152
    assert s.status()['state'] == 'draining'
    assert not s.controller.switches
    assert s.begin('next') is None  # Zero remaining sends is still an active task.
    p.close()
    assert s.begin('next') is not None
    assert s.controller.switches == [('Proxies', 'B')]


def test_multiple_accounts_drain_and_never_switch_under_active_peer(setup):
    s = setup
    s.seed('A', 146)
    first, second = s.begin('first'), s.begin('second')
    assert s.begin('third') is None
    send(first, 'first-send')
    send(second, 'second-send')
    first.close()
    # The unused retry reservation is released; another task may fit before reaching 150.
    third = s.begin('third')
    assert third is not None
    assert s.begin('fourth') is None
    third.close()
    second.close()
    assert not s.controller.switches


def test_concurrent_admissions_share_one_durable_reservation_budget(setup):
    s = setup
    s.seed('A', 148)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: s.begin(f'lane-{i}'), range(8)))
    assert sum(p is not None for p in results) == 1
    assert s.status()['reserved'] == 2
    assert not s.controller.switches


def test_manual_selection_changes_candidate_and_charges_actual_node(setup):
    s = setup
    p = s.begin()
    s.controller.selected = 'B'  # Manually move to the previously displayed candidate.
    assert s.status()['next_node'] == 'C'
    send(p)
    rows = {row['name']: row for row in s.status()['nodes']}
    assert rows['A']['used'] == 0 and rows['B']['used'] == 1
    assert rows['B']['reserved'] == 1
    p.close()
    s.seed('B', 149)
    s.begin('next')
    assert s.controller.switches == [('Proxies', 'C')]


def test_wrap_skips_full_nodes_and_reordered_list_uses_actual_selection(setup):
    s = setup
    s.seed('A', 150)
    s.seed('C', 150)
    s.controller.nodes = ['B', 'A', 'C']
    s.controller.selected = 'C'
    s.begin()
    assert s.controller.switches == [('Proxies', 'B')]


def test_removed_and_readded_node_retains_usage_across_groups(setup):
    s = setup
    s.seed('B', 150)
    s.controller.nodes = ['A', 'C']
    s.begin().close()
    s.controller.nodes = ['A', 'B', 'C']
    s.controller.group = 'GLOBAL'
    s.controller.selected = 'B'
    s.begin('next')
    assert s.controller.switches == [('GLOBAL', 'C')]


def test_rolling_window_exact_boundary_and_restart_persist_usage(setup):
    s = setup
    s.seed('A', 150)
    replacement = TaskProxyGate(s.root, controller=s.controller, clock=lambda: s.clock[0])
    assert usage_status(s.root, now=s.clock[0])['used'] == 0  # No selector has been recorded yet.
    s.clock[0] += WINDOW - .01
    assert s.status()['used'] == 150
    s.clock[0] += .01
    assert s.status()['used'] == 0
    p = replacement.begin('new-batch', 2)
    try:
        send(p)
        assert s.status()['used'] == 1
    finally:
        p.close()


def test_exhaustion_reports_actual_earliest_admission_and_requires_manual_resume(setup):
    s = setup
    s.seed('A', 2, s.clock[0] - 1000)
    s.seed('A', 150, s.clock[0] - 500)
    s.seed('B', 150, s.clock[0] - 100)
    s.seed('C', 150)
    with pytest.raises(ProxyQuotaError, match='手动恢复'):
        s.begin()
    assert s.status()['earliest_available_at'] == s.clock[0] - 500 + WINDOW
    assert not s.controller.switches
    s.clock[0] += WINDOW
    assert s.begin() is not None


@pytest.mark.parametrize('failure', ['before', 'after'])
def test_unconfirmed_switch_does_not_replay_put_on_restart(setup, failure):
    s = setup
    s.seed('A', 150)
    s.controller.fail = failure
    with pytest.raises(ValueError, match='未确认'):
        s.begin()
    assert s.status()['state'] == 'uncertain'
    s.controller.fail = None
    if failure == 'before':
        with pytest.raises(ValueError, match='禁止重放'):
            s.begin('retry')
        s.controller.selected = 'B'  # Human verifies/sets the saved target.
    s.begin('retry')
    assert len(s.controller.switches) == 1
    assert s.status()['switch_count'] == 1


def test_unknown_input_is_charged_and_duplicate_recovery_cannot_resend(setup):
    s = setup
    p = s.begin()

    def unknown(commit):
        commit()
        raise OSError('lost SDK response')

    with pytest.raises(OSError):
        p.send('unknown', 'login', unknown)
    assert s.status()['used'] == 1
    p.close()
    resumed = s.begin(resume=True)
    assert s.status()['reserved'] == 1
    with pytest.raises(ValueError, match='禁止重复'):
        resumed.send('unknown', 'login', lambda commit: pytest.fail('input replay'))
    assert s.status()['used'] == 1
    with closing(sqlite3.connect(s.gate.store.path)) as db:
        assert db.execute('SELECT status FROM sends WHERE id=?', ('unknown',)).fetchone()[0] == 'uncertain'


def test_stale_task_lease_releases_only_reservations_not_unknown_usage(setup):
    s = setup
    p = s.begin()
    send(p)
    p.lease.__exit__(None, None, None)  # Emulate process exit without writing task completion.
    p.closed = True
    assert s.status()['used'] == 1 and s.status()['reserved'] == 0
    s.begin('replacement')
    assert s.status()['used'] == 1 and s.status()['reserved'] == 2


def test_manual_change_to_node_at_actual_limit_blocks_additional_mail(setup):
    s = setup
    p = s.begin()
    s.seed('B', 200)
    s.controller.selected = 'B'
    with pytest.raises(ValueError, match='200'):
        send(p)
    assert s.status()['used'] == 200


def test_legacy_batch_counts_do_not_backfill_usage_or_fix_next_target(setup):
    from ournotes.runtime.store import RuntimeStore
    s = setup
    db = RuntimeStore(s.root)
    try:
        run = db.create_run('MyGO only', 'test', 1)
        run['completed'] = 900
        db.save_run(run)
        db.save_batch({'id': 'old', 'created_at': 1, 'status': 'complete',
                       'lanes': [{'run_id': run['id']}],
                       'proxy_rotation': {'nodes': ['A', 'B'], 'position': 0, 'boundary': 750}})
        connection = {'state': 'connected', 'nodes': s.controller.nodes, 'node': 'B', 'group': 'Proxies'}
        before = db.get_batch()
        value = rotation_status(s.root, connection)
        assert value['processed'] == 900 and value['used'] == 0
        assert value['batch_state'] == 'complete' and value['next_node'] == 'C'
        assert db.get_batch() == before
        assert not s.gate.store.path.exists()
    finally:
        db.close()


@pytest.mark.parametrize('chunked',[True,False])
def test_pipe_http_response_handles_short_reads(monkeypatch, chunked):
    import io

    from ournotes.proxy.rotation import ClashController
    payload=b'{"proxies":{}}'
    body=(f'{len(payload):x}\r\n'.encode()+payload+b'\r\n0\r\n\r\n') if chunked else payload
    header=b'Transfer-Encoding: chunked' if chunked else f'Content-Length: {len(payload)}'.encode()
    wire=b'HTTP/1.1 200 OK\r\n'+header+b'\r\n\r\n'+body
    class ShortPipe(io.RawIOBase):
        def __init__(self):self.source=io.BytesIO(wire)
        def readable(self):return True
        def readinto(self, target):
            value=self.source.read(min(3,len(target)))
            target[:len(value)]=value
            return len(value)
        def write(self,data):return len(data)
    controller=ClashController.__new__(ClashController)
    controller.pipe,controller.secret=r'\\.\pipe\test','test'
    monkeypatch.setattr('ournotes.platforms.proxy.open_pipe', lambda _: ShortPipe())
    assert controller.request('/proxies')=={'proxies':{}}


@pytest.mark.parametrize('method', ['GET','PUT'])
def test_incomplete_response_retries_only_reads(monkeypatch, method):
    import http.client
    from unittest.mock import Mock

    from ournotes.proxy.rotation import ClashController
    controller=ClashController.__new__(ClashController)
    call=Mock(side_effect=[http.client.IncompleteRead(b''),{'ok':True}])
    monkeypatch.setattr(controller,'_request_once',call)
    monkeypatch.setattr('ournotes.proxy.rotation.time.sleep',lambda _:None)
    if method=='GET':
        assert controller.request('/proxies',method=method)=={'ok':True}
        assert call.call_count==2
    else:
        with pytest.raises(http.client.IncompleteRead):controller.request('/proxies',{'name':'B'},method=method)
        assert call.call_count==1


def test_read_failures_are_bounded_without_quota_changes(monkeypatch):
    import http.client
    from unittest.mock import Mock

    from ournotes.proxy.rotation import ClashController
    controller=ClashController.__new__(ClashController)
    call=Mock(side_effect=http.client.IncompleteRead(b''))
    monkeypatch.setattr(controller,'_request_once',call)
    monkeypatch.setattr('ournotes.proxy.rotation.time.sleep',lambda _:None)
    with pytest.raises(ValueError,match='节点信息读取连续失败'):controller.request('/proxies')
    assert call.call_count==3


def test_report_limit_charges_original_node_and_allows_continuous_rotation(tmp_path):
    from ournotes.proxy.usage import snapshot
    controller = Controller()
    gate = TaskProxyGate(tmp_path, controller=controller)
    first = gate.begin('first', 2)
    peer = gate.begin('peer', 2)
    try:
        first.send('send-a', 'login', lambda commit: commit())
        peer.send('peer-a', 'login', lambda commit: commit())
        gate.report_mail_limit('send-a')
        assert controller.selected == 'B'
        gate.report_mail_limit('peer-a')
        gate.report_mail_limit('send-a')
        assert len(controller.switches) == 1
        first.send('send-b', 'login', lambda commit: commit())
        gate.report_mail_limit('send-b')
        assert controller.selected == 'C'
        first.send('send-c', 'login', lambda commit: commit())
        with pytest.raises(ValueError, match='全部代理'):
            gate.report_mail_limit('send-c')
        with gate.session() as db:
            assert snapshot(db, gate.store.clock())[0] == {'A': 200, 'B': 200, 'C': 200}
            assert db.execute('SELECT count(*) FROM sends').fetchone()[0] == 4
    finally:
        first.close()
        peer.close()

