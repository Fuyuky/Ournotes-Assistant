"""Verify a portable package and boot its runtime in a disposable data directory."""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ournotes.updates.portable import validate_package


def smoke(package, public_key, platform):
    with tempfile.TemporaryDirectory(prefix='ournotes-release-') as temporary:
        root = Path(temporary) / '中文 portable'
        root.mkdir()
        envelope = validate_package(package, public_key, platform, root)
        version = envelope['manifest']['version']
        version_root = root / 'versions' / version
        app = version_root / 'app'
        python = version_root / ('runtime/python.exe' if platform == 'windows-x64' else 'runtime/bin/python3')
        env = dict(os.environ, OURNOTES_DATA_ROOT=str(root / 'user-data'), PYTHONDONTWRITEBYTECODE='1')
        env.pop('OURNOTES_INSTALL_ROOT', None)
        code = (
            'import sys; sys.path.insert(0, sys.argv[1]); '
            'import cv2, numpy, cryptography, onnxruntime, fastapi, uvicorn, websockets; '
            'import ournotes.control.web, ournotes.runtime.scheduler, ournotes.rebind.web; '
            'from rapidocr_onnxruntime import RapidOCR; '
            'RapidOCR()(numpy.full((64,128,3),255,dtype=numpy.uint8)); '
            "print('Packaged imports and offline OCR passed')"
        )
        subprocess.run([str(python), '-X', 'utf8', '-c', code, str(app)], env=env, check=True, timeout=120)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        with (root / 'smoke.log').open('wb') as output:
            options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            process = subprocess.Popen([str(python), '-X', 'utf8', str(app / 'scripts/webui.py'),
                                        '--no-browser', '--port', str(port)],
                                       env=env, stdout=output, stderr=subprocess.STDOUT, **options)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    with opener.open(f'http://127.0.0.1:{port}/api/launcher/ping', timeout=1) as response:
                        assert json.load(response)['app'] == 'ournotes-launcher'
                    with opener.open(f'http://127.0.0.1:{port}/', timeout=1) as response:
                        assert b'mumu-settings' in response.read()
                    print('Packaged WebUI startup passed')
                    break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError('Packaged WebUI exited: ' + (root / 'smoke.log').read_text('utf-8')) from None
                    time.sleep(.25)
            else:
                raise RuntimeError('Packaged WebUI startup timed out')
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--public-key', type=Path, required=True)
    parser.add_argument('--platform', required=True)
    args = parser.parse_args()
    smoke(args.package, args.public_key.read_text('utf-8').strip(), args.platform)
