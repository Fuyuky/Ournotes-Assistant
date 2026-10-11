"""Retire accounts after a failed submitted draw without resolving or replaying it."""
import time

SUBMISSION_ERRORS = (
    '已提交抽卡期间发生网络错误',
    '已提交抽卡180秒未取得结果证据',
    '已提交抽卡期间出现开屏演出',
    '已提交交易的结算页在返回标题后丢失',
)


def retire_submission_error(db, run, account, reason):
    if not any(reason.startswith(prefix) for prefix in SUBMISSION_ERRORS):
        raise ValueError('不是可自动放弃的提交结果错误。')
    with db.transaction():
        saved = db.account(account['id'])
        current = db.get_run(run['id'])
        if saved.get('submission_abandoned'):
            return saved['submission_abandoned']
        pending = [d['id'] for d in db.draws(account['id']) if d['status'] == 'submitted']
        if (not pending or saved['status'] != 'running' or current['active_account'] != account['id']
                or saved['run_id'] != run['id']):
            raise ValueError('账号或已提交交易状态已变化，未放弃。')
        audit = {'account_id': account['id'], 'index': saved['alias_index'],
                 'batch_id': saved.get('batch_id'), 'at': time.time(), 'reason': reason,
                 'transaction_ids': pending, 'previous_status': saved['status']}
        saved.update(status='cancelled', submission_abandoned=audit, batch_exclusion=audit)
        db.save_account(saved)
        current['active_account'] = None
        current['abandoned_game_reset'] = {'account_id': account['id'], 'status': 'pending'}
        current.setdefault('excluded_accounts', []).append(audit)
        current.pop('pause_reason', None)
        db.save_run(current)
        run.update(current)
    db.export(account['id'])  # Cancelled archive is excluded from upload and public results.
    return audit
