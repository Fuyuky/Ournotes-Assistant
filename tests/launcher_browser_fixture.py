"""Serve synthetic console data for browser QA, with device and network work disabled."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18866)
    args = parser.parse_args()
    import uvicorn

    from ournotes.common.files import atomic_json
    from ournotes.control.launcher import Launcher
    from ournotes.control.web import create_app
    from ournotes.integrations import cloud_client, cloud_sync
    from ournotes.proxy.ui.web import Extension as Clash
    from ournotes.rebind.web import Extension as Rebind
    from ournotes.runtime.presets import canonical_preset

    preset = canonical_preset({'name': '测试共享预设', 'description': '仅供页面检查', 'entries': [
        {'pool_key': 'fixture-pool', 'resource': 'stars', 'pulls': 43}]})
    presets = {'version': 1, 'presets': {'0': preset}}
    class Cloud:
        next_preset_id = 1
        def presets(self):
            return json.loads(json.dumps(presets))
        def get_result_by_index(self, index):
            return {'version': 1, 'usage_status': 'available', 'owner_username': '测试管理员',
                    'result': {'index': index, 'email': 'fixture@example.test', 'account_id': 'fixture-account',
                               'used': False, 'pull_count': 43, 'preset_snapshot': preset,
                               'cards': [{'card': {'kind': 'member', 'id': 1, 'rarity': 'SSR',
                                                  'name': '测试角色', 'title': '测试卡面'}, 'count': 2},
                                         {'card': {'kind': 'support', 'id': 1, 'rarity': 'SSR',
                                                  'name': '测试支援', 'title': '支援卡面'}, 'count': 1}]}}
        def usage_capability(self):
            return {'usage_protocol': 2}
        def card_art(self, kind, identity):
            import io

            from PIL import Image
            output = io.BytesIO()
            Image.new('RGB', (220, 280), '#bbade0' if kind == 'member' else '#92bbc7').save(output, 'PNG')
            return output.getvalue(), 'image/png'
        def create_preset(self, chosen, expected_version):
            key = str(Cloud.next_preset_id)
            value = self.save_preset(key, chosen, expected_version)
            Cloud.next_preset_id += 1
            return {**value, 'created_id': int(key)}
        def save_preset(self, key, chosen, expected_version):
            if expected_version != presets['version']:
                raise ValueError('共享版本已变化。')
            presets['presets'][key] = canonical_preset(chosen)
            presets['version'] += 1
            return self.presets()
        def delete_preset(self, key, expected_version):
            if expected_version != presets['version']:
                raise ValueError('共享版本已变化。')
            del presets['presets'][key]
            presets['version'] += 1
            return self.presets()
    cloud_client.CloudClient.from_root = classmethod(lambda cls, root: Cloud())
    cloud_client.CloudClientConfig.load = classmethod(lambda cls, root: SimpleNamespace(
        public=lambda: {'configured': True, 'service_url': 'https://fixture.invalid'}))
    cloud_sync.SyncWorker.start = lambda self: None
    cloud_sync.SyncWorker.close = lambda self: None
    with tempfile.TemporaryDirectory(prefix='ournotes-console-browser-') as folder:
        root = Path(folder)
        (root/'state').mkdir()
        with sqlite3.connect(root/'state/runtime.sqlite3') as db:
            db.executescript('CREATE TABLE batches(id TEXT,created REAL,data TEXT);'
                'CREATE TABLE runs(id TEXT,data TEXT);CREATE TABLE accounts(id TEXT,alias_index INTEGER,status TEXT,phase TEXT,run_id TEXT,data TEXT);'
                'CREATE TABLE counters(channel TEXT,next_index INTEGER);')
            batch = {'id': 'b'*32, 'status': 'complete', 'lanes': [], 'total_count': 8, 'count_mode': 'shared'}
            db.execute('INSERT INTO batches VALUES(?,?,?)', (batch['id'], 1, json.dumps(batch)))
        directory = root/'data/catalog/snapshots/fixture'
        atomic_json(directory/'pools.json', {'pools': [{'key': 'fixture-pool', 'name': '测试卡池',
            'gacha_id': 12, 'ticket_name': '测试券', 'pickup_card_keys': ['member:1'],
            'unsupported_card_keys': [], 'products': [{'itemType': 12, 'drawCount': n, 'price': 200*n} for n in (1, 10)]}]})
        atomic_json(directory/'catalog.json', {'cards': [{'key': 'member:1', 'kind': 'member', 'rarity': 'SSR', 'name': '测试角色'}]})
        atomic_json(root/'data/catalog/current.json', {'directory': 'snapshots/fixture',
            'sha256': {name: hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in ('pools.json','catalog.json')}})
        atomic_json(root/'state/game-update.json', {'status': 'failed', 'message': '[WinError 32] 上次安装包占用',
            'results': [{'instance': 1, 'status': 'updated', 'version': '1.0.3'}]})
        launcher = Launcher(root)
        launcher.updates.check = lambda: launcher.updates.status()
        launcher.notifications.start = lambda: None
        launcher.notifications.close = lambda: None
        def disabled(*args, **kwargs):
            raise ValueError('页面测试不执行运行、设备、通知或网络动作。')
        launcher.dispatch = disabled
        launcher.game_update.start = disabled
        launcher.notifications.test = disabled
        launcher.updates.download = disabled
        launcher.updates.start_install = disabled
        rebind = Rebind(root, service=SimpleNamespace(status=lambda: {'task': None, 'running': False}, close=lambda: None))
        clash = Clash(root)
        clash.status = lambda **kwargs: {'connection': {'state': 'connected', 'node': '测试节点',
            'group': '测试代理组', 'transport': '本机连接', 'message': '测试连接已就绪', 'checked_at': 1},
            'rotation': {'protocol': 2, 'state': 'ready', 'used': 7, 'reserved': 0, 'nodes': [], 'remaining_nodes': 2}}
        def decorate(html):
            return clash.decorate(rebind.decorate(html))
        extension = SimpleNamespace(rebind=rebind, decorate=decorate,
            status=lambda: {'email_rebind': rebind.service.status(), 'clash': clash.status()},
            mount=lambda app: (rebind.mount(app), clash.mount(app)), close=lambda: None)
        app = create_app(root, launcher, extension=extension)
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=args.port, access_log=False))
        app.state.shutdown = lambda: setattr(server, 'should_exit', True)
        print(f'Synthetic console fixture http://127.0.0.1:{args.port}', flush=True)
        server.run()


if __name__ == '__main__':
    main()
