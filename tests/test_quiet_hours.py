from datetime import datetime
from unittest.mock import Mock

import pytest

from ournotes.runtime.model import PauseRun
from ournotes.runtime.quiet_hours import admission_delay
from ournotes.runtime.runner import Runner


@pytest.mark.parametrize('timestamp,seconds', [
    ('2026-10-10T22:49:59+08:00', 0),
    ('2026-10-10T22:50:00+08:00', 900),
    ('2026-10-10T23:04:59+08:00', 1),
    ('2026-10-10T23:05:00+08:00', 0),
    ('2026-10-10T14:50:00+00:00', 900),
    ('2026-10-11T00:00:00+08:00', 0),
])
def test_beijing_daily_boundary(timestamp, seconds):
    assert admission_delay(datetime.fromisoformat(timestamp)) == seconds


def test_wait_allows_existing_account_and_resumes_new_work(monkeypatch):
    r = Runner.__new__(Runner)
    r.account = {'id': 'active'}
    clock = Mock(return_value=900)
    monkeypatch.setattr('ournotes.runtime.quiet_hours.admission_delay', clock)
    r.wait_for_admission()
    clock.assert_not_called()
    r.account = None
    r.wait_for_uploads = Mock()
    r.run = {'id': 'run'}
    r.db = Mock()
    r.db.get_run.return_value = {}
    r.db.batch_quota_exhausted.return_value = False
    r.check_pause = Mock()
    r.publish = Mock()
    r.stop = Mock()
    r.stop.wait.side_effect = lambda _: clock.configure_mock(return_value=0)
    r.wait_for_admission()
    r.stop.wait.assert_called_once_with(1)
    assert r.publish.call_count == 2
    clock.return_value = 900
    r.check_pause.side_effect = PauseRun('用户暂停')
    with pytest.raises(PauseRun, match='用户暂停'):
        r.wait_for_admission()
