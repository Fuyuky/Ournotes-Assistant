"""Migration preserves private routing and old assets; no production changes."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('publication_proxy', Path(__file__).parents[1] / 'deploy/cloud/publication_proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


@pytest.fixture
def config():
    body = '''    server_name 192.0.2.1;
    root /var/www/ournotes-cards/current;
    location ^~ /.well-known/acme-challenge/ { root /var/www/acme; }
    location = / { try_files /index.html =404; }
    location = /index.html { try_files $uri =404; }
    location = /data/current.json { try_files $uri =404; }
    location ~ "__PUBLIC_RESOURCES__" {
        try_files $uri =404;
    }
    location / { return 404; }
'''
    return ('server {\n    listen 80;\n' + body + '}\nserver {\n    listen 443 ssl;\n'
            '    ssl_certificate /etc/cert/fullchain.pem;\n'
            '    location ^~ /api/cloud/v1/ {\n'
            '        proxy_pass http://127.0.0.1:8770;\n'
            '        proxy_set_header Authorization $http_authorization;\n'
            '    }\n' + body + '}\n').replace('__PUBLIC_RESOURCES__', proxy.PUBLIC_RESOURCES)


def test_migration_proxies_only_public_routes_and_preserves_private_api(config):
    updated = proxy.render(config)
    assert updated.count('proxy_pass http://127.0.0.1:8770;') == 9
    assert updated.count('proxy_set_header Authorization "";') == 8
    assert updated.count('proxy_set_header Authorization $http_authorization;') == 1
    assert updated.count('try_files $uri @ournotes_published;') == 2
    assert updated.count('location / { return 404; }') == 2
    assert updated.count('location ^~ /.well-known/acme-challenge/') == 2
    assert updated.count(proxy.PUBLIC_RESOURCES) == 2
    assert 'ssl_certificate /etc/cert/fullchain.pem;' in updated
    assert proxy.render(updated) == updated


@pytest.mark.parametrize('replace', [
    ('listen 80;', 'listen 80;\nserver {'),
    ('location = /data/current.json', 'location = /data/other.json'),
    ('location ~ "', 'location ~* "'),
    ('location / { return 404; }', 'location / { autoindex on; }'),
])
def test_unexpected_routing_is_rejected(config, replace):
    with pytest.raises(ValueError):
        proxy.render(config.replace(*replace, 1))


def test_partial_migration_is_rejected(config):
    with pytest.raises(ValueError, match='Incomplete'):
        proxy.render(config + proxy.MARKER)


def test_broader_resource_whitelist_is_rejected(config):
    with pytest.raises(ValueError, match='whitelist'):
        proxy.render(config.replace(proxy.PUBLIC_RESOURCES, '^/.*$', 1))


def test_cli_never_overwrites_live_or_existing_candidate(config, tmp_path, monkeypatch):
    source = tmp_path / 'live.conf'; source.write_text(config)
    monkeypatch.setattr('sys.argv', ['publication_proxy', '--input', str(source), '--output', str(source)])
    with pytest.raises(SystemExit):
        proxy.main()
    assert source.read_text() == config
    candidate = tmp_path / 'next.conf'
    monkeypatch.setattr('sys.argv', ['publication_proxy', '--input', str(source), '--output', str(candidate)])
    proxy.main()
    assert candidate.read_text() == proxy.render(config)
    with pytest.raises(SystemExit):
        proxy.main()
