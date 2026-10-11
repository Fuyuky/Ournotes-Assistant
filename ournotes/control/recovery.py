"""Shared recovery guards; no device, credential or runtime-store initialization."""
from __future__ import annotations

import hashlib
import json
import time


def lane_recovery(batch, run, account, unresolved, *, now=None):
    context = (account or {}).get('context', {})
    request = run.get('recovery_request', {})
    phase = (account or {}).get('phase')
    otp = context.get('otp', {})
    now = time.time() if now is None else now
    reason = ''
    if (account or {}).get('evidence_pruned_at') or run.get('storage_retention', {}).get('pruned_at'):
        from ournotes.runtime.storage import EVIDENCE_PRUNED
        reason = EVIDENCE_PRUNED
    elif batch.get('status') in ('complete', 'cancelled'):
        reason = '批次已结束。'
    elif run.get('status') != 'paused':
        reason = '请等待该实例安全暂停。'
    elif request.get('status') in ('requested', 'starting'):
        reason = '已有恢复请求或重启结果未确认；请核对原回执。'
    elif run.get('memory_restart', {}).get('status') in ('restarting', 'needs_review'):
        reason = '内存维护重启结果未确认；请先核查实例。'
    elif run.get('recovery_hold'):
        reason = '该实例处于待核查状态，请先完成复核。'
    elif unresolved:
        reason = '存在已提交抽卡、待识别或待复核交易；请先核对原交易。'
    elif context.get('login_network_uncertain'):
        reason = '发码或登录网络错误的结果尚未确认；请先核对原票据，不能自动恢复或重启。'
    elif run.get('active_account') and (
        not account or account.get('id') != run['active_account']
        or account.get('run_id') != run['id'] or account.get('batch_id') != batch['id']
        or account.get('status') != 'running'
    ):
        reason = '活动账号归属或状态不一致，请先核查。'
    elif (phase == 'login' and context.get('otp_retry') and not context.get('login_submitted')
          and otp.get('expires_at', float('inf')) <= now):
        reason = '验证码票据已过期且一次重发额度已使用；请核查收码，不能再次发码。'
    restart_reason = reason
    if not restart_reason and (run.get('mail_send_limit_reached')
                               or '今日邮箱发送次数已达上限' in run.get('pause_reason', '')):
        restart_reason = '邮箱已达日限额；确认解除后使用手动重试，重启游戏不能解除限额。'
    if not restart_reason and phase == 'login' and (
        context.get('login_submitted') or context.get('send_intent') and not otp
    ):
        restart_reason = '登录或发码结果尚未确认；保留原界面和票据，不能重启游戏。'
    bound = {
        'batch_id': batch['id'], 'batch_status': batch['status'],
        'run': {k: run.get(k) for k in ('id', 'status', 'active_account', 'completed',
                'pause_reason', 'recovery_hold', 'mail_send_limit_reached', 'recovery_request',
                'memory_restart')},
        'account': {k: (account or {}).get(k) for k in ('id', 'run_id', 'batch_id', 'status', 'phase', 'alias_index')},
        'login_submitted': bool(context.get('login_submitted')),
        'login_network_uncertain': bool(context.get('login_network_uncertain')),
        'send_intent': bool(context.get('send_intent')), 'otp_present': bool(otp),
        'otp_expires_at': otp.get('expires_at'), 'otp_retry': bool(context.get('otp_retry')),
        'unresolved': sorted((d['id'], d['status']) for d in unresolved),
    }
    version = hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()
    return {'version': version, 'retry': {'enabled': not reason, 'reason': reason},
            'restart_game': {'enabled': not restart_reason, 'reason': restart_reason}}


def require_recovery(capability, mode, expected=None):
    if mode not in ('retry', 'restart_game'):
        raise ValueError('不支持的恢复方式。')
    if expected is not None and expected != capability['version']:
        raise ValueError('所选实例状态已变化，请刷新后重新操作。')
    if not capability[mode]['enabled']:
        raise ValueError(capability[mode]['reason'])


def runtime_recovery(db, batch, run):
    account = db.account(run['active_account']) if run.get('active_account') else None
    unresolved = [{'id': r[0], 'status': r[1]} for r in db.db.execute(
        "SELECT d.id,d.status FROM draws d JOIN accounts a ON a.id=d.account_id "
        "WHERE a.run_id=? AND NOT (a.status='cancelled' AND json_extract(a.data, '$.submission_abandoned') IS NOT NULL) AND d.status IN ('submitted','captured','needs_review')", (run['id'],))]
    return lane_recovery(batch, run, account, unresolved)
