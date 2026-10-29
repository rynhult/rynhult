"""Reviewed, transactional builtin restoration into the writable catalog layer."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import tomllib
import uuid
from pathlib import Path, PurePosixPath

from .. import providers
from . import catalog_import
from .catalog import ID, CatalogError, _check_entry, load, read_local, restored_layer
from .catalog import restored_root as active_restored_root
from .catalog_store import CatalogStore, StoreError, catalog_lock


class Restorer:
    def __init__(self, repo_root: Path, local_root: Path):
        self.repo_root, self.local_root = repo_root, local_root

    def locked(self):
        return catalog_lock(self.local_root)

    def fingerprint(self):
        digest = hashlib.sha256()
        for root in (self.repo_root, self.local_root, active_restored_root(self.local_root)):
            if root is None or not root.exists():
                continue
            for path in sorted(root.rglob('*')):
                if path.is_file() and '.restore.lock' != path.name and not (root == self.local_root and 'restored' in path.relative_to(root).parts):
                    digest.update(str(path.relative_to(root)).encode() + b'\0' + path.read_bytes())
        active = self.local_root / 'restored/active.json'
        if active.exists():
            digest.update(active.read_bytes())
        return digest.hexdigest()

    def preview(self, source_root: Path, version: str, source: str) -> dict:
        with self.locked():
            manifest = tomllib.loads((source_root / 'manifest.toml').read_text())
            installed = tomllib.loads((self.repo_root / 'manifest.toml').read_text())
            local = read_local(self.local_root)
            restored, restored_root = restored_layer(self.local_root)
            rows = []
            for kind in ('skill', 'agent', 'mcp'):
                base = {e['id']: e for e in installed.get(kind, [])}
                saved = {e['id']: e for e in restored.get(kind, [])}
                custom = {e['id']: e for e in local[kind + 's']}
                for entry in manifest.get(kind, []):
                    item_id = entry.get('id', '')
                    row = {'key': f'{kind}/{item_id}', 'kind': kind, 'id': item_id, 'meta': entry,
                           'files': {}, 'diff': '', 'flags': [], 'refused': '', 'collision': item_id in custom,
                           'excluded_profiles': [], 'status': 'new'}
                    try:
                        item = _check_entry(kind, entry, source_root)
                        row['files'] = item.files
                        row['flags'] = catalog_import.review(item.files, [])
                        if not set(entry.get('providers', [])) <= set(providers.ids()):
                            raise ValueError('unknown provider; update Rynhult before restoring this item')
                        for name, text in item.files.items():
                            path = PurePosixPath(name)
                            if path.is_absolute() or '..' in path.parts or len(path.parts) > 6:
                                raise ValueError('unsupported file path')
                            if name.endswith('.md'):
                                parsed = catalog_import.parse_frontmatter(text)
                                unknown = set(parsed.meta) - set(catalog_import.ALLOWED_FRONTMATTER) - catalog_import.RISKY_FRONTMATTER
                                if parsed.error or unknown:
                                    raise ValueError('unsupported frontmatter: ' + (parsed.error or ', '.join(sorted(unknown))))
                        if kind == 'mcp':
                            # Exercise the same structural validation used by manual/imported entries.
                            CatalogStore._providers(entry.get('providers') or ['claude', 'codex'], kind)
                            CatalogStore.mcp_fields(entry)
                            row['flags'] = catalog_import.review({'mcp.json': json.dumps(entry)}, [])
                        old = None
                        if item_id in saved or item_id in base:
                            row['status'] = 'missing'
                            try:
                                old = _check_entry(kind, saved.get(item_id, base.get(item_id)),
                                                   restored_root if item_id in saved else self.repo_root)
                            except (OSError, CatalogError):
                                pass
                        old_files = old.files if old else {}
                        row['diff'] = ''.join(''.join(difflib.unified_diff(
                            old_files.get(name, '').splitlines(True), item.files.get(name, '').splitlines(True),
                            fromfile=f'current/{name}', tofile=f'{version}/{name}'))
                            for name in sorted(set(old_files) | set(item.files)))
                        old_meta = {k: v for k, v in old.meta.items() if k not in ('origin', 'restore_version', 'builtin_id', 'env')} if old else {}
                        new_meta = {k: v for k, v in entry.items() if k != 'env'}
                        if old:
                            row['status'] = 'unchanged' if old.files == item.files and old_meta == new_meta else 'changed'
                            if kind == 'mcp' and 'env' in old.meta:
                                row['meta'] = {**entry, 'env': old.meta['env']}
                        row['diff'] += ''.join(difflib.unified_diff(
                            json.dumps(old_meta, indent=2, sort_keys=True).splitlines(True) if old else [],
                            json.dumps(new_meta, indent=2, sort_keys=True).splitlines(True),
                            fromfile='current/metadata', tofile=f'{version}/metadata'))
                        for profile_id, change in local['profiles'].items():
                            if item_id in change.get('remove', {}).get(kind + 's', []):
                                row['excluded_profiles'].append(profile_id)
                            elif (self.repo_root / 'profiles' / f'{profile_id}.toml').exists() and kind + 's' in change and item_id not in change[kind + 's']:
                                row['excluded_profiles'].append(profile_id)
                    except (ValueError, OSError, TypeError) as error:
                        row['refused'] = str(error)
                    rows.append(row)
            return {'source': source, 'version': version, 'fingerprint': self.fingerprint(), 'items': rows}

    def apply(self, preview: dict, ids: list[str] | None = None, *, aliases: dict | None = None,
              accept_risks: bool = False):
        with self.locked():
            if preview['fingerprint'] != self.fingerprint():
                raise StoreError('catalog changed; preview again')
            aliases = aliases or {}
            rows = {r['key']: r for r in preview['items']}
            if ids is not None and (not ids or set(ids) - rows.keys()):
                raise StoreError('choose known builtin items')
            selected = [rows[k] for k in ids] if ids is not None else [r for r in rows.values() if not r['collision'] and not r['refused']]
            data, old_root = restored_layer(self.local_root)
            local = read_local(self.local_root)
            targets = set()
            for row in selected:
                if row['refused']:
                    raise StoreError(row['refused'])
                if catalog_import.has_bad_flags(row['flags']) and not accept_risks:
                    raise StoreError('confirm risk acceptance before restoring flagged items')
                target = aliases.get(row['key'], row['id'])
                if not ID.fullmatch(target):
                    raise StoreError('invalid alternate id')
                if row['collision'] and target == row['id']:
                    raise StoreError('custom id collision; choose an unused alternate id')
                if target != row['id']:
                    manifest = tomllib.loads((self.repo_root / 'manifest.toml').read_text())
                    occupied = {e['id'] for e in manifest.get(row['kind'], []) + data.get(row['kind'], []) + local[row['kind'] + 's']}
                    if target in occupied:
                        raise StoreError('alternate id already exists')
                if (row['kind'], target) in targets:
                    raise StoreError('duplicate target id')
                targets.add((row['kind'], target))
            area = self.local_root / 'restored'
            area.mkdir(exist_ok=True)
            pointer = area / 'active.json'
            previous = pointer.read_bytes() if pointer.exists() else None
            generation = area / uuid.uuid4().hex
            try:
                if old_root:
                    shutil.copytree(old_root, generation)
                else:
                    generation.mkdir()
                for row in selected:
                    kind, target = row['kind'], aliases.get(row['key'], row['id'])
                    entries = [e for e in data.get(kind, []) if e['id'] != target]
                    entries.append({**row['meta'], 'id': target, 'restore_version': preview['version'], 'builtin_id': row['id']})
                    data[kind] = entries
                    if kind == 'skill':
                        directory = generation / 'skills' / target
                        if directory.exists():
                            shutil.rmtree(directory)
                        for name, content in row['files'].items():
                            path = directory / name
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text(content)
                    elif kind == 'agent':
                        path = generation / 'agents' / f'{target}.md'
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(next(iter(row['files'].values())))
                for path in [generation, *generation.rglob('*')]:
                    path.chmod(0o755 if path.is_dir() else 0o600 if path.name == 'items.json' else 0o644)
                area.chmod(0o755)
                self.local_root.chmod(0o755)
                metadata = generation / 'items.json'
                metadata.write_text(json.dumps(data))
                metadata.chmod(0o600)
                self._point(pointer, json.dumps(generation.name).encode())
                return load(self.repo_root, self.local_root)
            except Exception as error:
                if previous is None:
                    pointer.unlink(missing_ok=True)
                else:
                    self._point(pointer, previous)
                shutil.rmtree(generation, ignore_errors=True)
                raise StoreError(str(error)) from error

    @staticmethod
    def _point(pointer, content):
        tmp = pointer.with_suffix('.tmp')
        tmp.write_bytes(content)
        tmp.chmod(0o644)
        os.replace(tmp, pointer)
