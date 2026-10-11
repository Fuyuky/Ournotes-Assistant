"""Shared Clash Verge rotation; private controller secrets never enter state."""
from __future__ import annotations

import http.client
import json
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from ournotes.common.files import atomic_json
from ournotes.common.secrets import SecretStore
from ournotes.paths import DATA_ROOT
from ournotes.proxy.usage import (
    LIMIT,
    THRESHOLD,
    UsageStore,
    available,
    candidate,
    snapshot,
    task_lock,
    usage_status,
)


class ClashController:
    def __init__(self):
        from ournotes.platforms.proxy import controller_config
        config = controller_config()
        self.pipe = config.get('external-controller-pipe')
        self.unix = config.get('external-controller-unix')
        self.address = config.get('external-controller')
        self.secret = config.get('secret', '')

    def request(self, path, body=None, *, method=None):
        method = method or ('GET' if body is None else 'PUT')
        for attempt in range(3):
            try:
                return self._request_once(path, body, method=method)
            except (http.client.HTTPException, ConnectionError, TimeoutError) as error:
                if method != 'GET' or body is not None:
                    raise
                if attempt == 2:
                    raise ValueError('Clash 节点信息读取连续失败，请检查本机 Clash 服务后恢复。') from error
                time.sleep(.2 * (attempt + 1))

    def _request_once(self, path, body=None, *, method):
        from ournotes.platforms.proxy import request
        return request(path, body, method=method, pipe=getattr(self, 'pipe', None),
                       unix=getattr(self, 'unix', None), address=getattr(self, 'address', None),
                       secret=self.secret)

    def selection(self):
        proxies = self.request('/proxies')['proxies']
        mode = self.request('/configs').get('mode')
        group = 'GLOBAL' if mode == 'global' else 'Proxies'
        if group not in proxies or proxies[group]['type'] != 'Selector':
            raise ValueError('Clash Verge 缺少可手动切换的 Proxies 节点组。')
        nodes = []

        def expand(name, visited):
            if name in visited:
                return
            item = proxies[name]
            if 'all' in item:
                for child in item['all']:
                    expand(child, visited | {name})
            elif item['type'] not in ('Direct', 'Reject', 'RejectDrop', 'Compatible', 'Pass') and name not in nodes:
                nodes.append(name)

        expand(group, set())
        selected = proxies[group]['now']
        visited = set()
        while 'now' in proxies.get(selected, {}):
            if selected in visited:
                raise ValueError('Clash Verge 当前节点组选择有歧义。')
            visited.add(selected)
            selected = proxies[selected]['now']
        if selected not in nodes:
            raise ValueError('Clash Verge 当前未选择具体代理节点。')
        return group, nodes, selected

    def switch(self, group, target):
        self.request('/proxies/' + quote(group, safe=''), {'name': target})
        if self.request('/proxies/' + quote(group, safe=''))['now'] != target:
            raise ValueError('Clash Verge 节点切换未确认。')


class ProxyQuotaError(ValueError):
    """All already admitted tasks have drained; another task needs manual resume."""


class ProxyCoordinator:
    """One cross-process controller queue for dashboard, batch and rebind callers."""
    def __init__(self, root=DATA_ROOT, *, controller=None, clock=time.time):
        self.root = Path(root)
        self.controller = controller or ClashController()
        self.clock = clock
        self.cache = self.root / 'state/proxy-selection.json'

    @property
    def pipe(self):
        return self.controller.pipe

    def selection(self, *, fresh=False):
        with SecretStore(self.root / 'state').lock('proxy-controller', wait=True):
            if not fresh:
                try:
                    saved = json.loads(self.cache.read_text('utf-8'))
                    if 0 <= self.clock() - saved['observed_at'] < 1:
                        return saved['group'], saved['nodes'], saved['selected']
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            group, nodes, selected = self.controller.selection()
            atomic_json(self.cache, {'observed_at': self.clock(), 'group': group,
                                     'nodes': nodes, 'selected': selected})
            return group, nodes, selected

    def switch(self, group, target):
        with SecretStore(self.root / 'state').lock('proxy-controller', wait=True):
            # Invalidate before PUT, including ambiguous results. Do not replay it.
            self.cache.unlink(missing_ok=True)
            self.controller.switch(group, target)


