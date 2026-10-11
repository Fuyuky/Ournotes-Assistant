"""Authenticated cloud invalidations, with reconnect and bounded shutdown."""
from __future__ import annotations

import json
import threading

from ournotes.integrations.cloud_client import CloudClientConfig


class CloudEvents:
    def __init__(self, root, callback):
        self.root, self.callback = root, callback
        self.stop = threading.Event()
        self.thread = None
        self.connection = None
        self.state = 'disconnected'

    def start(self):
        self.thread = threading.Thread(target=self.run, name='cloud-events', daemon=True)
        self.thread.start()

    def run(self):
        try:
            from websockets.sync.client import connect
        except ImportError:
            self.state = 'dependency_missing'
            return

        delay = 1
        while not self.stop.is_set():
            try:
                config = CloudClientConfig.load(self.root)
                if config is None:
                    self.state = 'not_configured'
                    self.stop.wait(3)
                    continue
                url = config.service_url.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1)
                with connect(url + '/api/cloud/v1/events',
                             additional_headers={'Authorization': 'Bearer ' + config.credential},
                             open_timeout=5, close_timeout=1, max_size=16384, proxy=None) as connection:
                    self.connection = connection
                    self.state, delay = 'connected', 1
                    while not self.stop.is_set():
                        # Credential changes require a fresh connection, never an old authenticated socket.
                        if CloudClientConfig.load(self.root) != config:
                            break
                        try:
                            message = json.loads(connection.recv(timeout=1))
                        except TimeoutError:
                            continue
                        if (isinstance(message, dict) and message.get('type') in
                                {'ready', 'results.changed', 'presets.changed',
                                 'activities.changed', 'publication.changed'}):
                            self.callback({'type': message['type'], 'revision': message.get('revision')})
            except Exception:  # noqa: BLE001 -- background boundary must not expose credentials
                # Socket/credential errors contain no user-facing secret-bearing diagnostics.
                self.state = 'disconnected'
            finally:
                self.connection = None
            if self.stop.wait(delay):
                break
            delay = min(delay * 2, 30)

    def close(self):
        self.stop.set()
        if self.connection is not None:
            self.connection.close()
        if self.thread is not None:
            self.thread.join(timeout=7)
