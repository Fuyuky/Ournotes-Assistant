"""Explicit, manual-only official game APK updates. Never imported by the scheduler."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import struct
import threading
import time
import urllib.request
import zipfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from ournotes.common.devices import instance_leases, lock_held
from ournotes.common.files import atomic_json
from ournotes.common.memory import read_commit_memory
from ournotes.common.secrets import SecretStore
from ournotes.integrations.mumu import (
    app_info,
    close_app,
    install_apk,
    instance_info,
    launch_instance,
    shutdown_instances,
)

PACKAGE = 'com.bilibili.sirius.official'
MAX_APK = 1024 ** 3


def official_url(value):
    value = value.strip()
    url = urlsplit(value)
    prefixes = {'pkg.biligame.com': '/games/', 'l12-pkg-download.biligames.com': '/sirius/apk/'}
    if (url.scheme != 'https' or url.hostname not in prefixes or url.port not in (None, 443)
            or url.username or url.password or url.query or url.fragment
            or not url.path.startswith(prefixes[url.hostname])
            or not re.fullmatch(r'/[A-Za-z0-9_./-]+\.apk', url.path) or '..' in url.path):
        raise ValueError('官方返回的 APK 地址不在允许的 HTTPS 下载范围内。')
    return value


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


VERSION_ENDPOINT = ('https://l14-prod-hk-all-gs-sirius.gamerfusiontech.com/'
                    'app.masterdata.MasterdataService/Version')


class NoDiscoveryRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('官方版本查询发生重定向，已停止更新。')


def discover_official_apk():
    """Public read-only version RPC; no login, device identity or player credentials.

    An intentionally old client version requests the server's required release.
    The website's static download button may lag behind this response.
    """
    request = urllib.request.Request(VERSION_ENDPOINT, data=b'\0\0\0\0\0', headers={
        'Content-Type': 'application/grpc-web+proto', 'x-grpc-web': '1',
        'x-client-version': '1.0.2', 'x-platform': 'Android'})
    with urllib.request.build_opener(NoDiscoveryRedirect()).open(request, timeout=30) as response:
        url = response.headers.get('x-client-download-url', '')
        version = response.headers.get('x-client-recommended-version', '')
        if not url or not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', version):
            raise ValueError('官方版本接口未返回有效的更新地址与版本，请稍后手动重试。')
        return {'url': official_url(url), 'version': version, 'source': VERSION_ENDPOINT}


def apk_identity(path):
    """Read only the root manifest's binary XML attributes; never execute APK content."""
    with zipfile.ZipFile(path) as archive:
        item = archive.getinfo('AndroidManifest.xml')
        if item.file_size > 4 * 1024 * 1024:
            raise ValueError('APK manifest 过大。')
        data = archive.read(item)
    if len(data) < 8 or struct.unpack_from('<H', data)[0] != 3:
        raise ValueError('不是有效的 Android APK。')
    strings, offset = [], 8
    while offset + 8 <= len(data):
        kind, header, size = struct.unpack_from('<HHI', data, offset)
        if header < 8 or size < header or offset + size > len(data):
            raise ValueError('APK manifest 损坏。')
        if kind == 1:
            count, _, flags, start, _ = struct.unpack_from('<5I', data, offset + 8)
            if count > 100000 or header + count * 4 > size:
                raise ValueError('APK 字符串表无效。')
            def length(pos, utf8):
                value = data[pos] if utf8 else struct.unpack_from('<H', data, pos)[0]
                step, mask = (1, 0x80) if utf8 else (2, 0x8000)
                if value & mask:
                    next_value = data[pos + step] if utf8 else struct.unpack_from('<H', data, pos + step)[0]
                    return ((value & (mask - 1)) << (8 if utf8 else 16)) | next_value, pos + step * 2
                return value, pos + step
            for i in range(count):
                pos = offset + start + struct.unpack_from('<I', data, offset + header + i * 4)[0]
                utf8 = bool(flags & 0x100)
                n, pos = length(pos, utf8)
                if utf8:
                    n, pos = length(pos, True)
                else:
                    n *= 2
                if pos + n > offset + size:
                    raise ValueError('APK 字符串越界。')
                strings.append(data[pos:pos + n].decode('utf-8' if utf8 else 'utf-16le'))
        elif kind == 0x102:
            name = struct.unpack_from('<I', data, offset + 20)[0]
            if strings[name] != 'manifest':
                raise ValueError('APK 根节点无效。')
            start, width, count = struct.unpack_from('<HHH', data, offset + 24)
            if width < 20 or 16 + start + width * count > size:
                raise ValueError('APK 属性无效。')
            attrs = {}
            for i in range(count):
                pos = offset + 16 + start + width * i
                ns, key, raw = struct.unpack_from('<III', data, pos)
                dtype, value = data[pos + 15], struct.unpack_from('<I', data, pos + 16)[0]
                namespace = '' if ns == 0xffffffff else strings[ns]
                attrs[namespace, strings[key]] = (strings[raw] if raw != 0xffffffff
                    else strings[value] if dtype == 3 else value)
            android = 'http://schemas.android.com/apk/res/android'
            result = {'package': attrs.get(('', 'package')),
                      'version': str(attrs.get((android, 'versionName'), '')),
                      'version_code': int(attrs.get((android, 'versionCode'), 0))}
            if result['package'] != PACKAGE or result['version_code'] <= 0:
                raise ValueError('安装包不是 Our Notes 官方渠道，已拒绝安装。')
            return result
        offset += size
    raise ValueError('无法读取 APK 身份。')


