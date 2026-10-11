import json
import random
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from ournotes.accounts.archive import cleanup_formal_runs, compact_account, write_archive
from ournotes.runtime.input_policy import InputPolicy
from ournotes.runtime.runner import Runner
from ournotes.runtime.store import RuntimeStore
from ournotes.vision.ocr import TextBox


def test_jitter_stays_in_target_and_screen_and_varies():
    policy = InputPolicy({'click_radius_px': 4, 'interval_seconds': [.12, .4]}, random.Random(4))
    obs = SimpleNamespace(image=Image.new('RGB', (1600, 900)), texts=[TextBox('x', 1, (98, 98, 103, 103))])
    points = {policy.point((100, 100), obs) for _ in range(200)}
    assert all(98 <= x < 103 and 98 <= y < 103 for x, y in points)
    obs.texts = []
    points = {policy.point((0, 899), obs) for _ in range(200)}
    assert len(points) > 1 and all(0 <= x <= 4 and 895 <= y <= 899 for x, y in points)
    delays = [policy.interval() for _ in range(100)]
    assert len(set(delays)) > 1 and all(.12 <= d <= .4 for d in delays)


def test_interval_defers_before_any_login_or_draw_intent(monkeypatch):
    runner = Runner()
    runner.next_action_at = 101
    monkeypatch.setattr('ournotes.runtime.runner.time.monotonic', lambda: 100)
    # No observation/account is required: the gate returns before inspecting or mutating them.
    runner.action = Mock()
    runner.step()
    runner.action.assert_not_called()


