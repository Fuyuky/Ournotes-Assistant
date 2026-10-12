"""Authenticated client-facing business queries and shared configuration routes."""
from __future__ import annotations

from typing import Literal

from fastapi import Path, Query, WebSocket
from fastapi.responses import FileResponse
from pydantic import Field, StrictInt

from ournotes.cloud.models import Model
from ournotes.cloud.store import Forbidden, Unavailable

PREFIX = '/api/cloud/v1'


class PresetUpdate(Model):
    expected_version: StrictInt = Field(ge=0)
    preset: dict


class ActiveUpdate(Model):
    expected_version: StrictInt = Field(ge=0)
    activity_id: str | None = Field(max_length=100)


class MailExecute(Model):
    message_id: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)


def mount(app, store, identity_dependency, *, validator, presets, activities, events, manual_mail):
    @app.get(PREFIX + '/accounts')
    def accounts(index: int | None = Query(default=None, ge=0, le=2**53 - 1),
                 owner_username: str | None = Query(default=None, max_length=80), identity=identity_dependency):
        source = store.results(owner_username=owner_username, index_no=index)
        values = source['results']
        return {'revision': source['revision'], 'accounts': values}

    @app.get(PREFIX + '/accounts/{account_id}')
    def account(account_id: str, identity=identity_dependency):
        return store.results(account_id)

    @app.get(PREFIX + '/art/{kind}/{identifier}')
    def art(kind: Literal['member', 'support'], identifier: int = Path(gt=0), identity=identity_dependency):
        if validator is None or f'{kind}:{identifier}' not in validator.images:
            raise KeyError(identifier)
        image = validator.images[f'{kind}:{identifier}']
        if image.stat().st_size > 5 * 1024 * 1024:
            raise Unavailable('Card image is too large')
        media_type = {'.png': 'image/png', '.jpg': 'image/jpeg',
                      '.jpeg': 'image/jpeg', '.webp': 'image/webp'}[image.suffix.lower()]
        return FileResponse(image, media_type=media_type, headers={'Cache-Control': 'no-store'})

    @app.get(PREFIX + '/presets')
    def read_presets(identity=identity_dependency):
        return presets.snapshot()

    @app.post(PREFIX + '/presets')
    def create_preset(body: PresetUpdate, identity=identity_dependency):
        value = presets.create(body.preset, body.expected_version)
        events.notify('presets.changed', value['version'])
        return value

    @app.put(PREFIX + '/presets/{key}')
    def save_preset(key: str, body: PresetUpdate, identity=identity_dependency):
        value = presets.save(key, body.preset, body.expected_version)
        events.notify('presets.changed', value['version'])
        return value

    @app.delete(PREFIX + '/presets/{key}')
    def delete_preset(key: str, expected_version: int = Query(ge=0), identity=identity_dependency):
        value = presets.delete(key, expected_version)
        events.notify('presets.changed', value['version'])
        return value

    @app.get(PREFIX + '/activities')
    def read_activities(identity=identity_dependency):
        return activities.snapshot()

    @app.post(PREFIX + '/activities/active')
    def active(body: ActiveUpdate, identity=identity_dependency):
        value = activities.activate(body.activity_id, body.expected_version)
        events.notify('activities.changed', value['version'])
        return value

    @app.get(PREFIX + '/gmail/health')
    def gmail_health(identity=identity_dependency):
        return manual_mail.health()

    @app.websocket(PREFIX + '/events')
    async def stream(websocket: WebSocket):
        authorization = websocket.headers.get('authorization', '')

        def authenticate():
            if not authorization.startswith('Bearer ') or len(authorization) > 512:
                raise Forbidden('Credential required')
            return store.authenticate(authorization[7:])

        await events.stream(websocket, authenticate)
