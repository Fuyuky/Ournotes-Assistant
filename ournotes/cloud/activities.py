"""Offline masterdata import and explicit activity assignment, pinned at allocation time."""
from __future__ import annotations

import json
from pathlib import Path

from ournotes.cloud.models import ActivitySnapshot
from ournotes.cloud.store import Conflict, encoded


class Activities:
    def __init__(self, store, catalog=None):
        self.store = store
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS activities(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO metadata VALUES('activities_version','0')")
            db.execute("INSERT OR IGNORE INTO metadata VALUES('active_activity_id','')")
        if catalog is not None:
            self.import_catalog(catalog)
        self.backfill_history()

    def import_catalog(self, catalog):
        """Use only the supplied verified local catalog; this operation never downloads anything."""
        catalog = Path(catalog)
        if (not (catalog / 'pools.json').is_file() and not (catalog / 'raw/MasterGacha.json').is_file()
                and (catalog.parent / 'current.json').is_file()):
            catalog = catalog.parent
        if (catalog / 'current.json').is_file():
            from ournotes.catalogs.runtime import current_catalog
            catalog = current_catalog(catalog)
        if (catalog / 'pools.json').is_file():
            pools = json.loads((catalog / 'pools.json').read_text('utf-8'))['pools']
        elif (catalog / 'raw/MasterGacha.json').is_file():
            from ournotes.catalogs.cards import TABLES, _rows
            from ournotes.catalogs.runtime import EXTRA_TABLES, compile_pools
            tables = {name: _rows(catalog / 'raw' / f'{name}.json') for name in TABLES + EXTRA_TABLES}
            pools = compile_pools(tables, json.loads((catalog / 'catalog.json').read_text('utf-8'))['cards'])
        else:
            return False
        source = catalog / 'raw/source.json'
        if source.is_file() and json.loads(source.read_text('utf-8')).get('region') != 'hk-tw-mo':
            raise ValueError('Activities require hk-tw-mo masterdata')
        snapshots = []
        for pool in pools:
            snapshot = {'id': pool['key'], 'pool_key': pool['key'], 'gacha_id': pool['gacha_id'],
                        'name': pool['name'], 'label': {10: '梦限大 一期', 12: 'millsage 一期'}.get(
                            pool['gacha_id'], pool['name']),
                        'pickup_card_keys': sorted(set(pool.get('pickup_card_keys', ())))}
            snapshots.append(ActivitySnapshot.model_validate(snapshot).model_dump())
        changed = False
        with self.store.transaction() as db:
            for value in snapshots:
                prior = db.execute('SELECT payload FROM activities WHERE id=?', (value['id'],)).fetchone()
                content = encoded(value)
                if not prior or prior['payload'] != content:
                    db.execute('INSERT INTO activities VALUES(?,?) ON CONFLICT(id) '
                               'DO UPDATE SET payload=excluded.payload', (value['id'], content))
                    changed = True
            if changed:
                self.store.set_meta(db, 'activities_version', int(self.store.meta(db, 'activities_version')) + 1)
        return changed

    def snapshot(self):
        with self.store.connection() as db:
            return {'version': int(self.store.meta(db, 'activities_version')),
                    'active_activity_id': self.store.meta(db, 'active_activity_id') or None,
                    'activities': [json.loads(row['payload']) for row in db.execute('SELECT * FROM activities ORDER BY id')]}

    def activate(self, activity_id, expected_version):
        with self.store.transaction() as db:
            if int(self.store.meta(db, 'activities_version')) != expected_version:
                raise Conflict('Activity configuration changed')
            if activity_id is not None and not db.execute('SELECT 1 FROM activities WHERE id=?',
                                                         (activity_id,)).fetchone():
                raise KeyError(activity_id)
            self.store.set_meta(db, 'active_activity_id', activity_id or '')
            self.store.set_meta(db, 'activities_version', expected_version + 1)
        return self.snapshot()

    def backfill_history(self):
        from ournotes.accounts.quality import historical_activity
        with self.store.transaction() as db:
            changed = False
            for row in db.execute('SELECT r.*,a.activity_payload FROM results r JOIN allocations a '
                                  'ON a.account_id=r.account_id WHERE a.activity_payload IS NULL').fetchall():
                if not self.store.final(row):
                    continue
                value = json.loads(row['payload'])
                activity = historical_activity(value.get('pool_keys', ()))
                if activity is None:
                    continue
                stored = db.execute('SELECT payload FROM activities WHERE id=?', (activity['id'],)).fetchone()
                if stored:
                    activity = json.loads(stored['payload'])
                value.update(activity_id=activity['id'], activity=activity)
                db.execute('UPDATE allocations SET activity_payload=? WHERE account_id=?',
                           (encoded(activity), row['account_id']))
                db.execute('UPDATE results SET payload=? WHERE account_id=?', (encoded(value), row['account_id']))
                changed = True
            if changed:
                self.store.bump_revision(db)
