"""Portable bootstrap: report readiness and failures without importing app dependencies."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


def ready(url):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url + '/api/launcher/ping', timeout=1) as response:
            if json.load(response).get('app') != 'ournotes-launcher':
                return False
        with opener.open(url + '/', timeout=1) as response:
            return response.status == 200 and bool(response.read(1))
    except (OSError, ValueError):
        return False


def wait_ready(process, url, *, timeout=60, probe=ready, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout
    while clock() < deadline:
        if probe(url):
            return
        if process.poll() is not None:
            raise RuntimeError('后台进程已提前退出。')
        sleep(.5)
    raise RuntimeError('等待控制台就绪超过 60 秒；后台可能仍在启动，请查看日志。')


def boot(root):
    root = Path(root).resolve()
    pointer = json.loads((root / 'current.json').read_text('utf-8'))
    version = pointer.get('version', '')
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('版本指针无效。')
    app = root / 'versions' / version / 'app'
    entry = app / 'scripts/webui.py'
    if not entry.is_file():
        raise ValueError('发行包不完整：缺少控制台入口。')
    data = root / 'user-data'
    logs = data / 'state/webui'
    logs.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OURNOTES_INSTALL_ROOT=str(root), OURNOTES_DATA_ROOT=str(data),
               PYTHONDONTWRITEBYTECODE='1')
    if (root / 'client-connection.json').is_file():
        subprocess.run([sys.executable, '-X', 'utf8', str(root / 'configure_client.py')],
                       env=env, check=True)
    url = 'http://127.0.0.1:8766'
    if not ready(url):
        options = {'creationflags': subprocess.CREATE_NO_WINDOW}
        # Keep only the latest bootstrap's early import/launch errors.
        with (logs / 'boot.log').open('wb') as output:
            process = subprocess.Popen([sys.executable, '-X', 'utf8', str(entry), '--no-browser'],
                                       cwd=app, env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, **options)
        wait_ready(process, url)
    if not webbrowser.open(url):
        print('控制台已就绪，请在浏览器打开：' + url)
    return url


if __name__ == '__main__':
    try:
        boot(Path(__file__).resolve().parent)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'控制台启动失败：{error}', file=sys.stderr)
        print('请检查目录写入权限、8766 端口以及发行包是否完整。', file=sys.stderr)
        print('日志目录：' + str(Path(__file__).resolve().parent / 'user-data/state/webui'), file=sys.stderr)
        sys.exit(1)
