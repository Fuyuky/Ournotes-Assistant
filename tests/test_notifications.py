import pytest

from ournotes.control.notifications import Notifications, events


def snapshot(reason='异常 person@example.com 验证码=123456'):
    return {'lanes': [{'run_id': 'r1', 'instance': 1, 'status': 'paused', 'message': reason}]}


class Sender:
    def __init__(self):
        self.sent = []

    def send(self, title, body):
        self.sent.append((title, body))


def test_private_fields_never_enter_notifications():
    found = events(snapshot(), {'task': {'id': 't', 'state': 'waiting_b',
                                        'message': 'secret', 'email': 'person@example.com'}})
    assert len(found) == 2
    assert all('123456' not in body and '@' not in body and 'secret' not in body for _, _, body in found)


def test_persistent_dedup_and_recurrence(tmp_path):
    value, sender = snapshot(), Sender()
    service = Notifications(tmp_path, lambda: value)
    service.deliver(sender)
    service.deliver(sender)
    assert len(sender.sent) == 1
    service = Notifications(tmp_path, lambda: value)
    service.deliver(sender)
    assert len(sender.sent) == 1
    value['lanes'][0]['status'] = 'running'
    service.deliver(sender)
    value['lanes'][0]['status'] = 'paused'
    service.deliver(sender)
    assert len(sender.sent) == 2


def test_disable_and_rebind_phase_transitions(tmp_path):
    task = {'id': 'task', 'state': 'waiting_b'}
    service = Notifications(tmp_path, dict, lambda: {'task': task})
    sender = Sender()
    service.configure(False)
    service.deliver(sender)
    assert not sender.sent
    service.configure(True)
    service.deliver(sender)
    task['state'] = 'success'
    service.deliver(sender)
    assert len(sender.sent) == 2
    assert '邮件确认' in sender.sent[0][1] and '完成' in sender.sent[1][1]


def test_multiple_lanes_coalesce_and_update_reason():
    found = events(snapshot('游戏要求更新客户端'))
    assert '手动更新' in found[0][2]
    assert not events(snapshot('用户请求暂停。'))


@pytest.mark.parametrize('reason', [
    '用户暂停，已停止新输入；已采集结果将完成后台识别后退出。',
    '用户暂停，实例尚未启动。',
    '用户暂停；内存维护已关闭本实例，未重新启动。',
    '用户请求暂停。', '用户在启动前请求暂停。',
])
def test_user_pause_is_silent_even_with_stale_drain_message(reason):
    assert not events(snapshot(reason))
    value = snapshot('正在安全暂停：停止新交易，等待已提交抽卡结算保存。')
    value['lanes'][0]['pause_reason'] = reason
    assert not events(value)


def test_user_pause_does_not_hide_another_lane_or_recovery_hold():
    value = snapshot('用户暂停，实例尚未启动。')
    value['lanes'].append({'run_id': 'r2', 'instance': 2, 'status': 'paused', 'message': '识别失败'})
    assert len(events(value)) == 1
    value['lanes'][0]['recovery_hold'] = '重启结果未确认'
    assert len(events(value)) == 2
