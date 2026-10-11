"""One in-memory development task; durable send boundaries never contain secrets."""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import threading
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path

from ournotes.accounts.usage import cloud_status as usage_status
from ournotes.accounts.used import set_used_item
from ournotes.common.devices import rebind_lease
from ournotes.common.files import atomic_json
from ournotes.common.secrets import SecretStore
from ournotes.integrations.cloud_client import CloudClient, CloudError
from ournotes.rebind.preflight import PreflightError, idle_batch


class Stopped(Exception):
    pass


def validate_email(value):
    value = value.strip()
    if len(value) > 254 or not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}', value):
        raise ValueError('请输入完整有效的目标邮箱。')
    if value.split('@')[1].lower() not in {'gmail.com', 'hotmail.com', 'msn.com', 'outlook.com',
                                          'yahoo.com', 'icloud.com', 'qq.com', '163.com', '126.com'}:
        raise ValueError('游戏目前只支持 Gmail、Hotmail、MSN、Outlook、Yahoo、iCloud、QQ、163、126 邮箱。')
    return value


class Service:
    def __init__(self, root, *, client=None, driver=None, lease=None, idle_check=idle_batch, observer=None,
                 shutdown=None):
        self.root = Path(root)
        self.client_factory = client or (lambda: CloudClient.from_root(self.root))
        self.driver_factory = driver
        self.lease = lease or (lambda instance: rebind_lease(self.root, instance))
        self.idle_check, self.observer = idle_check, observer
        if shutdown is None:
            from ournotes.integrations.mumu import shutdown_instances
            shutdown = shutdown_instances
        self.shutdown = shutdown
        self.mutex = threading.RLock()
        self.stop_event, self.confirm_event, self.email_event = threading.Event(), threading.Event(), threading.Event()
        self.thread = None
        self.task = None
        self.confirmations = set()
        self.email_commands = set()
        self.original_email = None
        self.finished_requests = set()
        self.password = None
        self.journal = self.root / 'state/email-rebind-task.json'
        self.outcomes = self.root / 'state/email-rebind-outcomes.json'
        self.closing = False
        if self.journal.exists():
            saved = json.loads(self.journal.read_text('utf-8'))
            if saved.get('state') == 'error' and saved.get('error', {}).get('phase') == 'preflight':
                self.task = {**saved, 'message': saved['error']['message']}
            else:
                self.task = {**saved, 'state': 'interrupted', 'message': '服务曾中断；不会重放发信或换绑。核对游戏后停止并清除任务。'}

    def status(self):
        with self.mutex:
            return {'task': dict(self.task) if self.task else None,
                    'running': bool(self.thread and self.thread.is_alive())}

    def save(self):
        atomic_json(self.journal, {k: self.task[k] for k in ('id', 'index', 'instance', 'state', 'request_id', 'error', 'email_revision')
                                  if k in self.task})

    def stage(self, state, message):
        with self.mutex:
            self.check()
            self.task.update(state=state, message=message)
            self.save()

    def check(self):
        if self.stop_event.is_set():
            raise Stopped()

    def wait(self, seconds):
        if self.stop_event.wait(seconds):
            raise Stopped()

    def start(self, index, instance, email, request_id):
        email = validate_email(email)
        with self.mutex:
            if self.closing:
                raise ValueError('控制台正在关闭。')
            if request_id in self.finished_requests:
                raise ValueError('该开始请求对应的任务已结束，请使用新的请求。')
            if self.task:
                if self.task.get('request_id') == request_id:
                    return self.status()
                raise ValueError('请先停止并清除当前换绑任务。')
            self.stop_event.clear()
            self.confirm_event.clear()
            self.email_event.clear()
            self.confirmations.clear()
            self.email_commands.clear()
            self.password = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789') for _ in range(16))
            self.task = {'id': uuid.uuid4().hex, 'request_id': request_id, 'index': index,
                         'instance': instance, 'email': email, 'state': 'starting', 'message': '检查账号与实例…'}
            self.save()
            self.thread = threading.Thread(target=self.run, args=(dict(self.task),), daemon=True, name='email-rebind')
            self.thread.start()
            return self.status()

    def secret(self, task_id):
        with self.mutex:
            self.require(task_id)
            if not self.password:
                raise ValueError('密码已清除或服务已重启。')
            return {'password': self.password}

    def require(self, task_id):
        if not self.task or self.task['id'] != task_id:
            raise ValueError('换绑任务已变化，请刷新页面。')

    def confirmed(self, task_id, command_id):
        with self.mutex:
            self.require(task_id)
            if command_id in self.confirmations:
                return self.status()
            if self.task['state'] != 'waiting_b':
                raise ValueError('当前不在等待目标邮箱确认的阶段。')
            self.confirmations.add(command_id)
            self.task.update(state='checking_b', message='正在核验邮件…')
            self.confirm_event.set()
            return self.status()

    def wait_confirmation(self, message='请打开目标邮箱的认证链接，再点击“邮件已确认”。'):
        with self.mutex:
            self.confirm_event.clear()
            self.stage('waiting_b', message)
        while not self.confirm_event.wait(.2):
            self.check()
        self.check()

    def wait_target_email(self):
        with self.mutex:
            self.email_event.clear()
            self.task['email_revision'] = self.task.get('email_revision', 0) + 1
            self.stage('waiting_email', '邮箱已注册，请更换邮箱')
        while not self.email_event.wait(.2):
            self.check()
        with self.mutex:
            self.check()
            return self.task['email'], self.task['email_revision']

    def retry_target(self, task_id, command_id, email, email_revision):
        email = validate_email(email)
        with self.mutex:
            self.require(task_id)
            self.check()
            if command_id in self.email_commands:
                return self.status()
            if (self.task['state'] != 'waiting_email' or self.task.get('email_revision') != email_revision
                    or not self.thread or not self.thread.is_alive()):
                raise ValueError('当前不在等待更换邮箱的阶段，或页面已过期，请刷新。')
            if email.lower() in {self.task['email'].lower(), (self.original_email or '').lower()}:
                raise ValueError('请更换为其他目标邮箱，不能使用刚被拒绝的邮箱或原邮箱。')
            self.email_commands.add(command_id)
            self.task.update(email=email, state='changing_email', message='正在更换目标邮箱并继续当前任务…')
            self.save()
            self.email_event.set()
            return self.status()

    def stop(self, task_id):
        with self.mutex:
            self.require(task_id)
            self.stop_event.set()
            self.confirm_event.set()
            self.email_event.set()
            if self.thread and self.thread.is_alive():
                if self.task['state'] in ('success', 'ending'):
                    self.task.update(state='ending', message='换绑已完成，正在收尾并清除任务。')
                else:
                    self.task.update(state='stopping', message='停止中；已发送邮件或已生效换绑无法撤销。')
            else:
                self.clear()
            return self.status()

    def clear(self):
        if self.task:
            self.finished_requests.add(self.task['request_id'])
        self.confirmations.clear()
        self.email_commands.clear()
        self.original_email = None
        self.password = None
        self.task = None
        self.journal.unlink(missing_ok=True)

    def outcome(self, index, account_id, state=None):
        # Account business evidence is not disposable UI task data. No target/password.
        with self.mutex, SecretStore(self.root / 'state').lock('account-used', wait=True):
            records = json.loads(self.outcomes.read_text('utf-8')) if self.outcomes.exists() else {}
            if state is None:
                records.pop(str(index), None)
            else:
                records[str(index)] = {'account_id': account_id, 'state': state}
            atomic_json(self.outcomes, records)

    def check_outcome(self, index, item):
        current = usage_status(item)
        if item['result']['index'] != index:
            raise ValueError('换绑核查记录的账号编号不一致。')
        records = json.loads(self.outcomes.read_text('utf-8')) if self.outcomes.exists() else {}
        if str(index) in records:
            if records[str(index)]['account_id'] != item['result']['account_id']:
                raise ValueError('换绑核查记录的账号身份不一致。')
            if current == 'used':
                self.outcome(index, item['result']['account_id'])
            else:
                raise PreflightError('account_result_uncertain', '此账号存在换绑结果或已使用同步待核查记录',
                                     '请核对游戏结果与云端使用状态；清除任务不会解除保护，禁止重复换绑。')

    def run(self, task):
        driver = None
        entered_flow = False
        rebind_completed = False
        leases = ExitStack()
        try:
            leases.enter_context(self.lease(task['instance']))
            with self.mutex:
                self.check()
            self.idle_check(self.root, task['instance'], task['index'])
            self.check()
            client = self.client_factory()
            try:
                capability = client.request('GET', '/rebind/capability')
                if capability.get('enabled') is not True or capability.get('protocol') != 2:
                    raise PreflightError('cloud_protocol_incompatible', '云端换绑协议不兼容',
                                         '请更新云端换绑服务并核对开发节点权限。', source='cloud')
            except CloudError as error:
                if error.status in (403, 404):
                    raise PreflightError('cloud_rebind_disabled', '云端尚未启用邮箱换绑或开发节点未获授权',
                                         '请部署对应服务并授权开发节点。', source='cloud') from None
                raise
            item = client.get_result_by_index(task['index'])
            self.idle_check(self.root, task['instance'], task['index'], item['result']['account_id'])
            self.check_outcome(task['index'], item)
            if item['result']['index'] != task['index']:
                raise PreflightError('cloud_account_mismatch', '云端成果编号与查询编号不一致',
                                     '请核对云端账号身份，禁止换绑。', source='cloud')
            if 'usage_status' not in item:
                raise PreflightError('cloud_protocol_incompatible', '云端账号状态协议不兼容',
                                     '请更新云端服务。', source='cloud')
            if usage_status(item) == 'used':
                raise PreflightError('account_used', '账号已使用，禁止换绑',
                                     '请清除任务并选择其他未使用账号。', source='cloud')
            if item['result']['email'].lower() == task['email'].lower():
                raise PreflightError('email_unchanged', '目标邮箱与原邮箱相同',
                                     '请清除任务并填写其他目标邮箱。', source='account-check')
            self.original_email = item['result']['email']
            if self.driver_factory is None:
                from ournotes.rebind.flow import Flow
                factory = Flow
            else:
                factory = self.driver_factory
            entered_flow = True
            driver = factory(self, client, task, item, self.password)
            driver.run()
            rebind_completed = True
            # Once the game proves success, stopping cannot discard the usage update.
            with self.mutex:
                self.task.update(state='marking_used', message='换绑已成功，正在标记已使用…')
                self.save()
            self.outcome(task['index'], item['result']['account_id'], 'used_pending')
            current = client.get_result_by_index(task['index'])
            if current['result']['account_id'] != item['result']['account_id']:
                raise ValueError('换绑已成功，但云端身份冲突；请核查使用状态。')
            for attempt in range(3):
                if usage_status(current) == 'used':
                    break
                try:
                    set_used_item(self.root, client, current)
                    break
                except CloudError as error:
                    if error.status != 409 or attempt == 2:
                        raise
                    current = client.get_result_by_index(task['index'])
                    if current['result']['account_id'] != item['result']['account_id']:
                        raise ValueError('换绑成功后的账号身份冲突。') from None
            updated = client.get_result_by_index(task['index'])
            if usage_status(updated) != 'used':
                raise ValueError('云端使用状态冲突，需核查。')
            self.outcome(task['index'], item['result']['account_id'])
            if self.observer:
                self.observer([updated])
            with self.mutex:
                self.task.update(state='success', message='邮箱换绑成功，账号已标记已使用。结束前请复制密码。')
                self.save()
        except Stopped:
            pass
        except Exception as error:  # noqa: BLE001 -- isolate device failures without exposing secrets
            if driver and hasattr(driver, 'record_failure'):
                try:
                    driver.record_failure(error)
                except (OSError, ValueError):
                    pass  # Evidence storage failure must not prevent ticket/device cleanup.
            with self.mutex:
                if self.task:
                    # Never expose SDK/HTTP exception details, which may include credentials.
                    from ournotes.proxy.rotation import ProxyQuotaError
                    message = str(error) if isinstance(error, (PreflightError, ProxyQuotaError)) or type(error) is ValueError else '操作未完成，请核对游戏与云端状态；未自动重发。'
                    if entered_flow and isinstance(error, CloudError):
                        expired_authorization = error.code == 'gmail_authorization_required'
                        detail = {'phase': 'flow', 'code': 'gmail_authorization_required' if expired_authorization else 'cloud_request_failed',
                                  'source': 'cloud', 'exception_type': 'CloudError', 'http_status': error.status,
                                  'occurred_at': datetime.now(UTC).isoformat(),
                                  'hint': '请在“接入与检查”重新完成 Google 浏览器授权，并安全迁移到服务器。'
                                  if expired_authorization else '请检查云端连接和诊断中的 HTTP 状态码；保留现场核查，未重发邮件。'}
                        if expired_authorization:
                            message = '云端 Gmail 授权已失效，换绑已停止；请重新授权并迁移到服务器。未自动重发邮件。'
                        detail['message'] = message
                        self.task['error'] = detail
                    if not entered_flow:
                        if isinstance(error, PreflightError):
                            detail = dict(error.info)
                        elif isinstance(error, CloudError):
                            detail = {'phase': 'preflight', 'code': 'cloud_preflight_failed', 'source': 'cloud',
                                      'message': '云端账号或换绑权限查询失败，未启动换绑。',
                                      'hint': '请在“接入与检查”核对云端连接、开发节点权限及账号是否已上传。',
                                      'exception_type': type(error).__name__}
                            message = detail['message']
                        else:
                            detail = {'phase': 'preflight', 'code': 'preflight_rejected', 'source': 'account-check',
                                      'message': '账号、实例或云端启动检查未通过，未启动换绑。',
                                      'hint': '请按提示核对账号及云端状态，然后清除本次未启动任务。',
                                      'exception_type': type(error).__name__}
                            if type(error) is not ValueError:
                                message = detail['message']
                        detail['occurred_at'] = datetime.now(UTC).isoformat()
                        # Keep only allowlisted metadata, never the raw exception, email or password.
                        folder = self.root / 'results/maintenance/diagnostics/email-rebind' / task['id']
                        try:
                            atomic_json(folder / 'failure.json', {'task_id': task['id'], 'index': task['index'],
                                                                 'instance': task['instance'], 'error': detail})
                            detail['diagnostic_saved'] = True
                        except OSError:
                            detail['diagnostic_saved'] = False
                        self.task['error'] = detail
                    if self.task['state'] == 'marking_used':
                        message = '换绑已成功，已使用状态待云端核对；禁止重新执行换绑。'
                    self.task.update(state='error', message=message)
                    self.save()
        finally:
            try:
                if driver:
                    driver.close()
            finally:
                try:
                    if rebind_completed:
                        try:
                            result = self.shutdown([task['instance']])
                        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                            result = {'status': 'failed', 'instances': [task['instance']]}
                        with self.mutex:
                            if self.task:
                                self.task['shutdown'] = result
                                self.save()
                finally:
                    try:
                        leases.close()  # Release only after SDK, ticket and instance cleanup.
                    finally:
                        with self.mutex:
                            if self.stop_event.is_set():
                                self.clear()

    def close(self):
        self.closing = True
        with self.mutex:
            if self.task:
                self.stop(self.task['id'])
        if self.thread:
            self.thread.join(timeout=25)
