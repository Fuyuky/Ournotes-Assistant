"""Serve an isolated synthetic admin portal for browser QA; no mail or device work."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASSWORD = 'synthetic-browser-password-only'


def build_catalog(root):
    from PIL import Image, ImageDraw

    catalog = root / 'catalog'
    (catalog / 'images').mkdir(parents=True)
    cards = []
    for kind, color, size in (('member', '#abbad8', (180, 240)), ('support', '#aacfc5', (240, 135))):
        for identifier in (1, 2):
            filename = f'images/{kind}-{identifier}.png'
            image = Image.new('RGB', size, color)
            ImageDraw.Draw(image).rectangle((15, 15, size[0] - 15, size[1] - 15), outline='#ffffff', width=4)
            image.save(catalog / filename)
            cards.append({'key': f'{kind}:{identifier}', 'kind': kind, 'id': identifier, 'rarity': 'SSR',
                          'name': ('合成角色' if kind == 'member' else '合成支援') + str(identifier),
                          'title': '页面验证', 'image': filename})
    (catalog / 'catalog.json').write_text(json.dumps({'cards': cards}), 'utf-8')
    (catalog / 'raw').mkdir()
    (catalog / 'raw/MasterMemberCard.json').write_text(json.dumps({'_allData': [
        {'_id': 1, '_characterID': 1}, {'_id': 2, '_characterID': 6}]}), 'utf-8')
    pools = []
    for identifier, name in ((12, '合成梦限卡池'), (4, '合成常驻卡池')):
        pools.append({'key': f'fixture-pool-{identifier}', 'name': name, 'gacha_id': identifier,
                      'ticket_name': '合成招募券' if identifier == 12 else None,
                      'pickup_card_keys': ['member:1', 'support:1'] if identifier == 12 else [],
                      'unsupported_card_keys': [], 'products': [
                          {'itemType': 12, 'drawCount': n, 'price': n * 200} for n in (1, 10)]})
    (catalog / 'pools.json').write_text(json.dumps({'pools': pools}), 'utf-8')
    return catalog


def create_fixture(root, *, count=45, account_delay_ms=150):
    from fastapi.responses import JSONResponse

    from ournotes.cloud.admin_portal import password_record
    from ournotes.cloud.api import create_app
    from ournotes.cloud.presets import Presets
    from ournotes.cloud.store import Store
    from ournotes.runtime.presets import canonical_preset, expand_preset

    catalog = build_catalog(root)
    store = Store(root / 'cloud.sqlite3', 'syntheticbrowserfixture@gmail.com')
    store.create_node('fixture-node')
    store.high_water({'test_next_index': 8, 'formal_next_index': 1 << 20,
                      'evidence': 'isolated synthetic browser namespace'})
    chosen = [canonical_preset({'name': name, 'description': '仅用于本机合成页面验证', 'entries': [
        {'pool_key': 'fixture-pool-12', 'resource': 'stars', 'pulls': 43}]})
        for name in ('合成预设零', '合成预设一', '合成历史已删除预设')]
    presets = Presets(store, initial={'presets': chosen}, catalog=catalog)
    app = create_app(store, 'synthetic-browser-administrator-token-1234567890', catalog=catalog,
                     portal_credential=password_record(PASSWORD))
    app.state.administrators.create('Fuyuki', 'FixtureUser', PASSWORD)
    for ordinal in range(count):
        allocation = store.allocate('fixture-node', {'account_id': f'fixture-account-{ordinal}',
            'request_id': f'fixture-allocation-{ordinal}', 'channel': 'test'})
        member, support = 1 + ordinal % 3, ordinal % 2
        cards = [{'card': {'kind': 'member', 'id': 1, 'rarity': 'SSR', 'name': '合成角色1',
                           'title': '页面验证'}, 'count': member}]
        if support:
            cards.append({'card': {'kind': 'support', 'id': 1, 'rarity': 'SSR', 'name': '合成支援1',
                                   'title': '页面验证'}, 'count': support})
        result = {'schema_version': 1, 'account_id': allocation['account_id'], 'index': allocation['index'],
            'email': allocation['alias'], 'cards': cards, 'pull_count': 43, 'draw_labels': ['合成 43 抽'],
            'pool_keys': ['fixture-pool-12'], 'used': False,
            'totals': {'member': {'SSR': member, 'SR': 0, 'R': 43 - member - support},
                       'support': {'SSR': support, 'SR': 0, 'R': 0}}}
        if ordinal % 11:
            identifier = 2 if ordinal % 13 == 0 else ordinal % 2
            result['preset_snapshot'] = {'key': str(identifier), 'version': 1,
                                         'plan': expand_preset(chosen[identifier])}
        store.upload('fixture-node', allocation['account_id'],
                     {'event_id': f'fixture-result-{ordinal}', 'version': 1, 'result': result})
    presets.delete('2', presets.snapshot()['version'])

    @app.middleware('http')
    async def fixture_only(request, call_next):
        path = request.url.path
        if path.startswith(('/api/cloud/', '/admin/api/mail')):
            return JSONResponse({'detail': '合成页面验证不执行邮件、设备或客户端动作。'}, status_code=503)
        # The production portal requires HTTPS. This local-only fixture keeps its
        # real handlers while adapting the synthetic session to loopback HTTP.
        host = request.headers.get('host', '')
        if request.headers.get('origin') == 'http://' + host:
            request.scope['headers'] = [(name, ('https://' + host).encode() if name == b'origin' else value)
                                         for name, value in request.scope['headers']]
        if path == '/admin/api/accounts' and account_delay_ms:
            await asyncio.sleep(account_delay_ms / 1000)
        response = await call_next(request)
        if 'set-cookie' in response.headers:
            response.headers['set-cookie'] = response.headers['set-cookie'].replace('; Secure', '')
        return response

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18867)
    parser.add_argument('--count', type=int, default=45)
    parser.add_argument('--account-delay-ms', type=int, default=150)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='ournotes-admin-browser-') as folder:
        app = create_fixture(Path(folder), count=args.count, account_delay_ms=args.account_delay_ms)
        print(f'Synthetic admin fixture http://127.0.0.1:{args.port}/admin/ · Fuyuki / {PASSWORD}', flush=True)
        uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
