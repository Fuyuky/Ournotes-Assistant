"""Local Mihomo controller transports: Windows pipe or loopback HTTP."""
from __future__ import annotations

import http.client
import io
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

import yaml


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        raise ValueError('Clash本机控制接口不接受重定向。')


def controller_config():
    selected = os.environ.get('OURNOTES_CLASH_CONFIG')
    if selected:
        paths = [Path(selected).expanduser()]
    else:
        base = Path(os.environ['APPDATA'])
        folders = ('cashrev', 'io.github.clash-verge-rev.clash-verge-rev',
                   'io.github.clash-verge.clash-verge')
        paths = [base / folder / name for folder in folders
                 for name in ('caserg.yaml', 'clash-verge.yaml', 'config.yaml')]
    for path in paths:
        if path.is_file():
            config = yaml.safe_load(path.read_text('utf-8')) or {}
            if any(config.get(name) for name in ('external-controller-pipe',
                                                 'external-controller')):
                return config
    raise ValueError('未找到本机Clash控制接口配置。')


def open_pipe(path):
    deadline = time.monotonic() + 5
    while True:
        try:
            return open(path, 'r+b', buffering=0)
        except OSError as error:
            if error.errno != 22 and getattr(error, 'winerror', None) != 231:
                raise
            if time.monotonic() >= deadline:
                raise ValueError('Clash控制管道持续繁忙，请稍后恢复。') from error
            time.sleep(.05)


def request(path, body=None, *, method='GET', pipe=None, address=None, secret=''):
    if not path.startswith('/') or any(char in path for char in '\r\n'):
        raise ValueError('Clash控制路径无效。')
    if any(char in secret for char in '\r\n'):
        raise ValueError('Clash控制凭据格式无效。')
    if method not in ('GET', 'PUT', 'POST', 'DELETE', 'PATCH'):
        raise ValueError('Clash控制方法无效。')
    headers = {'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'}
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else b''
    if pipe:
        wire = (f'{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
                + ''.join(f'{key}: {value}\r\n' for key, value in headers.items())
                + f'Content-Length: {len(data)}\r\n\r\n').encode() + data
        if os.name != 'nt' or not str(pipe).startswith('\\\\.\\pipe\\'):
            raise ValueError('Clash命名管道只接受Windows本机管道。')
        class PipeSocket:
            def __init__(self, stream):
                self.stream = stream
            def makefile(self, *_):
                return io.BufferedReader(self.stream)
        with open_pipe(pipe) as stream:
            stream.write(wire)
            response = http.client.HTTPResponse(PipeSocket(stream))
            response.begin()
            raw = response.read(4 * 1024 * 1024 + 1)
            status = response.status
        if not 200 <= status < 300:
            raise ValueError('Clash拒绝控制请求，请核对本机控制配置。')
    else:
        url = address if str(address).startswith(('http://', 'https://')) else 'http://' + str(address)
        parsed = urlsplit(url)
        if (parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or parsed.username
                or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('Clash控制接口必须位于本机。')
        call = Request(url.rstrip('/') + path, data=data if body is not None else None,
                       headers=headers, method=method)
        with build_opener(ProxyHandler({}), NoRedirect()).open(call, timeout=5) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('Clash控制响应过大。')
    return json.loads(raw) if raw else {}
