"""Authenticated multi-user browser administration with persistent 30-day sessions."""
from __future__ import annotations

import hashlib
import secrets
import statistics
import threading
from collections import Counter
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ournotes.accounts.index import AccountIndex
from ournotes.accounts.pricing import quote
from ournotes.cloud.administrators import SUPERADMIN, Administrators, password_record
from ournotes.cloud.models import Usage
from ournotes.cloud.services import ActiveUpdate, MailExecute, PresetUpdate
from ournotes.cloud.store import Conflict, Forbidden, Unavailable
from ournotes.paths import ROOT

SESSION_SECONDS = 30 * 24 * 60 * 60
COOKIE = 'ournotes_admin_session'
METRICS = {'ssr_total': 'SSR 总张数（角色卡＋支援卡）',
           'mygo_characters': '不同 MyGO SSR 角色数',
           'mujica_characters': '不同 Mujica SSR 角色数',
           'unique_characters': '不同 SSR 角色数'}
WEB = ROOT / 'web/admin'
__all__ = ['password_record']


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class Announcement(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(max_length=5000)
    expected_version: str = Field(pattern=r'^[a-f0-9]{64}$')


class AdministratorCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,79}$')
    password: str = Field(min_length=8, max_length=256)


def mount(app, store, credential, catalog, publisher=None, *, presets=None, activities=None,
          events=None, manual_mail=None):
    directory = Administrators(store, credential)
    app.state.administrators = directory
    index = AccountIndex(Path(catalog) / 'unused-admin-source', catalog)
    attempts, lock = {}, threading.Lock()

    @app.middleware('http')
    async def headers(request, call_next):
        response = await call_next(request)
        if request.url.path == '/admin' or request.url.path.startswith('/admin/'):
            response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy':
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    def same_origin(request):
        origin = request.headers.get('origin')
        if (request.headers.get('sec-fetch-site') == 'cross-site' or
                (origin and (urlsplit(origin).netloc != request.headers.get('host') or
                             urlsplit(origin).scheme != 'https'))):
            raise HTTPException(403, '请求来源无效。')

    def authenticated(request: Request):
        token = request.cookies.get(COOKIE, '')
        try:
            return directory.session(token)
        except Forbidden:
            raise HTTPException(401, '请先登录。')

    authenticated_dependency = Depends(authenticated)

    @app.get('/admin')
    @app.get('/admin/')
    def home():
        return FileResponse(WEB / 'index.html')

    @app.get('/admin/assets/{name}')
    def asset(name: Literal['app.js', 'style.css']):
        return FileResponse(WEB / name)

    @app.post('/admin/api/login')
    def login(body: Login, request: Request):
        same_origin(request)
        address = request.client.host
        now = store.clock()
        with lock:
            failures = attempts.setdefault(address, [])
            failures[:] = [t for t in failures if now - t < 900]
            if len(failures) >= 10:
                raise HTTPException(429, '登录尝试过多，请稍后重试。')
            failures.append(now)
        try:
            fingerprint = directory.authenticate_password(body.username, body.password)
        except Forbidden:
            raise HTTPException(401, '账号或密码错误。')
        with lock:
            attempts.pop(address, None)
        token = secrets.token_hex(32)
        with store.transaction() as db:
            db.execute('DELETE FROM portal_sessions WHERE expires<=?', (now,))
            db.execute('INSERT INTO portal_sessions VALUES(?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), body.username, now + SESSION_SECONDS, fingerprint))
        response = JSONResponse({'username': body.username, 'is_superadmin': body.username == SUPERADMIN,
                                 'expires_at': now + SESSION_SECONDS})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, secure=True,
                            samesite='strict', path='/admin')
        return response

    @app.get('/admin/api/session')
    def session(username=authenticated_dependency):
        return {'username': username, 'is_superadmin': username == SUPERADMIN}

    @app.get('/admin/api/administrators')
    def administrators(username=authenticated_dependency):
        return directory.list()

    @app.post('/admin/api/administrators')
    def create_administrator(body: AdministratorCreate, request: Request, username=authenticated_dependency):
        same_origin(request)
        return directory.create(username, body.username, body.password)

    @app.delete('/admin/api/administrators/{target}')
    def delete_administrator(target: str, request: Request, username=authenticated_dependency):
        same_origin(request)
        return directory.delete(username, target)

    @app.get('/admin/api/credential')
    def credential(username=authenticated_dependency):
        return directory.credential(username)

    @app.post('/admin/api/credential/reset')
    def reset_credential(request: Request, username=authenticated_dependency):
        same_origin(request)
        return directory.reset_credential(username)

    @app.get('/admin/api/gmail/health')
    def gmail_health(username=authenticated_dependency):
        return manual_mail.health()

    @app.get('/admin/api/accounts')
    def accounts(index_no: int | None = Query(default=None, alias='index', ge=0, le=2**53 - 1),
                 owner_username: str | None = Query(default=None, max_length=80),
                 offset: int = Query(default=0, ge=0), limit: int = Query(default=50, ge=1, le=100),
                 username=authenticated_dependency):
        source = store.results(owner_username=owner_username)
        values = source['results']
        if index_no is not None:
            values = [item for item in values if item['result']['index'] == index_no]
        total = len(values)
        selected = []
        for envelope in values[offset:offset + limit]:
            value = envelope['result']
            projection = index.account({**value, 'status': 'complete', 'alias_index': value['index']})
            selected.append({**envelope, 'projection': {**projection, 'price': quote(projection)},
                             'ssr_cards': [{**index.cards[c['key']], 'count': c['count']}
                                           for c in projection['cards']]})
        return {'revision': source['revision'], 'total': total, 'offset': offset, 'accounts': selected}

    @app.get('/admin/api/art/{kind}/{identifier}')
    def art(kind: Literal['member', 'support'], identifier: int, username=authenticated_dependency):
        path = index.images.get(f'{kind}:{identifier}')
        if path is None:
            raise KeyError(identifier)
        return FileResponse(path)

    @app.post('/admin/api/accounts/{index_no}/usage')
    def usage(index_no: int, body: Usage, request: Request, username=authenticated_dependency):
        same_origin(request)
        account_id = store.result_by_index(index_no)['result']['account_id']
        value = store.usage_admin(username, account_id, body.model_dump())
        if publisher:
            try:
                value['publication'] = publisher.publish_pending()
            except (OSError, ValueError):
                value['publication'] = {'pending': True}
        return value

    @app.get('/admin/api/mail')
    def mail(index: int = Query(ge=0, le=2**53 - 1), minutes: int = Query(default=10, ge=10, le=1440),
             username=authenticated_dependency):
        return manual_mail.read(index, minutes)

    @app.post('/admin/api/mail/{index_no}/execute')
    def execute_mail(index_no: int, body: MailExecute, request: Request, username=authenticated_dependency):
        same_origin(request)
        return manual_mail.execute(username, index_no, body.message_id, body.request_id)

    @app.get('/admin/api/presets')
    def shared_presets(username=authenticated_dependency):
        return presets.snapshot()

    @app.put('/admin/api/presets/{key}')
    def save_preset(key: str, body: PresetUpdate, request: Request, username=authenticated_dependency):
        same_origin(request)
        value = presets.save(key, body.preset, body.expected_version)
        events.notify('presets.changed', value['version'])
        return value

    @app.delete('/admin/api/presets/{key}')
    def delete_preset(key: str, expected_version: int, request: Request, username=authenticated_dependency):
        same_origin(request)
        value = presets.delete(key, expected_version)
        events.notify('presets.changed', value['version'])
        return value

    @app.get('/admin/api/activities')
    def read_activities(username=authenticated_dependency):
        return activities.snapshot()

    @app.post('/admin/api/activities/active')
    def activate(body: ActiveUpdate, request: Request, username=authenticated_dependency):
        same_origin(request)
        value = activities.activate(body.activity_id, body.expected_version)
        events.notify('activities.changed', value['version'])
        return value

    @app.websocket('/admin/api/events')
    async def stream(websocket: WebSocket):
        try:
            same_origin(websocket)
        except HTTPException:
            await websocket.close(code=4403)
            return
        await events.stream(websocket, lambda: directory.session(websocket.cookies.get(COOKIE, '')))

    @app.get('/admin/api/announcement')
    def announcement(username=authenticated_dependency):
        if publisher is None:
            raise HTTPException(503, '发布服务未配置。')
        return publisher.announcement()

    @app.post('/admin/api/announcement')
    def save_announcement(body: Announcement, request: Request, username=authenticated_dependency):
        same_origin(request)
        if publisher is None:
            raise HTTPException(503, '发布服务未配置。')
        value = publisher.save_announcement(body.content, body.expected_version)
        try:
            publication = publisher.publish_pending()
        except (OSError, ValueError, Conflict, Unavailable):
            publication = {'pending': True}
        return {**value, 'publication': publication,
                'message': '公告已保存，网站更新正在自动重试。' if publication['pending'] else '公告已保存并更新网站。'}

    @app.post('/admin/api/logout')
    def logout(request: Request, username=authenticated_dependency):
        same_origin(request)
        with store.transaction() as db:
            db.execute('DELETE FROM portal_sessions WHERE token_hash=?',
                       (hashlib.sha256(request.cookies[COOKIE].encode()).hexdigest(),))
        response = JSONResponse({'logged_out': True})
        response.delete_cookie(COOKIE, path='/admin', secure=True, httponly=True, samesite='strict')
        return response

    @app.get('/admin/api/email')
    def email(index: int = Query(ge=0, le=2**53 - 1), username=authenticated_dependency):
        value = store.result_by_index(index)['result']
        return {'index': index, 'email': value['email']}

    @app.get('/admin/api/statistics')
    def report(scope: Literal['all', 'used'] = 'all', owner_username: str | None = Query(default=None, max_length=80),
               username=authenticated_dependency):
        source = store.results(owner_username=owner_username)
        rows, excluded = [], 0
        for item in source['results']:
            value = item['result']
            if scope == 'used' and not value['used']:
                continue
            try:
                rows.append(index.account({**value, 'status': 'complete', 'alias_index': value['index']}))
            except (ValueError, KeyError, TypeError):
                excluded += 1
        metrics = []
        for key, label in METRICS.items():
            values = [r[key] for r in rows]
            counts = Counter(values)
            high = max(values) if values else None
            metrics.append({'key': key, 'label': label, 'max': high,
                'indices': sorted(r['index'] for r in rows if r[key] == high),
                'mean': statistics.mean(values) if values else None,
                'histogram': [{'value': n, 'count': counts[n], 'percent': counts[n] / len(rows) * 100}
                              for n in range(high + 1)] if values else []})
        return {'scope': scope, 'n': len(rows), 'excluded': excluded, 'revision': source['revision'],
                'observed_at': store.clock(), 'metrics': metrics}
