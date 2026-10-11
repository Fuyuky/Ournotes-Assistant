"""Authenticated websocket invalidations; payloads deliberately contain no private identities."""
from __future__ import annotations

import asyncio

from fastapi import WebSocket, WebSocketDisconnect

from ournotes.cloud.store import Forbidden

TYPES = {'results.changed', 'presets.changed', 'activities.changed', 'publication.changed'}


class Events:
    def __init__(self, store):
        self.store = store
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS cloud_events('
                       'id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT NOT NULL,revision INTEGER NOT NULL)')

    @staticmethod
    def emit(db, event_type, revision):
        if event_type not in TYPES:
            raise ValueError('Invalid event type')
        db.execute('INSERT INTO cloud_events(type,revision) VALUES(?,?)', (event_type, revision))
        db.execute('DELETE FROM cloud_events WHERE id < (SELECT MAX(id)-1000 FROM cloud_events)')

    def notify(self, event_type, revision):
        with self.store.transaction() as db:
            self.emit(db, event_type, revision)

    async def stream(self, websocket: WebSocket, authenticate):
        try:
            authenticate()
            cursor = int(websocket.query_params.get('since', 0))
            if cursor < 0:
                raise ValueError
        except (Forbidden, ValueError):
            await websocket.close(code=4403)
            return
        await websocket.accept()
        try:
            with self.store.connection() as db:
                revision = int(self.store.meta(db, 'revision'))
                high = db.execute('SELECT MAX(id) FROM cloud_events').fetchone()[0] or 0
            # A new subscriber receives a snapshot invalidation and then only new events.
            if not cursor:
                cursor = high
            await websocket.send_json({'id': cursor, 'type': 'ready', 'revision': revision})
            while True:
                authenticate()  # Rotation and deletion invalidate already-open connections.
                with self.store.connection() as db:
                    rows = [dict(row) for row in db.execute('SELECT id,type,revision FROM cloud_events '
                                                           'WHERE id>? ORDER BY id LIMIT 100', (cursor,))]
                for value in rows:
                    await websocket.send_json(value)
                    cursor = value['id']
                try:
                    await asyncio.wait_for(websocket.receive_text(), timeout=1)
                except TimeoutError:
                    continue
        except Forbidden:
            await websocket.close(code=4403)
        except WebSocketDisconnect:
            return
