import json

from ournotes.reports.performance import lane_report


def test_normal_batch_writes_only_small_summary(tmp_path):
    from ournotes.reports.performance import build
    batch = {'id': 'b' * 32, 'lanes': [{'run_id': 'r', 'instance': 1, 'preset': 'test'}]}
    build(batch, tmp_path)
    folder = tmp_path / 'results/batches' / batch['id']
    assert (folder / 'timing-summary.json').exists()
    assert not (folder / 'performance.json.gz').exists()
    assert not (folder / '完整分步骤用时.md').exists()


def test_resumed_timing_excludes_offline_gap_and_preserves_every_session(tmp_path):
    folder = tmp_path/'state/runs/test'
    folder.mkdir(parents=True)
    events = [
        {'event': 'run_started', 'time': 10},
        {'event': 'action_intent', 'time': 12, 'action_id': 'a', 'label': 'first'},
        {'event': 'action_finished', 'time': 13, 'action_id': 'a', 'label': 'first'},
        {'event': 'run_finished', 'time': 15},
        {'event': 'run_started', 'time': 100},
        {'event': 'action_intent', 'time': 104, 'action_id': 'b', 'label': 'second'},
        {'event': 'action_finished', 'time': 105, 'action_id': 'b', 'label': 'second'},
        {'event': 'run_finished', 'time': 110},
    ]
    metrics = [{'operation': 'startup_emulator', 'started_at': t, 'seconds': 1} for t in [10,100]]
    for name, rows in [('events', events), ('timings', metrics)]:
        (folder/(name+'.jsonl')).write_text('\n'.join(json.dumps(x) for x in rows), encoding='utf8')
    report = lane_report(tmp_path, {'run_id': 'test'})
    assert report['wall_seconds'] == 15 and report['paused_seconds'] == 85
    assert report['sessions'] == 2
    assert all(step['seconds'] >= 0 for step in report['steps'])
    assert sum(step['seconds'] for step in report['steps']) == 15
    from ournotes.runtime.storage import compress_log
    for name in ('events.jsonl', 'timings.jsonl'):
        compress_log(folder / name)
    assert lane_report(tmp_path, {'run_id': 'test'}) == report


def test_compressed_report_is_atomic_and_round_trips(tmp_path, monkeypatch):
    import gzip

    import pytest

    from ournotes.common.files import atomic_json_gzip
    path = tmp_path/'performance.json.gz'
    value = {'lanes': [{'steps': [{'label': '观察', 'seconds': 1.25}] * 1000}]}
    atomic_json_gzip(path, value)
    with gzip.open(path, 'rt', encoding='utf8') as source:
        assert json.load(source) == value
    before = path.read_bytes()
    monkeypatch.setattr('ournotes.common.files.replace_file', lambda *a: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError):
        atomic_json_gzip(path, {'replacement': True})
    assert path.read_bytes() == before
    assert not list(tmp_path.glob('*.tmp'))
