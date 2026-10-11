"""Cloud-owned shared presets with one compare-and-swap version and immutable run snapshots."""
from __future__ import annotations

import hashlib
import json
import re

from ournotes.cloud.store import Conflict, encoded
from ournotes.runtime.presets import canonical_preset, read_presets


class Presets:
    def __init__(self, store, initial=None):
        self.store = store
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS shared_presets(key TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            if self.store.meta(db, 'presets_version') is None:
                document = initial if initial is not None else read_presets()
                for i, entry in enumerate(document['presets']):
                    key = entry.get('key') or 'preset-' + hashlib.sha256(entry['name'].encode()).hexdigest()[:16]
                    db.execute('INSERT INTO shared_presets VALUES(?,?)', (key, encoded(canonical_preset(entry))))
                self.store.set_meta(db, 'presets_version', 1)

    def snapshot(self):
        with self.store.connection() as db:
            db.execute('BEGIN')
            return {'version': int(self.store.meta(db, 'presets_version')),
                    'presets': {row['key']: json.loads(row['payload']) for row in db.execute(
                        'SELECT * FROM shared_presets ORDER BY key')}}

    def save(self, key, preset, expected_version):
        if not re.fullmatch(r'[a-z0-9-]{1,100}', key):
            raise ValueError('Invalid preset key')
        value = canonical_preset(preset)
        with self.store.transaction() as db:
            if int(self.store.meta(db, 'presets_version')) != expected_version:
                raise Conflict('Shared presets changed')
            if db.execute("SELECT 1 FROM shared_presets WHERE key!=? AND json_extract(payload,'$.name')=?",
                          (key, value['name'])).fetchone():
                raise Conflict('Preset name already exists')
            db.execute('INSERT INTO shared_presets VALUES(?,?) ON CONFLICT(key) '
                       'DO UPDATE SET payload=excluded.payload', (key, encoded(value)))
            self.store.set_meta(db, 'presets_version', expected_version + 1)
        return self.snapshot()

    def delete(self, key, expected_version):
        with self.store.transaction() as db:
            if int(self.store.meta(db, 'presets_version')) != expected_version:
                raise Conflict('Shared presets changed')
            if not db.execute('DELETE FROM shared_presets WHERE key=?', (key,)).rowcount:
                raise KeyError(key)
            self.store.set_meta(db, 'presets_version', expected_version + 1)
        return self.snapshot()