def test_home_recruit_click_is_inside_icon_not_label_edge():
    from ournotes.paths import REFERENCE
    from ournotes.ui.observer import Observer
    image = Image.open(REFERENCE/'game-onboarding/15-announcement-corner-test.png')
    # The manifest must explicitly separate the visual label from the safe hit target.
    observer = Observer(allow_fallback=False)
    spec = observer.manifest['elements']['a18-16']
    assert spec['click'][3] < spec['area'][1]
    box = spec['click']
    point = ((box[0]+box[2])//2, (box[1]+box[3])//2)
    policy = InputPolicy({'click_radius_px': 4, 'interval_seconds': [.12, .4]}, random.Random(1))
    obs = SimpleNamespace(image=image.resize((1600, 900)), texts=[TextBox('招募', 1, box)])
    assert all(690 <= x <= 756 and 745 <= y <= 800 for x, y in [policy.point(point, obs) for _ in range(100)])


def test_paid_shortage_is_a_foreground_interrupt():
    from ournotes.paths import REFERENCE
    from ournotes.ui.observer import Observer
    image = Image.open(REFERENCE/'runtime-regressions/paid-shortage.png')
    observation = Observer(allow_fallback=False).read(image, phase='draws')
    assert observation.scene == 'paid_shortage'
    assert observation.find('关闭', exact=True)
    assert not observation.find('OK', exact=True)


def sample():
    card = {'id': 1, 'kind': 'member', 'rarity': 'SSR', 'title': '标题', 'name': '名字'}
    support = dict(card, kind='support', rarity='SR')
    return {'id': 'sample-account', 'alias_index': 35, 'alias': 'a.b@gmail.com', 'status': 'complete', 'unknown_slots': 0,
            'totals': {'member': {'SSR': 3, 'SR': 0, 'R': 20}, 'support': {'SSR': 0, 'SR': 1, 'R': 19}},
            'summary': [dict(card, count=1, source_page='private'), dict(card, count=2),
                        dict(support, count=1), dict(card, id=2, rarity='R', count=20)]}


def test_archive_merges_duplicates_preserves_namespaces_and_omits_runtime():
    value = compact_account(sample())
    assert set(value) == {'schema_version', 'account_id', 'alias_index', 'alias', 'status', 'totals', 'cards', 'used', 'pool_keys'}
    assert value['used'] is False
    assert [e['count'] for e in value['cards']] == [3, 1]
    assert all(set(e['card']) == {'id', 'kind', 'rarity', 'title', 'name'} for e in value['cards'])
    assert all(e['card']['rarity'] != 'R' for e in value['cards'])


@pytest.mark.parametrize('change', [{'status': 'running'}, {'unknown_slots': 1}])
def test_archive_rejects_unfinished_or_falsely_complete(change):
    with pytest.raises(ValueError):
        compact_account(dict(sample(), **change))


def test_archive_atomic_failure_keeps_old_name_and_rename_removes_stale(tmp_path, monkeypatch):
    r = sample()
    old = write_archive(tmp_path, r)
    assert old.name == '35 3 SSR.json' and old.read_text(encoding='utf-8').count('\n') == 1
    r['totals']['member']['SSR'] = 4
    r['summary'][0]['count'] = 2
    import ournotes.common.files as module
    original = module.atomic_json
    def fail(*a, **kw):
        raise OSError('full')
    monkeypatch.setattr(module, 'atomic_json', fail)
    with pytest.raises(OSError):
        write_archive(tmp_path, r)
    assert old.exists()
    monkeypatch.setattr(module, 'atomic_json', original)
    new = write_archive(tmp_path, r)
    assert new.name == '35 4 SSR.json' and not old.exists()


def test_both_channels_keep_temporary_evidence_until_cloud_ack(tmp_path):
    for channel in ('formal', 'test'):
        root = tmp_path / channel
        db = RuntimeStore(root)
        run = db.create_run('都抽', channel, 1)
        a = db.reserve(run, [])
        a.update(status='complete', phase='finished')
        db.save_account(a)
        card = dict(sample()['summary'][0], key='member:1')
        db.save_draw({'id': 'draw', 'account_id': a['id'], 'ordinal': 0, 'status': 'complete',
                      'result': {'slots': [{'status': 'matched', 'card': card}]}})
        report = db.export(a['id'])
        assert report['totals']['member']['SSR'] == 1
        assert db.report(a['id'])['totals'] == report['totals']
        assert not list((root / 'results/archive/accounts').glob('*.json'))
        evidence = root / 'state/runs' / run['id']
        evidence.mkdir(parents=True)
        (evidence / 'run.log').write_text('evidence')
        assert not cleanup_formal_runs(db, [run['id']])
        run['status'] = 'complete'
        db.save_run(run)
        cleanup_formal_runs(db, [run['id']])
        full = root / 'results/accounts' / (a['id'] + '.json')
        ledger = db.draws(a['id'])
        assert len(ledger) == 1 and ledger[0]['status'] == 'complete' and 'result' in ledger[0]
        assert full.exists() and evidence.exists()
        db.db.execute('UPDATE accounts SET export_pending=1')
        db.repair_exports()
        assert db.db.execute('SELECT export_pending FROM accounts').fetchone()[0] == 0
        db.close()


def test_formal_archive_failure_does_not_prune_draws(tmp_path, monkeypatch):
    db = RuntimeStore(tmp_path)
    run = db.create_run('都抽', 'formal', 1)
    a = db.reserve(run, [])
    a.update(status='complete', phase='finished')
    db.save_account(a)
    db.save_draw({'id': 'draw', 'account_id': a['id'], 'ordinal': 0, 'status': 'complete'})
    def fail(*args):
        raise OSError('full')
    monkeypatch.setattr('ournotes.integrations.cloud_sync.CloudSync.enqueue', fail)
    with pytest.raises(OSError):
        db.export(a['id'])
    assert db.draws(a['id']) and not db.account(a['id']).get('archive')
    db.close()


def test_reexport_cannot_overwrite_cloud_usage_cache(tmp_path):
    from ournotes.common.files import atomic_json
    report = sample()
    path = write_archive(tmp_path, report)
    cached = json.loads(path.read_text('utf-8'))
    atomic_json(path, dict(cached, used=True, cloud_version=7))
    report['used'] = False
    write_archive(tmp_path, report)
    saved = json.loads(path.read_text('utf-8'))
    assert saved['used'] is True and saved['cloud_version'] == 7
