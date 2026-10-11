"""Explicit publication of a whole cloud revision, with immutable content and atomic pointer."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path

from ournotes.accounts.site import WEB, digest, export_site, public_path
from ournotes.accounts.site import encoded as public_encoded
from ournotes.cloud.durability import sync_directory, sync_version
from ournotes.cloud.store import Conflict, Unavailable, encoded


class Publisher:
    def announcement(self):
        from ournotes.accounts.announcement import DEFAULT_ANNOUNCEMENT
        path = self.root / 'announcement.json'
        with self.store.connection() as db:
            content = self.store.meta(db, 'website_announcement')
        if content is None:
            content = json.loads(path.read_text('utf-8'))['content'] if path.exists() else DEFAULT_ANNOUNCEMENT
        return {'content': content, 'version': digest(content.encode('utf-8'))}

    def save_announcement(self, content, expected_version):
        with self.lock:
            with self.store.transaction() as db:
                # The announcement and desired publication revision commit together.
                # Keep the legacy file as a read-only fallback until the first edit.
                saved = self.store.meta(db, 'website_announcement')
                current = self.announcement() if saved is None else {
                    'version': digest(saved.encode('utf-8'))}
                if current['version'] != expected_version:
                    raise Conflict('Announcement changed; reload before saving')
                self.store.set_meta(db, 'website_announcement', content)
                self.store.bump_revision(db)
            return self.announcement()

    def __init__(self, store, root, catalog):
        self.store, self.root, self.catalog = store, Path(root).resolve(), Path(catalog).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.pointer = self.root / "published.json"
        self.lock = threading.RLock()
        self.recover_receipt()
        with self.store.transaction() as db:
            revision = int(self.store.meta(db, 'revision'))
            if revision and (self.current() is None or self.status(revision)['pending']):
                self.store.set_meta(db, 'publication_pending_revision', revision)

    def publish_pending(self):
        """Retry the durable desired revision; concurrent uploads leave a newer marker intact."""
        with self.lock:
            with self.store.connection() as db:
                desired = self.store.meta(db, 'publication_pending_revision')
                revision = int(self.store.meta(db, 'revision'))
            if desired is None:
                return self.status(revision)
            signature = self.build_signature()
            request = {'request_id': 'automatic-' + digest(public_encoded([revision, signature])),
                       'expected_revision': revision, 'limit': None}
            response = self.publish('system-publication', request, automatic=True)
            with self.store.transaction() as db:
                if int(self.store.meta(db, 'revision')) == revision:
                    db.execute("DELETE FROM metadata WHERE key='publication_pending_revision'")
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='cloud_events'").fetchone():
                    from ournotes.cloud.events import Events
                    Events.emit(db, 'publication.changed', revision)
            return {'pending': False, **response}

    def recover_receipt(self):
        current = self.current()
        if current and not current.get('usage_only'):
            with self.store.transaction() as db:
                prior = self.store.receipt(db, current["node"], "publish", current["request"]["request_id"],
                                           current["request"])
                if prior is None:
                    self.store.save_receipt(db, current["node"], "publish", current["request"]["request_id"],
                                            current["request"], current["response"])

    def current(self):
        if not self.pointer.exists():
            return None
        value = json.loads(self.pointer.read_text("utf-8"))
        if not isinstance(value.get("directory"), str) or not value["directory"].isalnum():
            raise Unavailable("Publication pointer invalid")
        return value

    def status(self, revision):
        current = self.current()
        return {"revision": current["response"]["revision"] if current else None,
                "published_at": current["response"]["published_at"] if current else None,
                "pending": current is None or current["response"]["revision"] != revision}

    def build_signature(self):
        sources = [*WEB.glob('*.js'), *WEB.glob('*.css'), WEB / 'index.html',
                   Path(__file__), Path(__file__).parents[1] / 'accounts/site.py',
                   Path(__file__).parents[1] / 'accounts/index.py',
                   Path(__file__).parents[1] / 'accounts/usage.py',
                   Path(__file__).parents[1] / 'accounts/quality.py',
                   Path(__file__).parents[1] / 'accounts/pricing.py', *WEB.glob('logos/*'),
                   *(p for p in self.catalog.rglob('*') if p.is_file())]
        return digest(public_encoded([sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in sources), self.announcement()]))

    @staticmethod
    def content_signature(accounts):
        return digest(public_encoded(sorted((a['account_id'], a['source_version']) for a in accounts)))

    def publish_usage(self, node=None):
        """Update only already-published availability; no image reads or new accounts."""
        with self.lock:
            current = self.current()
            if not current or not current.get('all_accounts'):
                return {'pending': True, 'message': '等待首次全量目录发布。'}
            remote = self.store.sync_manifest(inventory=True)
            if current.get('usage_revision') == remote['revision']:
                return {'pending': False, 'public_revision': current['response']['public_revision']}
            source = self.root / 'versions' / current['directory']
            old_data_name = f"data/snapshot.{current['response']['public_revision']}.json"
            data = json.loads((source / old_data_name).read_text('utf-8'))
            states = {s['result']['index']: s['result']['used'] for s in remote['states']}
            changed = False
            for account in data['accounts']:
                used = states.get(account['index'], False)
                changed |= account['used'] != used
                account['used'] = used
            content_same = current.get('content_signature') == self.content_signature(remote['accounts'])
            if not changed:
                current['usage_revision'] = remote['revision']
                self._usage_pointer(current, remote['revision'])
                return {'pending': False, 'public_revision': data['revision']}
            data.pop('revision')
            data['meta'].pop('loaded_at', None)
            data['meta']['used_accounts'] = sum(a['used'] for a in data['accounts'])
            data['meta']['valid_accounts'] = len(data['accounts']) - data['meta']['used_accounts']
            revision = digest(public_encoded(data))
            data['revision'] = revision
            from datetime import UTC, datetime
            data['meta']['loaded_at'] = datetime.now(UTC).isoformat()
            directory = digest(public_encoded(['usage', current['directory'], revision]))
            target = self.root / 'versions' / directory
            target.mkdir(exist_ok=True)
            files = {n: d for n, d in current['files'].items() if n not in (old_data_name, 'data/current.json')}
            for name in files:
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                if not path.exists():
                    try:
                        os.link(source / name, path)
                    except OSError:
                        shutil.copyfile(source / name, path)
            updates = {f'data/snapshot.{revision}.json': public_encoded(data),
                       'data/current.json': public_encoded({'schema_version': data['schema_version'], 'revision': revision})}
            for name, content in updates.items():
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                files[name] = {'sha256': digest(content), 'size': len(content)}
            sync_version(target, files)
            response = {**current['response'], 'public_revision': revision, 'published_at': self.store.clock(),
                        'changed_files': 2, 'revision': remote['revision'] if content_same else current['response']['revision']}
            pointer = {**current, 'directory': directory, 'files': files, 'response': response,
                       'usage_only': True, 'usage_revision': remote['revision'],
                       'history': [current['directory'], *current.get('history', [])]}
            self._usage_pointer(pointer, remote['revision'])
            return {'pending': False, **response}

    def _usage_pointer(self, pointer, expected):
        with self.store.transaction() as db:
            if int(self.store.meta(db, 'revision')) != expected:
                raise Conflict('Usage changed while publishing; retry')
            temporary = self.pointer.with_suffix('.tmp')
            with temporary.open('w', encoding='utf-8') as handle:
                handle.write(encoded(pointer))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.pointer)
            sync_directory(self.root)

    def publish(self, node, request, *, automatic=False):
        with self.lock:
            with self.store.connection() as db:
                if not automatic:
                    self.store.require_node(db, node)
            self.recover_receipt()
            with self.store.connection() as db:
                prior = self.store.receipt(db, node, "publish", request["request_id"], request)
            if prior:
                return prior
            request_hash = hashlib.sha256(encoded({"node": node, **request}).encode()).hexdigest()
            current = self.current()
            if current and not current.get('usage_only') and current["request_hash"] == request_hash:
                # Recover the response if switching succeeded before receipt commit.
                with self.store.transaction() as db:
                    self.store.save_receipt(db, node, "publish", request["request_id"], request,
                                            current["response"])
                return current["response"]
            manifest = self.store.sync_manifest()
            build_signature = self.build_signature()
            if manifest['revision'] != request['expected_revision']:
                raise Conflict('Private revision changed before publishing')
            if (current and current['response']['revision'] == manifest['revision']
                    and current.get('build_signature') == build_signature
                    and current.get('all_accounts')):
                response = {**current['response'], 'changed_files': 0, 'unchanged': True}
                with self.store.transaction() as db:
                    if not automatic:
                        self.store.require_node(db, node)
                    if int(self.store.meta(db, 'revision')) != manifest['revision']:
                        raise Conflict('Private revision changed during verification')
                    self.store.save_receipt(db, node, 'publish', request['request_id'], request, response)
                return response
            snapshot = self.store.results()
            if snapshot["revision"] != request["expected_revision"]:
                raise Conflict("Private revision changed; review before publishing")
            versions = self.root / "versions"
            versions.mkdir(exist_ok=True)
            target = versions / request_hash
            with tempfile.TemporaryDirectory(prefix="ournotes-cloud-export-") as temporary:
                archives = Path(temporary) / "accounts"
                archives.mkdir()
                for envelope in snapshot["results"]:
                    value = envelope["result"]
                    archive = {"schema_version": 1, "alias_index": value["index"], "alias": value["email"],
                               "status": "complete", "cards": value["cards"], "totals": value["totals"],
                               "pull_count": value["pull_count"], "pool_keys": value.get("pool_keys", []), "used": value["used"]}
                    if 'activity_id' in value:
                        archive.update(activity_id=value['activity_id'], activity=value.get('activity'))
                    (archives / f"{value['index']}.json").write_text(encoded(archive), "utf-8")
                # Existing exporter performs shared catalog validation, global ranking and whitelist projection.
                exported = export_site(archives, self.catalog, target, allow_empty=True,
                                       public_catalog=True,
                                       reuse_directory=versions / current['directory'] if current else None,
                                       asset_cache=current.get('asset_cache') if current else None,
                                       announcement=self.announcement()['content'])
            if exported["excluded"]:
                raise Conflict("Cloud results failed shared public catalog validation")
            old_files = current.get("files", {}) if current else {}
            changed = sum(old_files.get(name) != description for name, description in exported["files"].items())
            # Reuse unchanged bytes as hardlinks; never overwrite an old version.
            for name, description in exported["files"].items():
                path = target / name
                if not public_path(name) or path.is_symlink():
                    raise Conflict("Unexpected public resource")
                if current and old_files.get(name) == description:
                    previous = versions / current['directory'] / name
                    if previous.is_file() and os.path.samefile(previous, path):
                        continue  # Previously verified immutable inode, reused without rereading artwork.
                content = path.read_bytes()
                if hashlib.sha256(content).hexdigest() != description["sha256"] or len(content) != description["size"]:
                    raise Conflict("Public resource hash mismatch")
                if current and old_files.get(name) == description:
                    previous = versions / current["directory"] / name
                    if previous.is_file() and previous.read_bytes() == content:
                        path.unlink()
                        try:
                            os.link(previous, path)
                        except OSError:
                            path.write_bytes(content)
            sync_version(target, exported["files"])
            response = {"revision": snapshot["revision"], "public_revision": exported["revision"],
                        "accounts": exported["accounts"], "published_at": self.store.clock(),
                        "changed_files": changed}
            pointer = {"directory": request_hash, "request_hash": request_hash,
                       "node": node, "request": request,
                       "response": response, "files": exported["files"],
                       "asset_cache": exported.get('asset_cache', {}), 'all_accounts': True,
                       'build_signature': build_signature, 'usage_revision': snapshot['revision'],
                       'content_signature': self.content_signature([
                           {'account_id': e['result']['account_id'], 'source_version': e['source_version']}
                           for e in snapshot['results']]),
                       "history": ([current["directory"], *current.get("history", [])] if current else [])}
            with self.store.transaction() as db:
                if not automatic:
                    self.store.require_node(db, node)
                prior = self.store.receipt(db, node, "publish", request["request_id"], request)
                if prior:
                    return prior
                if int(self.store.meta(db, "revision")) != snapshot["revision"]:
                    raise Conflict("Private revision changed while rendering; review again")
                temporary = self.pointer.with_suffix(".tmp")
                with temporary.open("w", encoding="utf-8") as handle:
                    handle.write(encoded(pointer))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.pointer)
                sync_directory(self.root)
                self.store.save_receipt(db, node, "publish", request["request_id"], request, response)
            return response

    def resource(self, name):
        if not public_path(name):
            raise KeyError(name)
        current = self.current()
        if not current:
            raise KeyError(name)
        target = self.root / "versions" / current["directory"] / name
        if target.is_file() and not target.is_symlink():
            return target
        # A browser opened before a switch may still request its previous immutable snapshot.
        if name not in ("index.html", "data/current.json"):
            for directory in current.get("history", []):
                if not isinstance(directory, str) or not directory.isalnum():
                    raise Unavailable("Publication history invalid")
                version = self.root / "versions" / directory
                candidate = version / name
                if version.is_dir() and not version.is_symlink() and candidate.is_file() and not candidate.is_symlink():
                    return candidate
        raise KeyError(name)
