import json
import os

import pytest

from ournotes.common.files import atomic_json


def test_windows_transient_read_lock_keeps_old_json_until_atomic_replace(tmp_path, monkeypatch):
    path = tmp_path/'status.json'
    path.write_text('{"version": 1}', encoding='utf8')
    original, calls = os.replace, []
    def replace(source, target):
        calls.append(1)
        if len(calls) == 1:
            assert json.loads(path.read_text()) == {'version': 1}
            error = PermissionError('simulated sharing violation')
            error.winerror = 32
            raise error
        original(source, target)
    monkeypatch.setattr(os, 'replace', replace)
    atomic_json(path, {'version': 2})
    assert json.loads(path.read_text()) == {'version': 2}
    assert len(calls) == 2 and not list(tmp_path.glob('*.tmp'))


def test_unrelated_write_error_is_not_swallowed(tmp_path, monkeypatch):
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr(os, 'replace', fail)
    with pytest.raises(OSError, match='disk failure'):
        atomic_json(tmp_path/'status.json', {})
