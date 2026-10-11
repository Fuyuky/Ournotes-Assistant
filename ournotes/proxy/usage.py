"""Private, durable rolling mail usage shared by development executors."""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

from ournotes.common.devices import lock_held

WINDOW = 24 * 60 * 60
THRESHOLD = 150
LIMIT = 200


def task_lock(task_id):
    import hashlib
    parts = task_id.split('-')
    # Reuse a run/executor lease file between its sequential tasks; do not leave one lock per account.
    owner = '-'.join(parts[:2]) if len(parts) == 3 and parts[0] == 'account' else task_id
    if task_id.startswith('rebind-'):
        owner = 'rebind'
    return 'proxy-task-' + hashlib.sha256(owner.encode()).hexdigest()


def snapshot(db, now, *, root=None):
    counts = dict(db.execute('SELECT node, count(*) FROM sends WHERE created>? GROUP BY node',
                             (now - WINDOW,)))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='node_limits'").fetchone():
        for node, in db.execute('SELECT DISTINCT node FROM node_limits WHERE created>?', (now - WINDOW,)):
            counts[node] = max(LIMIT, counts.get(node, 0))
    reserved = {}
    tasks = []
    for task_id, node, remaining, kind in db.execute(
            'SELECT id,node,remaining,kind FROM tasks WHERE active=1'):
        if root is None or lock_held(root, task_lock(task_id)):
            reserved[node] = reserved.get(node, 0) + remaining
            tasks.append({'id': task_id, 'node': node, 'remaining': remaining, 'kind': kind})
    expiry = dict(db.execute('SELECT node,min(created)+? FROM sends WHERE created>? GROUP BY node',
                             (WINDOW, now - WINDOW)))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='node_limits'").fetchone():
        for node, until in db.execute('SELECT node,max(created)+? FROM node_limits WHERE created>? GROUP BY node',
                                      (WINDOW, now - WINDOW)):
            expiry[node] = max(until, expiry.get(node, 0))
    return counts, reserved, tasks, expiry


def available(node, counts, reserved, budget=3):
    used = counts.get(node, 0)
    pending = reserved.get(node, 0)
    return used + pending < THRESHOLD and used + pending + budget <= LIMIT


def candidate(nodes, selected, counts, reserved, budget=3):
    position = nodes.index(selected)
    order = nodes[position + 1:] + nodes[:position]
    return next((node for node in order if available(node, counts, reserved, budget)), None)


def earliest_admission(db, names, reserved, now):
    times = []
    blocked = (dict(db.execute('SELECT node,max(created)+? FROM node_limits WHERE created>? GROUP BY node',
                               (WINDOW, now - WINDOW)))
               if db.execute("SELECT 1 FROM sqlite_master WHERE name='node_limits'").fetchone() else {})
    for node in names:
        events = [row[0] for row in db.execute(
            'SELECT created FROM sends WHERE node=? AND created>? ORDER BY created', (node, now - WINDOW))]
        needed = len(events) + reserved.get(node, 0) - THRESHOLD + 1
        if 0 < needed <= len(events):
            times.append(max(events[needed - 1] + WINDOW, blocked.get(node, 0)))
        elif node in blocked:
            times.append(blocked[node])
    return min(times) if times else None