class TaskProxyGate:
    """Serialize admissions, switches and actual mail inputs across local processes."""
    interval = THRESHOLD

    def __init__(self, root=DATA_ROOT, *, controller=None, clock=time.time):
        self.root = Path(root)
        self.store = UsageStore(root, clock)
        self.controller = controller or ProxyCoordinator(root)
        self.controller.selection()  # Read-only preflight; no ledger or device is touched.

    @contextmanager
    def session(self):
        with SecretStore(self.root / 'state').lock('proxy-usage', wait=True), closing(self.store.connect()) as db:
            yield db

    def observe(self, db, *, fresh=False):
        group, nodes, selected = (self.controller.selection(fresh=fresh)
                                  if isinstance(self.controller, ProxyCoordinator)
                                  else self.controller.selection())
        self.store.observe(db, group, nodes, selected)
        # All game traffic shares this selector. Manual changes move outstanding reservations,
        # while already submitted mail remains charged to the originally observed node.
        db.execute('UPDATE tasks SET node=? WHERE active=1', (selected,))
        pending = db.execute("SELECT id,target FROM switches WHERE status='pending' ORDER BY id DESC LIMIT 1").fetchone()
        if pending:
            if selected != pending[1]:
                raise ValueError('代理节点切换尚未确认；请核对保存的目标后手动恢复，禁止重放切换。')
            db.execute("UPDATE switches SET status='confirmed' WHERE id=?", (pending[0],))
        return group, nodes, selected

    def begin(self, task_id, budget, kind='account', *, resume=False):
        """Return None only while live peers are finishing; never switch under an active task."""
        if budget not in (0, 1, 2, 3) or kind not in ('account', 'rebind'):
            raise ValueError('代理任务发信预算无效。')
        with self.session() as db:
            group, nodes, selected = self.observe(db)
            old = db.execute('SELECT remaining,kind FROM tasks WHERE id=?', (task_id,)).fetchone()
            if old:
                if old[1] != kind:
                    raise ValueError('代理任务身份不一致。')
                budget = min(budget, old[0])
            counts, reserved, tasks, _ = snapshot(db, self.store.clock())
            capacity = counts.get(selected, 0) + reserved.get(selected, 0)
            if not ((resume and (budget == 0 or capacity + budget <= LIMIT))
                    or (kind == 'rebind' and capacity + budget <= LIMIT)
                    or available(selected, counts, reserved, budget)):
                if tasks:
                    return None
                target = candidate(nodes, selected, counts, reserved, budget)
                if target is None:
                    value = usage_status(self.root, {'state': 'connected', 'nodes': nodes, 'node': selected,
                                                    'group': group}, now=self.store.clock())
                    earliest = value['earliest_available_at']
                    suffix = ''
                    if earliest:
                        when = datetime.fromtimestamp(earliest, UTC).astimezone(timezone(timedelta(hours=8)))
                        suffix = '；最早可恢复时间 ' + when.strftime('%m-%d %H:%M:%S') + '（北京时间）'
                    raise ProxyQuotaError('全部代理节点24小时额度已用完，当前任务已收尾，请手动恢复' + suffix)
                switch_id = db.execute('INSERT INTO switches(source,target,group_name,created,status) '
                                       "VALUES(?,?,?,?,'pending')",
                                       (selected, target, group, self.store.clock())).lastrowid
                self.controller.switch(group, target)
                confirmed_group, confirmed_nodes, confirmed = self.controller.selection()
                if confirmed_group != group or confirmed != target:
                    raise ValueError('Clash Verge 节点切换未确认。')
                db.execute("UPDATE switches SET status='confirmed' WHERE id=?", (switch_id,))
                self.store.observe(db, confirmed_group, confirmed_nodes, confirmed)
                selected = confirmed
            lease = SecretStore(self.root / 'state').lock(task_lock(task_id))
            lease.__enter__()
            try:
                db.execute('INSERT INTO tasks VALUES(?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET '
                           'node=excluded.node,remaining=excluded.remaining,active=1',
                           (task_id, selected, budget, kind))
            except BaseException:
                lease.__exit__(None, None, None)
                raise
            return TaskPermit(self, task_id, lease)

    def report_mail_limit(self, request_id):
        """Charge the node of the rejected send, even if another lane already switched."""
        with self.session() as db:
            group, nodes, selected = self.observe(db, fresh=True)
            send = db.execute('SELECT node,task_id FROM sends WHERE id=?', (request_id,)).fetchone()
            if not send:
                raise ValueError('邮箱限额缺少原发信节点记录，保留现场核查。')
            db.execute('BEGIN IMMEDIATE')
            try:
                inserted = db.execute('INSERT OR IGNORE INTO node_limits VALUES(?,?,?)',
                                      (request_id, send[0], self.store.clock())).rowcount
                if inserted:
                    db.execute('UPDATE tasks SET remaining=remaining+1 WHERE id=?', (send[1],))
                db.execute('COMMIT')
            except BaseException:
                db.execute('ROLLBACK')
                raise
            counts, reserved, _, _ = snapshot(db, self.store.clock())
            if counts.get(selected, 0) < LIMIT:
                return  # A peer has already moved off the rejected node.
            budget = sum(reserved.values())
            target = candidate(nodes, selected, counts, {}, budget)
            if target is None:
                raise ProxyQuotaError('全部代理节点额度已用完，保留账号等待手动恢复。')
            switch_id = db.execute('INSERT INTO switches(source,target,group_name,created,status) '
                                  "VALUES(?,?,?,?,'pending')",
                                  (selected, target, group, self.store.clock())).lastrowid
            self.controller.switch(group, target)
            confirmed_group, confirmed_nodes, confirmed = self.controller.selection()
            if confirmed_group != group or confirmed != target:
                raise ValueError('Clash Verge 节点切换未确认。')
            db.execute("UPDATE switches SET status='confirmed' WHERE id=?", (switch_id,))
            self.store.observe(db, confirmed_group, confirmed_nodes, confirmed)
            db.execute('UPDATE tasks SET node=? WHERE active=1', (confirmed,))


