"""Presentation-only package adapter in the Catalog storage/locking domain.

Never adds themes to executable Item pools, profile overlays or compiler revisions.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

from . import catalog_import, themes
from .catalog import load


class ThemeReviews(catalog_import.Previews):
    def put(self, payload):
        now = time.time()
        self._items = {k: v for k, v in self._items.items() if v[0] > now}
        themes.require(len(self._items) < 16 and sum(v[1]['actor'] == payload['actor'] for v in self._items.values()) < 2,
                       'Too many theme reviews. Cancel a review or wait for expiry.')
        token = super().put(payload)
        self._items[token] = (now + 900, payload)
        return token

    def peek(self, token, actor):
        value = self._items.get(token)
        if value is None or value[0] <= time.time():
            raise catalog_import.ImportError_('Theme review expired. Preview again.')
        if value[1]['actor'] != actor:
            raise PermissionError('This theme review belongs to another person.')
        return value[1]


class ThemePackages:
    def __init__(self, store):
        self.store = store
        self.root = store.local_root / 'packages' / 'themes'

    def directory(self):
        for path in (self.store.local_root, self.root.parent, self.root):
            themes.require(not path.is_symlink(), 'Catalog package directory is unsafe.')
            path.mkdir(exist_ok=True, parents=True)
        os.chmod(self.root, 0o700)
        return self.root

    def index(self):
        path = self.root / 'index.json'
        themes.require(not path.is_symlink(), 'Catalog theme index is unsafe.')
        try:
            data = json.loads(path.read_bytes())
            themes.require(isinstance(data, dict) and data.get('schema') == 1 and isinstance(data.get('items'), list) and
                           isinstance(data.get('receipts'), dict), 'Catalog theme index is invalid.')
            return data
        except FileNotFoundError:
            return {'schema': 1, 'items': [], 'receipts': {}}

    def revision(self):
        return hashlib.sha256((load(self.store.repo_root, self.store.local_root).revision +
                               json.dumps(self.index(), sort_keys=True)).encode()).hexdigest()

    def receipt_key(self, actor, token):
        return hashlib.sha256((actor + '\0' + token).encode()).hexdigest()

    def receipt(self, actor, token):
        return self.index()['receipts'].get(self.receipt_key(actor, token))

    def atomic(self, path, content):
        themes.require(not path.is_symlink(), 'Catalog package file is unsafe.')
        with tempfile.NamedTemporaryFile(dir=self.directory(), delete=False) as stream:
            tmp = Path(stream.name)
            try:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)

    def install(self, payload, actor, token):
        # Caller holds catalog_lock across revision recheck, install and audit.
        existing = self.receipt(actor, token)
        if existing:
            return existing, False
        themes.require(payload['revision'] == self.revision(), 'Catalog changed. Preview again.')
        result = themes.validate(payload['raw'])
        manifest = result['manifest']
        index = self.index()
        for item in index['items']:
            if item['id'] == manifest['id']:
                themes.require(item['author'] == manifest['author'] and item['source'] == manifest['source'],
                               'Publisher/source changed. Use a separate theme ID and review it.')
                themes.require(item['version'] != manifest['version'] or item['digest'] == manifest['digest'],
                               'Same theme version has different bytes.')
        digest = manifest['digest']
        path = self.directory() / (digest.removeprefix('sha256:') + '.zip')
        if path.exists():
            themes.require(themes.validate(path.read_bytes())['manifest']['digest'] == digest, 'Existing immutable package differs.')
        else:
            self.atomic(path, payload['raw'])
        entry = {'id': manifest['id'], 'version': manifest['version'], 'digest': digest,
                 'author': manifest['author'], 'source': manifest['source'], 'actor': actor,
                 'imported_at': time.time(), 'transport': 'upload', 'validator': themes.VALIDATOR,
                 'archive_sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'published': False}
        if not any(i['digest'] == digest for i in index['items']):
            index['items'].append(entry)
        key = self.receipt_key(actor, token)
        index['receipts'][key] = {'package_id': manifest['id'], 'digest': digest, 'published': False}
        # Bound receipt retention, never the immutable referenced package inventory.
        index['receipts'] = dict(list(index['receipts'].items())[-256:])
        self.atomic(self.root / 'index.json', json.dumps(index, sort_keys=True).encode())
        return index['receipts'][key], True

    def publish(self, actor, token):
        """Publish only after the durable audit receipt. Pending imports cannot be applied."""
        index = self.index()
        receipt = index['receipts'][self.receipt_key(actor, token)]
        for item in index['items']:
            if item['id'] == receipt['package_id'] and item['digest'] == receipt['digest']:
                item['published'] = True
        receipt['published'] = True
        self.atomic(self.root / 'index.json', json.dumps(index, sort_keys=True).encode())
        return {key: receipt[key] for key in ('package_id', 'digest')}

    def read(self, package_id, digest):
        themes.require(isinstance(digest, str) and len(digest) == 71 and digest.startswith('sha256:') and
                       all(c in '0123456789abcdef' for c in digest[7:]), 'Invalid theme digest.')
        entry = next((item for item in self.index()['items'] if item['id'] == package_id and item['digest'] == digest and item.get('published')), None)
        themes.require(entry is not None, 'Theme is not in the approved Catalog.')
        path = self.root / (digest[7:] + '.zip')
        themes.require(not path.is_symlink(), 'Catalog package file is unsafe.')
        raw = path.read_bytes()
        result = themes.validate(raw)
        themes.require(result['manifest']['id'] == package_id and result['manifest']['digest'] == digest and
                       result['archive_sha256'] == entry['archive_sha256'], 'Installed package integrity failure.')
        return result

    def list(self, *, owner=False):
        values = []
        for item in self.index()['items']:
            if not item.get('published'):
                continue
            try:
                values.append({**self.read(item['id'], item['digest']), 'provenance': {k: v for k, v in item.items() if owner or k != 'actor'}})
            except (themes.ThemeError, ValueError, OSError):
                values.append({'manifest': {'id': item['id'], 'digest': item['digest'], 'version': item['version'],
                                             'name': item['id']}, 'unavailable': True})
        return {'items': values, 'revision': self.revision()}