def check_transactions(root, instances):
    database = root / 'state/runtime.sqlite3'
    if not database.exists():
        return
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        runs = [key for key, raw in db.execute('SELECT id,data FROM runs')
                if json.loads(raw).get('instance') in instances]
        for run in runs:
            if db.execute("SELECT 1 FROM draws d JOIN accounts a ON a.id=d.account_id "
                          "WHERE a.run_id=? AND d.status IN ('submitted','captured','needs_review') LIMIT 1",
                          (run,)).fetchone():
                raise ValueError('所选实例存在未决抽卡交易，请先核查；未关闭游戏或安装。')


def download(url, path, progress, stop):
    opener = urllib.request.build_opener(OfficialRedirect())
    request = urllib.request.Request(official_url(url), headers={
        'User-Agent': 'Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/110.0.0.0 Mobile Safari/537.36',
        'Referer': 'https://bdon.biligames.com/'})
    digest, received, last = hashlib.sha256(), 0, 0
    deadline = time.monotonic() + 3600
    with opener.open(request, timeout=30) as response, path.open('wb') as stream:
        official_url(response.url)
        total = int(response.headers.get('Content-Length', 0))
        if total > MAX_APK:
            raise ValueError('安装包超过 1 GiB 限制。')
        while chunk := response.read(256 * 1024):
            if stop.is_set() or time.monotonic() > deadline:
                raise ValueError('下载已取消或超时；未安装游戏。')
            received += len(chunk)
            if received > MAX_APK:
                raise ValueError('安装包超过 1 GiB 限制。')
            stream.write(chunk)
            digest.update(chunk)
            if time.monotonic() - last >= 1:
                progress(stage='downloading', message='正在下载官方安装包', bytes=received, total_bytes=total)
                last = time.monotonic()
        if not received or (total and received != total):
            raise ValueError('安装包下载不完整。')
    return digest.hexdigest()


def install(instances, apk, identity, progress, stop=None):
    results = []
    for instance in instances:
        if stop and stop.is_set():
            raise ValueError('控制台正在关闭；已完成当前实例，未更新后续实例。')
        started = not instance_info(instance=instance).get('is_process_started')
        install_pending = False
        try:
            if started:
                memory = read_commit_memory()
                if memory and memory.low:
                    raise ValueError('系统提交内存不足，未启动下一个实例。')
                progress(stage='starting', message=f'正在启动实例 {instance}', results=results)
                launch_instance(instance)
            deadline = time.monotonic() + 180
            while not instance_info(instance=instance).get('is_android_started'):
                if time.monotonic() > deadline:
                    raise ValueError(f'实例 {instance} 启动超时。')
                time.sleep(1)
            before = app_info(instance, PACKAGE)
            if not before:
                raise ValueError(f'实例 {instance} 未安装官方渠道游戏，不能执行覆盖更新。')
            if int(before['version_code']) >= identity['version_code']:
                result = {'instance': instance, 'status': 'skipped', 'version': before['version'],
                          'message': '提供的安装包不高于已安装版本，未覆盖。'}
            else:
                progress(stage='installing', message=f'正在覆盖更新实例 {instance}', results=results)
                close_app(instance, PACKAGE)
                install_pending = True
                install_apk(instance, apk)
                after = app_info(instance, PACKAGE)
                if not after or int(after['version_code']) != identity['version_code']:
                    raise ValueError(f'实例 {instance} 安装后版本核验失败；请手动检查，未自动重试。')
                install_pending = False
                result = {'instance': instance, 'status': 'updated', 'version': after['version'],
                          'version_code': after['version_code']}
            results.append(result)
            progress(results=list(results))
        finally:
            if install_pending:
                progress(message=f'实例 {instance} 安装结果待核查，已保留实例，不自动重试。',
                         needs_review_instance=instance)
            elif started:
                shutdown_instances([instance])
    return results


