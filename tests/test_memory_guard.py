from unittest.mock import Mock

import pytest

from ournotes.common.memory import GIB, CommitMemory
from ournotes.runtime.model import PauseRun
from ournotes.runtime.runner import Runner


@pytest.mark.parametrize('used,limit,physical,low', [
    (159, 164, 19, True),  # Free RAM does not imply free commit budget.
    (8, 32, 1, False),
    (12, 16, 4, False),
    (13, 16, 4, True),
    (33, 32, 4, True),
])
def test_commit_budget(used, limit, physical, low):
    memory = CommitMemory(used * GIB, limit * GIB, physical * GIB)
    assert memory.low is low
    assert memory.available >= 0


def test_low_commit_records_budget_and_pauses_without_device_input(monkeypatch):
    memory = CommitMemory(159 * GIB, 164 * GIB, 19 * GIB)
    monkeypatch.setattr('ournotes.common.memory.read_commit_memory', lambda: memory)
    runner = Runner()
    runner.log = Mock()
    runner.device = Mock()
    with pytest.raises(PauseRun, match='系统提交内存不足'):
        runner.check_memory()
    runner.log.event.assert_called_once_with('system_memory', **memory.report())
    assert not runner.device.mock_calls


def test_healthy_budget_logging_is_bounded(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr('ournotes.runtime.runner.time.monotonic', lambda: clock[0])
    runner = Runner()
    runner.log = Mock()
    for _ in range(100):
        runner.check_memory()
    assert runner.log.event.call_count == 1
    clock[0] += 60
    runner.check_memory()
    assert runner.log.event.call_count == 2


def test_memory_probe_failure_is_actionable(monkeypatch):
    monkeypatch.setattr('ournotes.common.memory.read_commit_memory', Mock(side_effect=OSError('probe')))
    with pytest.raises(PauseRun, match='无法读取系统内存'):
        Runner().check_memory()