class UsageStore:
    def __init__(self, root, clock=time.time):
        self.root, self.clock = Path(root), clock
        self.path = self.root / 'state/proxy-usage.sqlite3'

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version > 1:
            db.close()
            raise ValueError('代理用量台账来自较新版本，禁止降级写入。')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS nodes(name TEXT PRIMARY KEY, position INTEGER, seen REAL);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, node TEXT NOT NULL,
                remaining INTEGER NOT NULL, kind TEXT NOT NULL, active INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS sends(id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                node TEXT NOT NULL, purpose TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS sends_window ON sends(node,created);
            CREATE TABLE IF NOT EXISTS switches(id INTEGER PRIMARY KEY, source TEXT NOT NULL,
                target TEXT NOT NULL, group_name TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS node_limits(request_id TEXT PRIMARY KEY, node TEXT NOT NULL,
                created REAL NOT NULL);
            PRAGMA user_version=1;
        ''')
        return db

    def observe(self, db, group, nodes, selected):
        now = self.clock()
        db.execute('BEGIN IMMEDIATE')
        try:
            db.execute('UPDATE nodes SET position=NULL')
            db.executemany('INSERT INTO nodes VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET '
                           'position=excluded.position,seen=excluded.seen',
                           ((node, i, now) for i, node in enumerate(nodes)))
            for key, value in (('group', group), ('selected', selected), ('observed_at', str(now))):
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, value))
            # An OS lease, not a timestamp or PID, determines whether work is still alive.
            for (task_id,) in db.execute('SELECT id FROM tasks WHERE active=1').fetchall():
                if not lock_held(self.root, task_lock(task_id)):
                    db.execute('UPDATE tasks SET active=0 WHERE id=?', (task_id,))
            db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK')
            raise


def usage_status(root, connection=None, *, now=None):
    """Read-only projection: never initialize a ledger from a dashboard request."""
    root = Path(root).resolve()
    now = time.time() if now is None else now
    result = {'protocol': 2, 'threshold': THRESHOLD, 'limit': LIMIT, 'window_seconds': WINDOW,
              'used': 0, 'reserved': 0, 'nodes': [], 'next_node': None,
              'earliest_available_at': None, 'switch_count': 0, 'last_switch': None,
              'state': 'idle', 'message': '等待任务；节点用量从新版启用后开始记录'}
    path = root / 'state/proxy-usage.sqlite3'
    connected = connection and connection.get('state') == 'connected'
    names = connection.get('nodes', []) if connected else []
    selected = connection.get('node') if connected else None
    counts, reserved, tasks, expiry = {}, {}, [], {}
    meta = {}
    pending = None
    if path.exists():
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                db.execute('BEGIN')
                if db.execute('PRAGMA user_version').fetchone()[0] > 1:
                    raise ValueError('newer proxy ledger')
                meta = dict(db.execute('SELECT key,value FROM meta'))
                if not connected:
                    names = [row[0] for row in db.execute(
                        'SELECT name FROM nodes WHERE position IS NOT NULL ORDER BY position')]
                    selected = meta.get('selected')
                counts, reserved, tasks, expiry = snapshot(db, now, root=root)
                if connected and tasks:
                    reserved = {selected: sum(task['remaining'] for task in tasks)}
                pending = db.execute("SELECT target FROM switches WHERE status='pending' ORDER BY id DESC LIMIT 1").fetchone()
                history = db.execute("SELECT target,created FROM switches WHERE status='confirmed' ORDER BY id DESC LIMIT 1").fetchone()
                result['switch_count'] = db.execute("SELECT count(*) FROM switches WHERE status='confirmed'").fetchone()[0]
                if history:
                    result['last_switch'] = {'node': history[0], 'at': history[1]}
                result['earliest_available_at'] = earliest_admission(db, names, reserved, now)
        except (sqlite3.Error, OSError, ValueError):
            return {**result, 'state': 'unavailable', 'message': '代理用量台账暂时不可读；禁止无保护地发信'}
    result.update(node=selected, group=connection.get('group') if connected else meta.get('group'),
                  used=counts.get(selected, 0), reserved=reserved.get(selected, 0),
                  active_tasks=len(tasks), selection_confirmed=bool(connected))
    result['nodes'] = [{'name': n, 'used': counts.get(n, 0), 'reserved': reserved.get(n, 0),
                        'remaining': max(THRESHOLD - counts.get(n, 0) - reserved.get(n, 0), 0),
                        'available': available(n, counts, reserved), 'next_release_at': expiry.get(n),
                        'current': n == selected} for n in names]
    result['remaining_nodes'] = sum(n['available'] and not n['current'] for n in result['nodes'])
    if selected in names:
        result['next_node'] = candidate(names, selected, counts, reserved)
    if pending:
        result.update(state='uncertain', pending_node=pending[0], message='节点切换尚未确认，请核查后手动恢复')
    elif selected in names and not available(selected, counts, reserved):
        result.update(state='draining' if tasks else 'exhausted',
                      message='达到节点额度，等待当前任务结束后切换' if tasks else
                      ('下一任务开始前切换节点' if result['next_node'] else '全部节点额度已用完，等待手动恢复'))
    elif tasks:
        result.update(state='running', message='当前任务执行中，按实际发信累计')
    elif names:
        result.update(state='ready', message='节点有可用额度；下一任务开始前重新核对')
    return result
