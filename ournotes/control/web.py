"""Loopback-only WebUI. No command shell, public deployment, or startup game actions."""

from __future__ import annotations

import asyncio
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Path, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from ournotes.control.launcher import Launcher
from ournotes.paths import DATA_ROOT, ROOT

WEB = ROOT / "web/launcher"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Lane(StrictModel):
    instance: int = Field(ge=0, le=255, strict=True)
    preset: str = Field(min_length=1, max_length=80)


class RunOptions(StrictModel):
    lanes: list[Lane] = Field(min_length=1, max_length=32)
    count: int = Field(default=1, ge=0, le=10000, strict=True)
    channel: Literal["test", "formal"] = "test"
    async_start: bool = False
    debug_mode: bool = False
    preset_version: int | None = Field(default=None, ge=0, strict=True)


class Action(StrictModel):
    command_id: str = Field(default_factory=lambda: secrets.token_hex(16), pattern=r"^[A-Za-z0-9_-]{8,128}$")
    expected_version: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    action: Literal[
        "start",
        "resume",
        "resume_lane",
        "pause",
        "finish",
        "cancel",
        "mumu_check",
        "gmail_check",
    ]
    run: RunOptions | None = None
    index: int | None = Field(default=None, ge=0, le=2**53 - 1, strict=True)
    instance: int = Field(default=0, ge=0, le=255, strict=True)
    expected_revision: int | None = Field(default=None, ge=0, strict=True)
    batch_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    recovery_mode: Literal['retry', 'restart_game'] = 'retry'


class CloudSettings(StrictModel):
    service_url: str = Field(max_length=250)
    credential: str = Field(min_length=16, max_length=2048)


class Folder(StrictModel):
    target: Literal["results", "logs", "batch"]


class GameUpdateRequest(StrictModel):
    instances: list[Annotated[int, Field(strict=True, ge=0, le=255)]] = Field(min_length=1, max_length=32)
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class NotificationSettings(StrictModel):
    enabled: bool = Field(strict=True)


class Account(StrictModel):
    index: int = Field(ge=0, le=2**53 - 1, strict=True)


class SetUsageAccount(Account):
    target_status: Literal['available', 'booked', 'used']
    token: str = Field(pattern=r'^[a-f0-9]{64}$')


class PresetEntry(StrictModel):
    pool_key: str = Field(min_length=1, max_length=100)
    resource: Literal['stars', 'ticket']
    pulls: int = Field(ge=1, le=1000, strict=True)
    until_owned: list[str] | None = Field(default=None, min_length=1, max_length=100)
    fallback_pool_key: str | None = Field(default=None, min_length=1, max_length=100)


class PresetUpdate(StrictModel):
    operation: Literal['update', 'create', 'delete'] = 'update'
    key: str | None = Field(default=None, pattern=r'^[a-z0-9-]{1,100}$')
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default='', max_length=500)
    entries: list[PresetEntry] = Field(default_factory=list, max_length=64)
    expected_version: int = Field(ge=0, strict=True)


