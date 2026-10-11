"""Bearer-authenticated automation API on the same loopback console."""
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from ournotes.control.web import Action


class MachineAction(Action):
    command_id: str = Field(pattern=r'^[A-Za-z0-9_-]{8,128}$')
    expected_version: str = Field(pattern=r'^[a-f0-9]{64}$')


def create_app(service, credential):
    if len(credential) < 32:
        raise ValueError('机器接口需要至少32字符的会话凭据。')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def authenticate(request: Request, call_next):
        if not secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + credential):
            return JSONResponse({'reason': 'unauthorized'}, status_code=401)
        return await call_next(request)

    @app.exception_handler(ValueError)
    async def conflict(request, error):
        return JSONResponse({'reason': 'conflict', 'detail': str(error)}, status_code=409)

    @app.get('/api/v1/capabilities')
    def capabilities():
        return {'protocol_version': 1, 'commands': ['start', 'resume', 'resume_lane', 'pause', 'finish', 'cancel'],
                'running': service.running()}

    @app.get('/api/v1/status')
    def status():
        return service.status()

    @app.get('/api/v1/operations/{command_id}')
    def operation(command_id: str):
        return service.operation(command_id)

    @app.get('/api/v1/observe')
    def observe(instance: int):
        runtime = service.runtime()
        lane = next((lane for lane in runtime['lanes'] if lane['instance'] == instance), None)
        if lane is None:
            raise ValueError('当前批次没有此实例。')
        return {'observed_at': runtime['observed_at'], 'version': runtime['version'],
                'batch_id': runtime['batch']['id'], 'lane': lane, 'source': 'controller_snapshot'}

    @app.post('/api/v1/commands')
    def command(value: MachineAction):
        if value.action not in ('start', 'resume', 'resume_lane', 'pause', 'finish', 'cancel'):
            raise HTTPException(422, detail='unsupported_command')
        if value.action == 'start' and value.run is None:
            raise HTTPException(422, detail='run_required')
        return service.execute(value.action, value.model_dump())

    return app
