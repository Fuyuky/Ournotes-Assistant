"""Cloud-owned presets with monotonic numeric identities and versioned snapshots."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ournotes.cloud.store import Conflict, encoded
from ournotes.runtime.presets import canonical_preset, read_presets


def migrate_preset(value):
    """Remove the retired policy from editable configuration, never from saved runs."""
    value = dict(value)
    had_policy = any('until_owned' in e or 'fallback_pool_key' in e for e in value['entries'])
    value['entries'] = [{k: v for k, v in e.items() if k not in ('until_owned', 'fallback_pool_key')}
                        for e in value['entries']]
    if had_policy:
        value['name'] = value['name'].replace('UP转常驻', 'UP固定招募').replace('集齐转池', '固定招募')
        value['name'] = re.sub(r'\d+\s*抽$', str(sum(e['pulls'] for e in value['entries'])) + ' 抽', value['name'])
        value['name'] = value['name'][:80]
        value['description'] = '按项目顺序和固定抽数招募；资源不足时跳过，不购买资源。'
    return canonical_preset(value)


def migrated_values(entries):
    """Disambiguate editable names without changing any historical lookup names."""
    seen = set()
    for identifier, entry in enumerate(entries):
        value = migrate_preset(entry)
        name = value['name']
        if name in seen:
            suffix = f' (#{identifier})'
            name = name[:80 - len(suffix)] + suffix
            while name in seen:
                suffix = '_' + suffix
                name = value['name'][:80 - len(suffix)] + suffix
            value['name'] = name
        seen.add(name)
        yield value


def write_aliases(db, aliases):
    candidates = {}
    for key, identifier in aliases:
        candidates.setdefault(key, set()).add(identifier)
    # A legacy key and a name shared one untyped snapshot field. Ambiguous text
    # cannot be assigned reliably, so it remains in the historical unknown group.
    for key, identifiers in candidates.items():
        if len(identifiers) == 1:
            db.execute('INSERT INTO preset_aliases VALUES(?,?)', (key, next(iter(identifiers))))


def pool_options(catalog):
    if catalog is None:
        return []
    catalog = Path(catalog)
    if (catalog / 'current.json').is_file():
        from ournotes.catalogs.runtime import current_catalog
        catalog = current_catalog(catalog)
    path = catalog / 'pools.json'
    if path.is_file():
        values = json.loads(path.read_text('utf-8'))['pools']
    elif (catalog / 'raw/MasterGacha.json').is_file():
        from ournotes.catalogs.cards import TABLES, _rows
        from ournotes.catalogs.runtime import EXTRA_TABLES, compile_pools
        tables = {name: _rows(catalog / 'raw' / f'{name}.json') for name in TABLES + EXTRA_TABLES}
        values = compile_pools(tables, json.loads((catalog / 'catalog.json').read_text('utf-8'))['cards'])
    else:
        return []
    result = []
    for p in values:
        resources = ['stars'] if any(v['itemType'] == 12 for v in p['products']) else []
        if p.get('ticket_name'):
            resources.append('ticket')
        result.append({**{k: p.get(k) for k in ('key', 'name', 'gacha_id', 'ticket_name')},
                       'resources': resources, 'supported': not p.get('unsupported_card_keys')})
    return result


class Presets:
    def __init__(self, store, initial=None, *, catalog=None):
        self.store = store
        self.pools = pool_options(catalog)
        with store.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS preset_aliases(key TEXT PRIMARY KEY,preset_id INTEGER NOT NULL)')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(shared_presets)')}
            migrating = 'key' in columns
            rows = list(db.execute('SELECT key,payload FROM shared_presets ORDER BY rowid')) if migrating else []
            if migrating:
                db.execute('ALTER TABLE shared_presets RENAME TO shared_presets_legacy')
            db.execute('CREATE TABLE IF NOT EXISTS shared_presets(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)')
            if migrating:
                entries = [json.loads(row['payload']) for row in rows]
                aliases = []
                for identifier, (row, value) in enumerate(zip(rows, migrated_values(entries), strict=True)):
                    db.execute('INSERT INTO shared_presets VALUES(?,?)', (identifier, encoded(value)))
                    aliases.extend(((row['key'], identifier), (entries[identifier]['name'], identifier)))
                write_aliases(db, aliases)
                db.execute('DROP TABLE shared_presets_legacy')
                store.set_meta(db, 'presets_next_id', len(rows))
                version = int(store.meta(db, 'presets_version') or 0) + 1
                store.set_meta(db, 'presets_version', version)
                store.set_meta(db, 'presets_numeric_version', version)
            elif store.meta(db, 'presets_version') is None:
                document = initial if initial is not None else read_presets()
                aliases = []
                for identifier, (entry, value) in enumerate(zip(document['presets'], migrated_values(document['presets']), strict=True)):
                    db.execute('INSERT INTO shared_presets VALUES(?,?)', (identifier, encoded(value)))
                    old = entry.get('key') or 'preset-' + hashlib.sha256(entry['name'].encode()).hexdigest()[:16]
                    aliases.extend(((old, identifier), (entry['name'], identifier)))
                write_aliases(db, aliases)
                store.set_meta(db, 'presets_next_id', len(document['presets']))
                store.set_meta(db, 'presets_version', 1)
                store.set_meta(db, 'presets_numeric_version', 1)
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS shared_presets_name ON shared_presets(json_extract(payload,'$.name'))")
            if store.meta(db, 'presets_next_id') is None:
                store.set_meta(db, 'presets_next_id', db.execute('SELECT COALESCE(MAX(id)+1,0) FROM shared_presets').fetchone()[0])

    def snapshot(self):
        with self.store.connection() as db:
            db.execute('BEGIN')
            return {'version': int(self.store.meta(db, 'presets_version')), 'pools': self.pools,
                    'presets': {str(row['id']): json.loads(row['payload']) for row in db.execute(
                        'SELECT * FROM shared_presets ORDER BY id')}}

    def _version(self, db, expected):
        if int(self.store.meta(db, 'presets_version')) != expected:
            raise Conflict('Shared presets changed')

    @staticmethod
    def _identifier(key):
        if not isinstance(key, str) or not re.fullmatch(r'0|[1-9][0-9]{0,15}', key):
            raise ValueError('Invalid numeric preset identifier')
        return int(key)

    def _validate(self, preset):
        value = canonical_preset(preset)
        if self.pools:
            for e in value['entries']:
                pool = next((p for p in self.pools if e['pool_key'] in (p['key'], f"bdon-gacha-{p['gacha_id']}")), None)
                if not pool or not pool['supported'] or e['resource'] not in pool['resources']:
                    raise ValueError('卡池或资源不受支持，请刷新卡池目录。')
        return value

    def create(self, preset, expected_version):
        value = self._validate(preset)
        with self.store.transaction() as db:
            self._version(db, expected_version)
            if db.execute("SELECT 1 FROM shared_presets WHERE json_extract(payload,'$.name')=?", (value['name'],)).fetchone():
                raise Conflict('Preset name already exists')
            identifier = int(self.store.meta(db, 'presets_next_id'))
            db.execute('INSERT INTO shared_presets VALUES(?,?)', (identifier, encoded(value)))
            self.store.set_meta(db, 'presets_next_id', identifier + 1)
            self.store.set_meta(db, 'presets_version', expected_version + 1)
        return {**self.snapshot(), 'created_id': identifier}

    def save(self, key, preset, expected_version):
        identifier = self._identifier(key)
        value = self._validate(preset)
        with self.store.transaction() as db:
            self._version(db, expected_version)
            if db.execute("SELECT 1 FROM shared_presets WHERE id!=? AND json_extract(payload,'$.name')=?",
                          (identifier, value['name'])).fetchone():
                raise Conflict('Preset name already exists')
            if not db.execute('UPDATE shared_presets SET payload=? WHERE id=?', (encoded(value), identifier)).rowcount:
                raise KeyError(key)
            self.store.set_meta(db, 'presets_version', expected_version + 1)
        return self.snapshot()

    def delete(self, key, expected_version):
        identifier = self._identifier(key)
        with self.store.transaction() as db:
            self._version(db, expected_version)
            if not db.execute('DELETE FROM shared_presets WHERE id=?', (identifier,)).rowcount:
                raise KeyError(key)
            self.store.set_meta(db, 'presets_version', expected_version + 1)
        return self.snapshot()
