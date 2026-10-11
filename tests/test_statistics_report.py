import json

import pytest

from ournotes.reports.statistics import account_metrics, analyze, describe


def test_character_identity_is_not_card_identity_or_support_identity():
    account = {'alias_index': 12, 'alias': 'a@gmail.com', 'preset': '都抽', 'status': 'complete',
               'totals': {'member': {'SSR': 4}, 'support': {'SSR': 3}},
               'summary': [{'id': i, 'kind': kind, 'rarity': 'SSR', 'count': n}
                           for i, kind, n in [(1, 'member', 2), (2, 'member', 1),
                                              (3, 'member', 1), (1, 'support', 3)]]}
    row = account_metrics(account, {1: 7, 2: 7, 3: 8}, {7: 1, 8: 2})
    assert row['ssr_total'] == 7
    assert row['unique_cards'] == 4 and row['duplicates'] == 3
    assert row['unique_characters'] == 2
    assert row['mygo_characters'] == row['mujica_characters'] == 1
    summary = describe([row, dict(row, ssr_total=9)])
    assert summary['ssr_total']['histogram'] == {7: 1, 9: 1}
    assert summary['ssr_total']['mean'] == 8
    assert sum(summary['ssr_total']['histogram']) != 2  # Keys are quantities, values are accounts.


def test_missing_member_mapping_cannot_silently_lower_character_counts():
    account = {'alias_index': 1, 'alias': 'a@gmail.com', 'preset': '都抽', 'status': 'complete',
               'totals': {'member': {'SSR': 1}, 'support': {'SSR': 0}},
               'summary': [{'id': 999, 'kind': 'member', 'rarity': 'SSR', 'count': 1}]}
    with pytest.raises(KeyError):
        account_metrics(account, {}, {})


def test_report_accepts_new_presets_and_dream_roster(tmp_path):
    catalog = tmp_path / 'data/catalog/site'
    (catalog / 'raw').mkdir(parents=True)
    (catalog / 'raw/MasterMemberCard.json').write_text(json.dumps({'_allData': [{'_id': 61, '_characterID': 15}]}))
    (catalog / 'raw/MasterCharacter.json').write_text(json.dumps({'_allData': [{'_id': 15, '_bandID': 3}]}))
    card = {'id': 61, 'kind': 'member', 'rarity': 'SSR', 'name': '由乃', 'title': '测试'}
    (catalog / 'catalog.json').write_text(json.dumps({'cards': [card]}))
    from ournotes.runtime.pools import PRESETS
    accounts = [{'alias_index': i, 'alias': 'test@example.test', 'preset': p, 'status': 'complete', 'pull_count': 62,
                     'totals': {'member': {'SSR': 1}, 'support': {'SSR': 0}}, 'summary': [dict(card, count=1)]}
                for i, p in enumerate(PRESETS[3:])]
    result = analyze({'id': 'test', 'accounts': accounts}, tmp_path)
    assert result['sample_size'] == len(accounts)
    assert result['excluded'] == 0
    assert {row['preset'] for row in result['accounts']} == set(PRESETS[3:])
    assert all(r['dream_characters'] == 1 and r['pull_count'] == 62 for r in result['accounts'])
    assert all(result['cards'][0]['by_preset'][p]['copies'] == 1 for p in PRESETS[3:])