def create_app(root=DATA_ROOT, service=None, *, extension=None, machine_credential=None):
    from ournotes.control.account_query import AccountQuery
    from ournotes.control.events import StateStream
    account_query = AccountQuery(root)
    launcher = service or Launcher(root)
    if extension is None:
        from ournotes.control.extensions import Extension
        extension = Extension(root)
    if extension and getattr(extension, 'rebind', None):
        launcher.notifications.rebind = extension.rebind.service.status
    token = secrets.token_urlsafe(32)

    def snapshot():
        value = launcher.status()
        value['notifications'] = launcher.notifications.status()
        value['cloud_sync'] = getattr(getattr(launcher, 'cloud_sync', None), 'status', {})
        if extension and hasattr(extension, 'status'):
            value.update(extension.status())
        elif extension and getattr(extension, 'rebind', None):
            value['email_rebind'] = extension.rebind.service.status()
        return value

    stream = StateStream(snapshot)

    @asynccontextmanager
    async def lifespan(app):
        from ournotes.integrations.cloud_sync import SyncWorker
        loop = asyncio.get_running_loop()
        def cloud_event(event):
            if event.get('type') in ('ready', 'presets.changed'):
                launcher._preset_checked_at = 0
            if not loop.is_closed() and not stream.closed:
                loop.call_soon_threadsafe(lambda: asyncio.create_task(stream.refresh()))
        sync = SyncWorker(root, event_observer=cloud_event)
        app.state.cloud_sync = launcher.cloud_sync = sync
        sync.start()
        threading.Thread(target=launcher.updates.check, daemon=True).start()
        launcher.notifications.start()
        await stream.start()
        try:
            yield
        finally:
            await stream.close()
            launcher.notifications.close()
            await asyncio.to_thread(launcher.game_update.close)
            if extension:
                await asyncio.to_thread(extension.close)
            sync.close()
            deadline, requested = time.monotonic() + 60, None
            while launcher.owned_running() and time.monotonic() < deadline:
                batch = launcher.runtime()["batch"]
                if batch and batch["status"] not in ("complete", "cancelled") and batch["id"] != requested:
                    launcher.execute("pause", {"command_id": secrets.token_hex(16), "batch_id": batch["id"]})
                    requested = batch["id"]
                await asyncio.sleep(0.2)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.launcher, app.state.token, app.state.shutdown = launcher, token, None
    app.state.stream = stream

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        host = request.headers.get("host", "")
        try:
            allowed = urlsplit("http://" + host).hostname in ("127.0.0.1", "localhost", "::1")
            origin = request.headers.get("origin")
            same_origin = origin is None or origin == f"http://{host}"
        except ValueError:
            allowed = same_origin = False
        if not allowed or not same_origin or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "此启动器仅允许本机同源访问。"}, status_code=403)
        machine_request = request.url.path.startswith('/agent/')
        if not machine_request and request.method not in ("GET", "HEAD") and not secrets.compare_digest(
            request.headers.get("x-launcher-token", ""), token
        ):
            return JSONResponse({"detail": "页面会话已失效，请刷新后重试。"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self'; style-src 'self'; "
            "script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({"detail": "参数无效，请检查输入值。"}, status_code=422)

    @app.exception_handler(ValueError)
    async def invalid_operation(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    from ournotes.integrations.cloud_client import CloudError

    @app.exception_handler(CloudError)
    async def cloud_failure(request, error):
        return JSONResponse({"detail": str(error), "reason": error.code}, status_code=409)

    @app.exception_handler(Exception)
    async def failed(request, error):
        # Do not return raw exceptions: OAuth errors can contain credentials.
        return JSONResponse(
            {"detail": f"操作失败（{type(error).__name__}），请检查本地配置。"}, status_code=500
        )

    @app.get("/")
    def home():
        html = (WEB / "index.html").read_text("utf-8").replace("__SESSION_TOKEN__", token)
        return HTMLResponse(extension.decorate(html) if extension else html)

    @app.get("/static/{name}")
    def static(name: str):
        if name not in ("app.js", "events.js", "account-actions.js", "presets.js", "account-query.js", "maintenance.js", "style.css"):
            raise HTTPException(404)
        return FileResponse(WEB / name)

    @app.get("/api/launcher/ping")
    def ping():
        return {"app": "ournotes-launcher", "version": 1}

    @app.get("/api/launcher/status")
    async def status():
        return await stream.refresh()

    @app.websocket('/api/launcher/events')
    async def events(socket: WebSocket):
        host = socket.headers.get('host', '')
        protocols = [part.strip() for part in socket.headers.get('sec-websocket-protocol', '').split(',')]
        credentials = [part.removeprefix('session.') for part in protocols if part.startswith('session.')]
        try:
            local = urlsplit('http://' + host).hostname in ('127.0.0.1', 'localhost', '::1')
        except ValueError:
            local = False
        if (not local or socket.headers.get('origin') not in (None, f'http://{host}')
                or 'ournotes' not in protocols or len(credentials) != 1
                or not secrets.compare_digest(credentials[0], token)):
            await socket.close(code=4403)
            return
        # Uvicorn logs the handshake URL. Keep the session credential in headers.
        await socket.accept(subprotocol='ournotes')
        value = stream.read()
        await socket.send_json({'type': 'snapshot', 'data': value})
        revision = value['stream_revision']
        receiver = asyncio.create_task(socket.receive())
        update = None
        try:
            while True:
                update = asyncio.create_task(stream.next(revision))
                done, _pending = await asyncio.wait((update, receiver), return_when=asyncio.FIRST_COMPLETED)
                if receiver in done:
                    update.cancel()
                    if receiver.result().get('type') == 'websocket.disconnect':
                        break
                    receiver = asyncio.create_task(socket.receive())
                    continue
                event = update.result()
                await socket.send_json(event)
                if event['type'] == 'snapshot':
                    revision = event['data']['stream_revision']
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            receiver.cancel()
            if update:
                update.cancel()

    @app.post('/api/launcher/refresh')
    async def refresh_state():
        return await stream.refresh()

    @app.get('/api/launcher/debug-report')
    def debug_report():
        import json
        import re
        import sqlite3
        from contextlib import closing

        database = root / 'state/runtime.sqlite3'
        if not database.is_file():
            raise HTTPException(404, '暂无调试批次。')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            batches = [json.loads(row[0]) for row in db.execute('SELECT data FROM batches ORDER BY created DESC')]
        batch = next((b for b in batches if b.get('debug_mode')), None)
        if not batch or not re.fullmatch('[a-f0-9]{32}', batch['id']):
            raise HTTPException(404, '暂无调试批次。')
        path = root / 'results/batches' / batch['id'] / '完整分步骤用时.md'
        from ournotes.runtime.storage import inside
        inside(root, path, root / 'results/batches')
        if not path.is_file():
            raise HTTPException(404, '调试步骤将在批次收尾后生成。')
        return FileResponse(path, media_type='text/plain; charset=utf-8')

    @app.get('/api/launcher/presets')
    def presets():
        from ournotes.control.presets import PresetEditor
        return PresetEditor(launcher).read()

    @app.post('/api/launcher/presets')
    def save_preset(value: PresetUpdate):
        from ournotes.control.presets import PresetEditor
        return PresetEditor(launcher).save(value.model_dump(exclude_none=True))

    @app.post("/api/launcher/action")
    def action(value: Action):
        if value.action == "start" and value.run is None:
            raise ValueError("请填写新批次配置。")
        receipt = launcher.execute(value.action, value.model_dump())
        return {**receipt.get("result", {}), "operation": receipt}

    @app.get("/api/launcher/operations/{identifier}")
    def operation(identifier: str):
        return launcher.operation(identifier)

    @app.get("/api/launcher/cloud")
    def cloud():
        from ournotes.integrations.cloud_client import CloudClientConfig
        saved = CloudClientConfig.load(root)
        return saved.public() if saved else {"configured": False, "service_url": ""}

    @app.get("/api/launcher/cloud/sync")
    def cloud_sync_status():
        return app.state.cloud_sync.status

    @app.get("/api/launcher/updates")
    def update_status():
        return launcher.updates.status()

    @app.post("/api/launcher/updates/check")
    def check_updates():
        return launcher.updates.check()

    @app.post("/api/launcher/updates/download")
    def download_update():
        return launcher.updates.download()

    @app.post("/api/launcher/updates/install")
    def install_update():
        with launcher.action_lock:
            if launcher.game_update.busy() or launcher.running() or any(job["status"] == "running" for job in launcher.jobs.listing()):
                raise ValueError("请先暂停运行并等待后台写入任务结束。")
            if app.state.shutdown is None:
                raise ValueError("当前启动方式不能完成更新器交接。")
            result = launcher.updates.start_install()
            app.state.shutdown()
            return result

    @app.post("/api/launcher/cloud")
    def save_cloud(value: CloudSettings):
        from ournotes.integrations.cloud_client import CloudClientConfig
        with launcher.action_lock:
            if launcher.running():
                raise ValueError("请先暂停运行再修改连接。")
            settings = CloudClientConfig.save(root, value.service_url, value.credential)
            launcher._preset_snapshot = None
            launcher._preset_checked_at = 0
            launcher.preset_error = ''
            return settings

    @app.get("/api/launcher/jobs/{identifier}")
    def job(identifier: str):
        return launcher.jobs.detail(identifier)

    @app.post("/api/launcher/folder")
    def folder(value: Folder):
        return launcher.open_folder(value.target)

    @app.post("/api/launcher/email")
    def email(value: Account):
        return account_query.email(value.index)

    @app.get('/api/launcher/accounts/{index}')
    def account(index: int = Path(ge=0, le=2**53 - 1)):
        return account_query.account(index)

    @app.post('/api/launcher/accounts/usage-preview')
    def usage_preview(value: Account):
        return launcher.account_set_usage(value.index)

    @app.post('/api/launcher/accounts/set-usage')
    def set_usage(value: SetUsageAccount):
        return launcher.account_set_usage(value.index, value.token, value.target_status)

    @app.post("/api/launcher/shutdown")
    def shutdown():
        if launcher.game_update.busy():
            raise ValueError('游戏更新正在执行，请等待结果后关闭控制台。')
        if app.state.shutdown is None:
            raise ValueError("当前启动方式不支持网页退出。")
        app.state.shutdown()
        return {"message": "启动器正在退出；本启动器运行中的游戏任务将请求安全暂停。"}

    @app.post('/api/launcher/game-update')
    def game_update(value: GameUpdateRequest):
        with launcher.action_lock:
            if launcher.owned_running():
                raise ValueError('请先安全暂停批次。')
            return launcher.game_update.start(value.instances, value.request_id)

    @app.get('/api/launcher/maintenance')
    def maintenance():
        return {'game_update': launcher.game_update.status(), 'notifications': launcher.notifications.status()}

    @app.post('/api/launcher/game-update/cancel')
    def cancel_game_update():
        return launcher.game_update.cancel()

    @app.post('/api/launcher/game-update/dismiss')
    def dismiss_game_update():
        return launcher.game_update.dismiss()

    @app.post('/api/launcher/notifications/settings')
    def notification_settings(value: NotificationSettings):
        return launcher.notifications.configure(value.enabled)

    @app.post('/api/launcher/notifications/test')
    def notification_test():
        return launcher.notifications.test()

    @app.get('/api/launcher/account-art/{kind}/{identity}')
    def account_art(kind: Literal['member', 'support'], identity: int = Path(ge=0)):
        from ournotes.integrations.cloud_client import CloudClient
        data, content_type = CloudClient.from_root(root).card_art(kind, identity)
        return Response(data, media_type=content_type)

    if extension:
        extension.mount(app)
    credential = machine_credential or os.environ.get('OURNOTES_AGENT_TOKEN', '')
    if credential:
        from ournotes.control.machine import create_app as create_machine
        machine = create_machine(launcher, credential)
        app.mount('/agent', machine)
    return app
