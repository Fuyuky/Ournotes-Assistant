import pytest

from ournotes.accounts.quality import describe, ssr_rosters


def test_current_pickups_count_card_identities_but_band_sorts_count_characters():
    counts = {'member:65': 3, 'member:66': 1, 'support:65': 2, 'support:66': 1,
              'member:99': 2, 'member:100': 1, 'member:61': 1, 'support:62': 3}
    value = describe(counts, {65: 16, 66: 17, 99: 16, 100: 21, 61: 11}, pool_keys=["bdon-gacha-12"])
    assert value['millsage_characters'] == 2
    assert value['dumb_characters'] == 1
    assert value['dream_characters'] == 1
    assert value['millsage_pickup_count'] == 4
    assert value['dream_pickup_count'] == 2
    assert 'millsage-4' in value['badges'] and not any(b.startswith('dream-') for b in value['badges'])


def test_rosters_follow_available_ssr_not_band_member_count_and_deduplicate_characters():
    mapping = {51: 1, 61: 15, 62: 12, 63: 13, 71: 15}
    catalog = {'cards': [{'id': i, 'kind': 'member', 'rarity': 'SR' if i == 63 else 'SSR'} for i in mapping]}
    rosters = ssr_rosters(catalog, mapping)
    assert rosters['dream'] == {12, 15}
    details = describe(dict.fromkeys(['member:61', 'member:71', 'member:62', 'support:62'], 1), mapping, rosters, ["bdon-gacha-10"])
    assert details['dream_characters'] == details['band_capacity']['dream'] == 2
    assert details['badges'] == ['dream-3']
    assert 'dream-full' not in details['badges']


def test_crychic_badge_and_character_identity():
    mapping = {1: 1, 11: 1, 4: 4, 5: 5, 7: 7, 10: 10}
    details = describe(dict.fromkeys(['member:1', 'member:11', 'member:4', 'member:5', 'member:7', 'member:10', 'support:2']), mapping)
    assert details['mygo_characters'] == 3 and details['mujica_characters'] == 2
    assert details['badges'] == ['mygo-3', 'crychic-full']
    with pytest.raises(ValueError):
        describe({'member:999': 1}, mapping)
