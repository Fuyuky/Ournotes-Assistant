"""Account business state and its deliberately different display/sale projections."""

STATUSES = ('available', 'booked', 'used')
LABELS = {'available': '未使用', 'booked': '已拍下', 'used': '已使用'}


def status(value):
    result = value.get('result', value)
    current = value.get('usage_status', result.get('usage_status'))
    if current is None:
        if 'usage_status' in value or 'usage_status' in result:
            raise ValueError('invalid_usage_status')
        used = result.get('used', False)
        if type(used) is not bool:
            raise ValueError('invalid_used_flag')
        return 'used' if used else 'available'
    if current not in STATUSES:
        raise ValueError('invalid_usage_status')
    if 'used' in result and (type(result['used']) is not bool or result['used'] != (current != 'available')):
        raise ValueError('inconsistent_usage_status')
    return current


def cloud_status(value):
    """A folded legacy response cannot prove a permanent state transition."""
    if value.get('usage_status') not in STATUSES:
        raise ValueError('云端账号状态协议不兼容，请升级云端服务。')
    return status(value)


def sale_blocked(value):
    """Booking, including its pending changes, never changes product eligibility."""
    pending = value.get('used_pending')
    return status(value) == 'used' or bool(value.get('rebind_used_pending')) or bool(pending and (
        pending.get('legacy_protection') or
        pending.get('target_status') == 'used' or
        ('target_status' not in pending and (value.get('used') or pending.get('previous')))))
