"""Local account-rebind routes shared by source and installed clients."""
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from ournotes.rebind.service import Service

ASSETS = Path(__file__).parent / 'static'


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Eligibility(Model):
    index: int = Field(ge=0, le=2**53 - 1)


class Start(Model):
    index: int = Field(ge=0, le=2**53 - 1)
    instance: int = Field(ge=0, le=999)
    email: str = Field(min_length=3, max_length=254)
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class Action(Model):
    task_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    command_id: str = Field(pattern=r'^[a-f0-9]{32}$')


class TargetEmail(Action):
    email: str = Field(min_length=3, max_length=254)
    email_revision: int = Field(ge=1)


class Extension:
    def __init__(self, root, service=None, observer=None):
        self.service = service or Service(root, observer=observer)

    def decorate(self, html):
        html = html.replace('<div id="account-rebind-slot"></div>',
                            '<button id="account-rebind-toggle" class="button secondary">账号换绑</button>'
                            + (ASSETS / 'view.html').read_text('utf-8'), 1)
        return html.replace('</head>', '<link rel="stylesheet" href="/email-rebind/static/style.css">'
                            '<script defer src="/email-rebind/static/app.js"></script></head>', 1)

    def close(self):
        self.service.close()

    def mount(self, app):
        @app.get('/email-rebind/static/{name}')
        def static(name: str):
            if name not in {'app.js', 'style.css'}:
                raise HTTPException(404)
            return FileResponse(ASSETS / name)

        @app.post('/api/launcher/email-rebind/eligibility')
        def eligibility(body: Eligibility):
            from ournotes.accounts.usage import cloud_status as status
            from ournotes.accounts.used import require_protocol
            require_protocol(self.service.client_factory())
            item = self.service.client_factory().get_result_by_index(body.index)
            if item['result']['index'] != body.index:
                raise ValueError('账号编号不一致。')
            current = status(item)
            return {'index': body.index, 'used': item['result']['used'], 'usage_status': current,
                    'can_rebind': current != 'used'}

        @app.get('/api/launcher/email-rebind/status')
        def status():
            return self.service.status()

        @app.post('/api/launcher/email-rebind/start')
        def start(body: Start):
            return self.service.start(body.index, body.instance, body.email, body.request_id)

        @app.post('/api/launcher/email-rebind/confirmed')
        def confirmed(body: Action):
            return self.service.confirmed(body.task_id, body.command_id)

        @app.post('/api/launcher/email-rebind/target')
        def target(body: TargetEmail):
            return self.service.retry_target(body.task_id, body.command_id, body.email, body.email_revision)

        @app.post('/api/launcher/email-rebind/stop')
        def stop(body: Action):
            return self.service.stop(body.task_id)

        @app.post('/api/launcher/email-rebind/password')
        def password(body: Action):
            return self.service.secret(body.task_id)
