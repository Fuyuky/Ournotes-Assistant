"""Only the development console mounts these local routes and assets."""
import time
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import FileResponse

from ournotes.proxy.ui.status import ConnectionStatus, rotation_status

ASSETS = Path(__file__).parent / 'static'


class Extension:
    def __init__(self, root):
        self.root = root
        self.connection = ConnectionStatus(root)

    def decorate(self, html):
        html = html.replace('</head>', '<link rel="stylesheet" href="/clash/static/style.css">'
                            '<script defer src="/clash/static/app.js"></script></head>', 1)
        summary = (ASSETS / 'summary.html').read_text('utf-8')
        html = html.replace('<section class="quick-section">', summary + '<section class="quick-section">', 1)
        needle = '<form id="run-form"'
        html = html.replace(needle, (ASSETS / 'view.html').read_text('utf-8') + needle, 1)
        needle = '<div class="two-column"><section class="panel"><div class="panel-head"><h2>MuMu 实例</h2>'
        return html.replace(needle, summary + needle, 1)

    def status(self, *, force=False):
        connection = self.connection.read(force=force)
        return {'connection': connection, 'rotation': rotation_status(self.root, connection),
                'observed_at': time.time()}

    def mount(self, app):
        @app.get('/clash/static/{name}')
        def static(name: str):
            if name not in ('app.js', 'style.css'):
                raise HTTPException(404)
            return FileResponse(ASSETS / name)

        @app.get('/api/launcher/clash/status')
        def status():
            return self.status()

        @app.post('/api/launcher/clash/check')
        def check():
            return self.status(force=True)

    def close(self):
        self.connection.close()
