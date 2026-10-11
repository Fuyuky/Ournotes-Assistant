"""Cloud account management projection; never reads or recreates local archives."""
from __future__ import annotations

from ournotes.accounts.usage import cloud_status
from ournotes.integrations.cloud_client import CloudClient


class AccountQuery:
    def __init__(self, root, client_factory=None):
        self.root = root
        self.client_factory = client_factory or (lambda: CloudClient.from_root(root))

    def account(self, index):
        item = self.client_factory().get_result_by_index(index)
        result = item['result']
        if type(result.get('index')) is not int or result['index'] != index:
            raise ValueError('云端返回的账号编号不一致，请重新查询。')
        owned = {}
        for entry in result.get('cards', []):
            card, count = entry.get('card', {}), entry.get('count')
            if card.get('rarity') != 'SSR':
                continue
            kind, identity = card.get('kind'), card.get('id')
            if kind not in ('member', 'support') or type(identity) is not int or identity < 0:
                raise ValueError('云端卡牌身份无效，请核查账号成果。')
            if type(count) is not int or count <= 0:
                raise ValueError('云端卡牌数量无效，请核查账号成果。')
            key = f'{kind}:{identity}'
            if key not in owned:
                owned[key] = {**{field: card.get(field, '') for field in ('name', 'title')},
                              'key': key, 'id': identity, 'kind': kind, 'rarity': 'SSR',
                              'image': f'/api/launcher/account-art/{kind}/{identity}', 'count': 0}
            owned[key]['count'] += count
        current = cloud_status(item)
        snapshot = result.get('preset_snapshot') or {}
        historical = snapshot.get('name') or result.get('preset') or result.get('preset_name')
        plan = snapshot.get('entries') or result.get('plan')
        planned_pulls = (sum(entry['pulls'] for entry in plan)
                         if isinstance(plan, list) and plan and all(
                             isinstance(entry, dict) and type(entry.get('pulls')) is int
                             and entry['pulls'] > 0 for entry in plan) else snapshot.get('total_pulls', result.get('planned_pull_count')))
        return {'index': index, 'usage_status': current, 'used': current in ('booked', 'used'),
                'usage_pending': False, 'version': item['version'], 'pull_count': result.get('pull_count'),
                'ssr_total': sum(card['count'] for card in owned.values()), 'cards': list(owned.values()),
                'historical_preset': {'name': historical, 'planned_pulls': planned_pulls,
                                      'status': 'recorded' if historical else 'not_recorded'},
                'pool_keys': result.get('pool_keys', []), 'draw_labels': result.get('draw_labels', []),
                'owner_username': item.get('owner_username'), 'activity': item.get('activity'),
                'email': result.get('email', '')}

    def email(self, index):
        result = self.client_factory().get_result_by_index(index)['result']
        if result.get('index') != index:
            raise ValueError('云端返回的账号编号不一致，请重新查询。')
        if not result.get('email'):
            raise ValueError('云端尚未保存此账号的邮箱。')
        return {'index': index, 'email': result['email']}