class GameUpdate:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / 'state/game-update.json'
        self.mutex, self.stop = threading.RLock(), threading.Event()
        self.thread, self.closing = None, False
        try:
            self.value = json.loads(self.path.read_text('utf-8'))
        except (OSError, ValueError):
            self.value = {'status': 'idle', 'message': '仅在手动点击后下载并更新游戏。'}
        if self.value.get('status') == 'running':
            self.value.update(status='interrupted', message='上次更新中断；请核对实例版本后手动重试。')
        self.historical = self.value.get('status') != 'idle'
        self.dismissed = False

    def status(self):
        with self.mutex:
            value = json.loads(json.dumps(self.value))
            if self.dismissed:
                return {'status': 'idle', 'message': '历史更新结果已收起。', 'historical': False}
            return {**value, 'historical': self.historical}

    def dismiss(self):
        with self.mutex:
            if self.busy():
                raise ValueError('更新正在执行，不能收起当前结果。')
            self.dismissed = True
            return self.status()

    def busy(self):
        return bool(self.thread and self.thread.is_alive())

    def progress(self, **values):
        with self.mutex:
            self.value.update(values, updated_at=time.time())
            atomic_json(self.path, self.value)

    def start(self, instances, request_id):
        if not instances or len(instances) > 32 or any(type(i) is not int or not 0 <= i <= 255 for i in instances):
            raise ValueError('请选择有效的 MuMu 实例。')
        instances = sorted(set(instances))
        with self.mutex:
            if self.closing:
                raise ValueError('控制台正在关闭。')
            if self.value.get('request_id') == request_id:
                if self.value.get('instances') != instances:
                    raise ValueError('同一请求编号不能用于不同更新内容。')
                return self.status()
            if self.busy() or lock_held(self.root, 'runner'):
                raise ValueError('请先安全暂停批次，并等待当前更新结束。')
            for i in instances:
                if lock_held(self.root, f'device-{i}'):
                    raise ValueError(f'实例 {i} 正被运行或换绑任务占用。')
            check_transactions(self.root, instances)
            self.value = {'request_id': request_id, 'instances': instances,
                          'status': 'running', 'stage': 'queued', 'message': '等待更新', 'results': []}
            self.historical = self.dismissed = False
            self.progress()
            self.stop.clear()
            self.thread = threading.Thread(target=self.work, args=(instances,), name='game-update')
            self.thread.start()
            return self.status()

    def work(self, instances):
        try:
            with SecretStore(self.root / 'state').lock('game-update'), \
                    SecretStore(self.root / 'state').lock('runner'), instance_leases(self.root, instances):
                check_transactions(self.root, instances)
                self.progress(stage='discovering', message='正在查询官方游戏版本')
                release = discover_official_apk()
                self.progress(release=release, message=f"已找到官方版本 {release['version']}")
                if self.stop.is_set():
                    raise ValueError('更新已取消；未下载或安装游戏。')
                folder = self.root / 'state/game-update-cache'
                folder.mkdir(exist_ok=True)
                if folder.is_symlink() or folder.is_junction():
                    raise ValueError('更新缓存目录不能是链接。')
                apk = folder / 'download.apk'
                if apk.is_symlink():
                    raise ValueError('更新缓存文件不能是链接。')
                try:
                    digest = download(release['url'], apk, self.progress, self.stop)
                    identity = apk_identity(apk)
                    if identity['version'] != release['version']:
                        raise ValueError('APK 实际版本与官方推荐版本不一致，已停止安装。')
                    self.progress(stage='verifying', message='正在校验 APK 完整性', package=identity, sha256=digest)
                    with zipfile.ZipFile(apk) as archive:
                        if sum(i.file_size for i in archive.infolist()) > 4 * MAX_APK or archive.testzip():
                            raise ValueError('APK 完整性校验失败。')
                    if self.stop.is_set():
                        raise ValueError('更新已取消；未安装游戏。')
                    result = install(instances, apk, identity, self.progress, self.stop)
                    self.progress(status='success', stage='done', results=result,
                                  message='所选实例处理完成。批次未恢复；请查看各实例结果。')
                finally:
                    try:
                        apk.unlink(missing_ok=True)
                    except OSError:
                        # MuMu may retain the installer handle after its installed
                        # version is verified. Cache cleanup is not installation.
                        self.progress(cache_cleanup_pending=True,
                                      cache_cleanup_message='安装包缓存暂时被占用，已保留；安装结果以实例核验为准。')
        except Exception as error:  # noqa: BLE001 -- durable boundary; no automatic retry
            from ournotes.control.launcher import redact
            prefix = ('安装结果待核查，实例已保留；' if self.value.get('needs_review_instance') is not None else '')
            self.progress(status='failed', message=prefix + redact(error)[:500])

    def cancel(self):
        self.stop.set()
        if self.busy():
            self.progress(message='已请求取消；进行中的安装会先完成，后续实例不再更新。')
        return self.status()

    def close(self):
        with self.mutex:
            self.closing = True
            self.stop.set()
        if self.thread:
            self.thread.join()  # An in-flight package install must settle before shutdown.
