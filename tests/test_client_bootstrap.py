import json
from types import SimpleNamespace

import pytest

from deploy.client.configure_client import configure


class Config:
    value = None

    @classmethod
    def load(cls, root):
        return cls.value

    @classmethod
    def save(cls, root, service_url, credential):
        cls.value = SimpleNamespace(service_url=service_url, credential=credential)


def seed(tmp_path):
    path = tmp_path / 'client-connection.json'
    path.write_text(json.dumps({'service_url': 'https://example.com', 'credential': 'synthetic-node-token'}))
    return path


def test_first_boot_imports_and_removes_seed(tmp_path):
    Config.value = None
    path = seed(tmp_path)
    assert configure(tmp_path, tmp_path / 'data', Config) == 'imported'
    assert Config.value.credential == 'synthetic-node-token'
    assert not path.exists()
    assert configure(tmp_path, tmp_path / 'data', Config) == 'no_seed'


def test_existing_node_is_not_replaced(tmp_path):
    Config.value = SimpleNamespace(credential='existing-node')
    path = seed(tmp_path)
    assert configure(tmp_path, tmp_path / 'data', Config) == 'already_configured'
    assert Config.value.credential == 'existing-node'
    assert not path.exists()


def test_save_failure_keeps_seed_for_retry(tmp_path):
    class Broken(Config):
        value = None

        @classmethod
        def save(cls, *args):
            raise OSError('synthetic write failure')

    path = seed(tmp_path)
    with pytest.raises(OSError):
        configure(tmp_path, tmp_path / 'data', Broken)
    assert path.exists()
