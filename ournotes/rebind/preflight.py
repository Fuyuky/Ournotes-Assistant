"""Read-only rebind guards and safe, actionable startup diagnostics."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from ournotes.common.devices import DEVICE_LEASE_PROTOCOL, lock_held


class PreflightError(ValueError):
    def __init__(self, code, message, hint, *, source='safety-check', cause=None):
        super().__init__(message + '，未启动换绑。')
        self.info = {'phase': 'preflight', 'code': code, 'source': source,
                     'message': str(self), 'hint': hint}
        if cause is not None:
            self.info['exception_type'] = type(cause).__name__
            for key in ('errno', 'winerror', 'sqlite_errorcode'):
                value = getattr(cause, key, None)
                if type(value) is int:
                    self.info[key] = value
            if isinstance(cause, KeyError) and cause.args and cause.args[0] in ('id', 'status', 'lanes', 'instance'):
                self.info['field'] = cause.args[0]


def _batch(raw):
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value['lanes'], list):
        raise TypeError('invalid batch shape')
    if not isinstance(value['id'], str) or not isinstance(value['status'], str):
        raise TypeError('invalid batch identity')
    if any(not isinstance(lane, dict) or type(lane['instance']) is not int for lane in value['lanes']):
        raise TypeError('invalid lane shape')
    return value


def _read_error(error, source):
    retry = '清除本次未启动任务后重试；若持续出现，请检查本机运行状态。'
    if isinstance(error, sqlite3.Error):
        code = getattr(error, 'sqlite_errorcode', 0) or 0
        if (code & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            kind, message = 'runtime_db_busy', '本机运行数据库正被占用，读取等待已超时'
        elif str(error).lower().startswith(('no such table:', 'no such column:')):
            kind, message = 'runtime_db_schema', '本机运行数据库缺少所需表或字段'
            retry = '请核对控制台与运行数据库版本，完成更新后再试；不要删除运行数据库。'
        elif 'malformed json' in str(error).lower():
            kind, message = 'runtime_record_invalid', '本机运行记录的 JSON 内容无法解析'
            retry = '请保留运行数据并核查对应记录，修复前不要启动换绑。'
        else:
            kind, message = 'runtime_db_read_failed', '本机运行数据库读取失败'
            retry = '请核对数据库访问权限和磁盘状态；诊断信息保留了 SQLite 错误码。'
    elif isinstance(error, FileNotFoundError):
        if source == 'runner-status':
            kind, message = 'runner_status_missing', '运行锁仍被持有，但调度状态文件不存在'
            retry = '等待调度器初始化或收尾；若持续出现，安全暂停后重启新版控制台。'
        else:
            kind, message = 'runtime_db_missing', '本机运行数据库在检查期间消失'
    elif isinstance(error, PermissionError):
        kind = 'runner_status_access_denied' if source == 'runner-status' else 'runtime_db_access_denied'
        message = ('调度状态文件' if source == 'runner-status' else '本机运行数据库') + '读取被拒绝，可能被占用或受权限限制'
    elif isinstance(error, json.JSONDecodeError):
        kind = 'runner_status_invalid' if source == 'runner-status' else 'runtime_record_invalid'
        message = '调度状态文件内容无法解析' if source == 'runner-status' else '本机运行记录的 JSON 内容无法解析'
        retry = '请保留状态文件并核查内容；不要清空数据库或解除账号保护。'
    elif isinstance(error, (KeyError, TypeError)):
        kind = 'runner_status_invalid' if source == 'runner-status' else 'runtime_record_invalid'
        message = '调度状态字段缺失或格式不符' if source == 'runner-status' else '本机批次记录字段缺失或格式不符'
        retry = '请核对控制台与运行记录版本；保留原数据，修复字段后再试。'
    else:
        kind = 'runner_status_read_failed' if source == 'runner-status' else 'runtime_db_read_failed'
        message = ('调度状态文件' if source == 'runner-status' else '本机运行数据库') + '读取失败'
    return PreflightError(kind, message, retry, source=source, cause=error)


def idle_batch(root, instance, index, account_id=None):
    """Check reservations under the caller's device lease, without repairing data."""
    path = Path(root) / 'state/runtime.sqlite3'
    batch = None
    source = 'runtime-db'
    try:
        if path.exists():
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                db.execute('PRAGMA query_only=ON')
                db.execute('BEGIN')
                row = db.execute('SELECT data FROM batches ORDER BY created DESC LIMIT 1').fetchone()
                batch = _batch(row[0]) if row else None
                for (data,) in db.execute("SELECT data FROM batches WHERE json_extract(data,'$.status') NOT IN ('complete','cancelled')"):
                    if any(lane['instance'] == instance for lane in _batch(data)['lanes']):
                        raise PreflightError('instance_reserved', f'MuMu 实例 {instance} 属于未结束批次',
                                             '请选择批次之外的空闲实例；暂停实例也不能借用。')
                if db.execute("SELECT 1 FROM runs WHERE json_extract(data,'$.instance')=? "
                              "AND json_extract(data,'$.status') NOT IN ('complete','cancelled')", (instance,)).fetchone():
                    raise PreflightError('instance_unfinished', f'MuMu 实例 {instance} 仍有未结束任务',
                                         '请等待该任务正常结束，或选择其他空闲实例。')
                for aid, status, local_index in db.execute('SELECT id,status,alias_index FROM accounts WHERE alias_index=? OR id=?', (index, account_id)):
                    if account_id is not None and (aid != account_id or local_index != index):
                        raise PreflightError('account_identity_mismatch', '本机账号与云端身份不一致',
                                             '请核查该编号的本机档案与云端成果身份，不要重复执行换绑。')
                    if status != 'complete' or db.execute(
                        "SELECT 1 FROM runs WHERE json_extract(data,'$.active_account')=?", (aid,)
                    ).fetchone():
                        raise PreflightError('account_unfinished', '该账号仍在批次中执行或尚未完整完成',
                                             '请等待原账号完成；不能通过清除任务绕过账号检查。')
                    if db.execute("SELECT 1 FROM draws WHERE account_id=? AND status IN "
                                  "('submitted','captured','needs_review')", (aid,)).fetchone():
                        raise PreflightError('account_unresolved', '该账号存在未决或待复核交易',
                                             '请先核查原交易与证据，不能重新提交抽卡或换绑。')
        source = 'runner-status'
        if lock_held(root, 'runner'):
            if lock_held(root, 'storage-maintenance'):
                raise PreflightError('storage_maintenance', '控制台正在整理存储',
                                     '请等待存储整理完成、运行锁释放后再试。', source=source)
            status = json.loads((Path(root) / 'state/runner-status.json').read_text('utf-8'))
            if not isinstance(status, dict):
                raise TypeError('invalid runner status shape')
            if batch and batch['status'] in ('complete', 'cancelled'):
                raise PreflightError('runner_finishing', '批次已结束，调度器仍在收尾并持有运行锁',
                                     '请等待实例及运行锁释放后再试。', source=source)
            if not batch or batch['status'] != 'running' or status.get('status') != 'running':
                raise PreflightError('runner_state_mismatch', '运行锁与批次运行状态不一致',
                                     '请等待初始化或收尾完成；若持续出现，安全暂停后重启控制台。', source=source)
            if status.get('batch_id') != batch['id']:
                raise PreflightError('runner_batch_mismatch', '数据库批次与调度状态中的批次不一致',
                                     '请刷新并核对当前批次；不能用旧状态并行启动换绑。', source=source)
            if status.get('device_lease_protocol') != DEVICE_LEASE_PROTOCOL:
                raise PreflightError('runner_protocol_old', '当前控制者不支持实例并行协议',
                                     '请安全暂停批次，退出并重开新版控制台后再试。', source=source)
            if any(not lock_held(root, f"device-{lane['instance']}") for lane in batch['lanes']):
                raise PreflightError('runner_leases_missing', '活动批次未持有全部实例锁',
                                     '请等待初始化或收尾；若持续出现，安全暂停并核查调度器。', source=source)
    except (OSError, sqlite3.Error, json.JSONDecodeError, KeyError, TypeError) as error:
        raise _read_error(error, source) from None
