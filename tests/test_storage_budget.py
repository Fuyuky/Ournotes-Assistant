import json

from ournotes.common.bounded_log import LogBudget
from ournotes.runtime.runlog import RunLog
from ournotes.runtime.storage_budget import pause_reason, usage


def test_log_budget_is_shared_and_preserves_complete_lines(tmp_path):
    budget = LogBudget(100)
    for number in range(100):
        budget.write(tmp_path / ('a' if number % 2 else 'b'), json.dumps({'n': number}) + '\n')
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= 100
    for path in tmp_path.iterdir():
        assert all(json.loads(line)['n'] >= 0 for line in path.read_text().splitlines())
    budget.clear()
    assert not list(tmp_path.iterdir())


def test_debug_is_opt_in_and_summary_survives(tmp_path):
    normal = RunLog(tmp_path, 'a' * 32)
    with normal.timed('capture', phase='login'):
        pass
    normal.close()
    assert not (normal.folder / 'timings.jsonl').exists()
    assert json.loads((normal.folder / 'timing-summary.json').read_text())['login:capture']['calls'] == 1
    debug = RunLog(tmp_path, 'b' * 32, debug_mode=True)
    with debug.timed('capture', phase='login'):
        pass
    debug.close()
    assert (debug.folder / 'timings.jsonl').stat().st_size > 0


def test_budget_counts_evidence_but_not_success_archives(tmp_path, monkeypatch):
    import ournotes.runtime.storage_budget as guard
    folder = tmp_path / 'state/runs/a'
    folder.mkdir(parents=True)
    (folder / 'evidence.png').write_bytes(b'x' * 100)
    archive = tmp_path / 'results/archive/accounts'
    archive.mkdir(parents=True)
    (archive / 'result.json').write_bytes(b'x' * 1000)
    monkeypatch.setattr(guard, 'LIMIT', 100)
    assert usage(tmp_path, refresh=True) == 100
    assert pause_reason(tmp_path)


def test_repeated_incident_reuses_original_pixels(tmp_path):
    from PIL import Image
    log = RunLog(tmp_path, 'c' * 32)
    log.screenshot(Image.new('RGB', (10, 10)), 'error')
    first = log.incident('same error', {'phase': 'login'})
    second = log.incident('same error', {'phase': 'login'})
    assert first == second
    assert len(list(first.glob('*.png'))) == 1
    assert next(iter(json.loads((log.folder / 'incident-index.json').read_text()).values()))['repeats'] == 2
    different = log.incident('same error', {'phase': 'login', 'account_id': 'another'})
    assert different != first and list(different.glob('*.png'))
    log.close()


def test_crash_logs_collapse_and_share_current_budget(tmp_path):
    from ournotes.common.bounded_log import ConsoleLogBudget
    previous = ConsoleLogBudget(tmp_path)
    previous.limit = 100
    previous.write(tmp_path / 'state/webui/jobs/one.log', b'a' * 80)
    previous.write(tmp_path / 'state/webui/server.log', b'b' * 80)
    resumed = ConsoleLogBudget(tmp_path)
    resumed.limit = 100
    resumed.prepare()
    assert len(list((tmp_path / 'state/webui').rglob('*.log'))) == 1
    resumed.write(tmp_path / 'state/webui/jobs/two.log', b'c' * 80)
    assert sum(p.stat().st_size for p in (tmp_path / 'state/webui').rglob('*.log')) <= 100
    resumed.clear()
    assert not list((tmp_path / 'state/webui').rglob('*.log'))


def test_budget_pause_broadcasts_to_scheduler_before_draining(monkeypatch):
    import threading
    from types import SimpleNamespace

    from ournotes.runtime.runner import Runner
    monkeypatch.setattr('ournotes.runtime.storage_budget.pause_reason', lambda root: 'budget')
    reasons = []
    runner = SimpleNamespace(stop=threading.Event(), check_pause=reasons.append)
    Runner.check_storage(runner)
    assert runner.stop.is_set() and reasons == ['budget']


def test_interrupted_crash_log_cleanup_keeps_retry_journal(tmp_path, monkeypatch):
    from pathlib import Path

    import pytest

    from ournotes.common.bounded_log import ConsoleLogBudget
    old = ConsoleLogBudget(tmp_path)
    path = tmp_path / 'state/webui/jobs/old.log'
    old.write(path, b'original evidence\n')
    unlink = Path.unlink

    def interrupted(self, *args, **kwargs):
        if self == path:
            raise OSError('interrupted cleanup')
        return unlink(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', interrupted)
        with pytest.raises(OSError, match='interrupted'):
            ConsoleLogBudget(tmp_path).prepare()
    assert path.relative_to(tmp_path).as_posix() in json.loads(old.registry.read_text())
    recovered = ConsoleLogBudget(tmp_path)
    recovered.prepare()
    assert not path.exists()
    assert b'original evidence' in (tmp_path / 'state/webui/previous-crash.log').read_bytes()
