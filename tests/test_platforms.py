
import pytest

from ournotes.platforms.system import current_platform


@pytest.mark.parametrize('system,machine,expected', [
    ('win32', 'AMD64', 'windows-x64'), ('win32', 'x86_64', 'windows-x64'),
])
def test_supported_client_architectures(system, machine, expected):
    assert current_platform(system, machine) == expected


@pytest.mark.parametrize('system,machine', [('darwin', 'arm64'), ('darwin', 'x86_64'), ('win32', 'ARM64'), ('linux', 'aarch64')])
def test_non_windows_x64_targets_are_not_shipped(system, machine):
    with pytest.raises(RuntimeError, match='Windows x64'):
        current_platform(system, machine)


@pytest.mark.parametrize('name', ['../other', 'a/b', 'a\\b', 'bad:name'])
def test_secret_and_lock_names_cannot_escape_their_root(tmp_path, name):
    from ournotes.common.secrets import SecretStore
    store = SecretStore(tmp_path)
    with pytest.raises(ValueError):
        store.load(name)
    with pytest.raises(ValueError):
        store.save(name, {})
    with pytest.raises(ValueError):
        store.delete(name)
    with pytest.raises(ValueError), store.lock(name):
        pass
    assert not list(tmp_path.iterdir())


def test_controller_rejects_remote_transport_and_header_injection():
    from ournotes.platforms.proxy import request
    with pytest.raises(ValueError, match='本机'):
        request('/proxies', address='https://remote.example.com')
    with pytest.raises(ValueError, match='路径'):
        request('/proxies\r\nBad: yes', address='127.0.0.1:9090')
    with pytest.raises(ValueError, match='凭据'):
        request('/proxies', secret='bad\nsecret', address='127.0.0.1:9090')


def test_controller_cannot_redirect_a_local_request_or_credential():
    from urllib.request import Request

    from ournotes.platforms.proxy import NoRedirect
    request = Request('http://localhost:9090/proxies', headers={'Authorization': 'Bearer synthetic-only'})
    with pytest.raises(ValueError, match='不接受重定向'):
        NoRedirect().redirect_request(request, None, 302, 'redirect', {}, 'https://remote.example.com')


def test_windows_credentials_use_dpapi_without_plaintext(tmp_path):
    from ournotes.common.secrets import SecretStore
    store = SecretStore(tmp_path)
    value = {'credential': 'synthetic-secret'}
    store.save('node', value)
    assert store.load('node') == value
    assert b'synthetic-secret' not in (tmp_path / 'node.dpapi').read_bytes()
    store.delete('node')
    assert store.load('node') is None