class TaskPermit:
    def __init__(self, gate, task_id, lease):
        self.gate, self.task_id, self.lease = gate, task_id, lease
        self.closed = False

    def reserve_rebind_mail(self):
        """An explicit replacement after confirmed SDK rejection needs a new quota unit."""
        if self.closed:
            raise ValueError('代理任务已结束，禁止发信。')
        with self.gate.session() as db:
            _, _, selected = self.gate.observe(db, fresh=True)
            task = db.execute('SELECT remaining,kind FROM tasks WHERE id=? AND active=1',
                              (self.task_id,)).fetchone()
            if not task or task[1] != 'rebind' or task[0] != 0:
                raise ValueError('当前任务不能预留换绑追加发信额度。')
            counts, reserved, _, _ = snapshot(db, self.gate.store.clock())
            if counts.get(selected, 0) + reserved.get(selected, 0) + 1 > LIMIT:
                raise ProxyQuotaError('当前节点发信额度不足，未向新目标邮箱发送邮件；请停止后核查。')
            db.execute('UPDATE tasks SET remaining=remaining+1 WHERE id=?', (self.task_id,))

    def send(self, request_id, purpose, perform):
        """perform(commit) must call commit after guards, immediately before the SDK input."""
        if self.closed:
            raise ValueError('代理任务已结束，禁止发信。')
        with self.gate.session() as db:
            _, _, selected = self.gate.observe(db)
            if db.execute('SELECT 1 FROM sends WHERE id=?', (request_id,)).fetchone():
                raise ValueError('原发信请求已有提交记录；保留票据核查，禁止重复发送。')
            committed = False

            def commit():
                nonlocal committed, selected
                if committed:
                    raise ValueError('同一发信请求不能提交两次。')
                _, _, selected = self.gate.observe(db, fresh=True)  # Recheck actual node immediately before input.
                db.execute('BEGIN IMMEDIATE')
                try:
                    remaining = db.execute('SELECT remaining FROM tasks WHERE id=? AND active=1',
                                           (self.task_id,)).fetchone()
                    if not remaining or remaining[0] <= 0:
                        raise ValueError('当前任务的代理发信预算已用完；禁止开启额外发信轮次。')
                    used = snapshot(db, self.gate.store.clock())[0].get(selected, 0)
                    if used >= LIMIT:
                        raise ValueError('实际当前节点已达到200次限制，请核查手动节点变更；未发送新邮件。')
                    db.execute("INSERT INTO sends VALUES(?,?,?,?,?,'uncertain')",
                               (request_id, self.task_id, selected, purpose, self.gate.store.clock()))
                    db.execute('UPDATE tasks SET remaining=remaining-1 WHERE id=?', (self.task_id,))
                    db.execute('COMMIT')
                    committed = True
                except BaseException:
                    db.execute('ROLLBACK')
                    raise

            result = perform(commit)
            if committed:
                db.execute("UPDATE sends SET status='submitted' WHERE id=?", (request_id,))
            return result

    def close(self):
        if self.closed:
            return
        try:
            with self.gate.session() as db:
                db.execute('UPDATE tasks SET active=0 WHERE id=?', (self.task_id,))
        finally:
            self.closed = True
            self.lease.__exit__(None, None, None)
