"""Online, versioned usage markers. The cloud owns this shared value."""
import hashlib
import json
import uuid
from pathlib import Path

from ournotes.accounts.usage import LABELS, STATUSES, cloud_status, status
from ournotes.common.files import atomic_json
from ournotes.common.secrets import SecretStore
from ournotes.integrations.cloud_client import CloudClient, CloudError


def find_result(client, index, root=None):
    if type(index) is not int or not 0 <= index <= 2**53 - 1:
        raise ValueError('请填写有效账号编号。')
    try:
        item = client.get_result_by_index(index)
    except CloudError as error:
        if error.status == 404:
            raise ValueError('云端没有此账号的完整成功成果。') from None
        raise
    if item['result']['index'] != index:
        raise ValueError('云端成果编号不一致，未修改使用状态。')
    return item


def _token(item):
    return hashlib.sha256(f"{item['result']['account_id']}:{item['version']}".encode()).hexdigest()



def preview(root, index):
    item = find_result(CloudClient.from_root(root), index, root)
    current = status(item)
    return {'index': index, 'token': _token(item), 'used': current != 'available',
            'usage_status': current, 'usage_protocol': 2 if 'usage_status' in item else 1,
            'version': item['version'], 'file_count': 0, 'bytes': 0}


def _journal(root):
    path = Path(root) / 'state/account-usage-pending.json'
    return path, json.loads(path.read_text('utf-8')) if path.exists() else {}


def _pending(root, item, target, request_id):
    kind = 'used_pending' if target == 'used' else 'booking_pending'
    result = item['result']
    pending = {'request_id': request_id, 'version': item['version'],
               'previous_status': status(item), 'target_status': target,
               'index': result['index'], 'account_id': result['account_id'], 'kind': kind}
    with SecretStore(Path(root) / 'state').lock('account-used', wait=True):
        path, ledger = _journal(root)
        if any(p['account_id'] == result['account_id'] and p['kind'] == kind for p in ledger.values()):
            raise ValueError('上次状态操作尚未确认，请先核对云端。')
        ledger[request_id] = pending
        atomic_json(path, ledger)
    return pending


def _finish_pending(root, pending, *, rollback=False, updated=None):
    if updated is not None and (updated['result']['account_id'] != pending['account_id'] or
                                updated['result']['index'] != pending['index']):
        raise ValueError('状态回执的账号身份不一致。')
    if updated is not None:
        cloud_status(updated)
    with SecretStore(Path(root) / 'state').lock('account-used', wait=True):
        path, ledger = _journal(root)
        ledger.pop(pending['request_id'], None)
        atomic_json(path, ledger)


def require_protocol(client):
    try:
        supported = client.usage_capability().get('usage_protocol') == 2
    except CloudError as error:
        if error.status != 404:
            raise
        supported = False
    if not supported:
        raise ValueError('请先升级云端账号状态服务；当前服务不支持三态。')


def set_usage(root, index, token, target_status):
    client = CloudClient.from_root(root)
    require_protocol(client)
    item = find_result(client, index, root)
    if _token(item) != token:
        raise ValueError('云端成果版本已变化，请刷新后重试。')
    return set_usage_item(root, client, item, target_status)


def set_usage_item(root, client, item, target_status):
    if target_status not in STATUSES or 'usage_status' not in item:
        raise ValueError('云端账号状态协议不兼容。')
    current = status(item)
    if current == 'used' and target_status != 'used':
        raise ValueError('已使用状态不可撤销。')
    if current == target_status:
        return {'index': item['result']['index'], 'used': current != 'available',
                'usage_status': current, 'version': item['version'],
                'message': f"账号 {item['result']['index']} 已是{LABELS[current]}。"}
    request_id = uuid.uuid4().hex
    pending = _pending(root, item, target_status, request_id)
    try:
        updated = client.set_usage(item['result']['account_id'], target_status, item['version'], request_id)
    except CloudError as error:
        if error.status is not None and 400 <= error.status < 500:
            _finish_pending(root, pending, rollback=True)
            raise
        try:
            # An absent receipt alone is not proof that an in-flight write failed.
            try:
                client.usage_receipt(item['result']['account_id'], request_id)
            except CloudError as receipt_error:
                if receipt_error.status != 404:
                    raise
            updated = find_result(client, item['result']['index'], root)
        except CloudError:
            raise CloudError('usage_unconfirmed', '状态操作尚未确认，保留记录等待云端核对。') from None
        if updated['version'] <= item['version']:
            raise CloudError('usage_unconfirmed', '状态操作尚未确认，请勿重复提交。') from None
        if cloud_status(updated) != target_status:
            _finish_pending(root, pending, updated=updated)
            raise CloudError('usage_conflict', '云端状态已变化，已读取最新状态；请重新确认。', 409) from None
        updated['publication'] = {'pending': True}
    if cloud_status(updated) != target_status:
        raise CloudError('usage_unconfirmed', '云端未确认目标状态，保留待核查记录。')
    _finish_pending(root, pending, updated=updated)
    publication = updated.get('publication', {})
    return {'index': item['result']['index'], 'used': target_status != 'available',
            'usage_status': target_status, 'version': updated['version'], 'publication': publication,
            'message': f"账号 {item['result']['index']} 已标记为{LABELS[target_status]}；" + (
                '网站更新待确认，将自动重试。' if publication.get('pending') else '网站使用状态已更新。')}


def set_used(root, index, token, *, used=True):
    if used is not True:
        raise ValueError('已使用状态不可撤销；请使用撤销已拍下。')
    return set_usage(root, index, token, 'used')


def set_used_item(root, client, item, *, used=True):
    if used is not True:
        raise ValueError('已使用状态不可撤销。')
    return set_usage_item(root, client, item, 'used')


def reconcile_pending(root, client, *, legacy_seen=None):
    """Read uncertain writes and finish proven rebinds; never replay game actions."""
    with SecretStore(Path(root) / 'state').lock('account-used', wait=True):
        _, ledger = _journal(root)
    for pending in ledger.values():
        latest = find_result(client, pending['index'], root)
        cloud_status(latest)
        if latest['result']['account_id'] != pending['account_id']:
            raise ValueError('待确认状态的账号身份已变化。')
        try:
            client.usage_receipt(pending['account_id'], pending['request_id'])
        except CloudError as error:
            if error.status != 404:
                raise
            if latest['version'] <= pending['version']:
                continue
        latest = find_result(client, pending['index'], root)
        _finish_pending(root, pending, updated=latest)
    outcomes_path = Path(root) / 'state/email-rebind-outcomes.json'
    if not outcomes_path.exists():
        return
    records = json.loads(outcomes_path.read_text('utf-8'))
    for number, record in records.items():
        if record['state'] != 'used_pending':
            continue
        item = find_result(client, int(number), root)
        if item['result']['account_id'] != record['account_id']:
            raise ValueError('换绑待确认记录的账号身份不一致。')
        with SecretStore(Path(root) / 'state').lock('account-used', wait=True):
            _, remaining = _journal(root)
        if any(p['account_id'] == record['account_id'] and p['kind'] == 'used_pending' for p in remaining.values()):
            continue
        if cloud_status(item) != 'used':
            try:
                set_usage_item(root, client, item, 'used')
            except CloudError as error:
                if error.status == 409:
                    continue
                raise
            item = find_result(client, int(number), root)
        if cloud_status(item) == 'used':
            with SecretStore(Path(root) / 'state').lock('account-used', wait=True):
                current = json.loads(outcomes_path.read_text('utf-8'))
                if current.get(number) == record:
                    current.pop(number)
                    atomic_json(outcomes_path, current)
