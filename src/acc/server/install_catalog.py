"""Service-only reviewed recipe retention and bounded canonical-origin fetching.

No install-time registry access. Catalog import is an explicit maintainer/person
publication step, not a proposal read from a task checkout. Empty catalog is valid.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import subprocess
import sys
from urllib.parse import quote, urlsplit

import httpx

from .. import install_artifact as artifact

RECIPE_FIELDS = {'id', 'revision', 'adapter', 'name', 'version', 'registry', 'artifact_sha512',
    'metadata_digest', 'manifest', 'manifest_digest', 'bins', 'runtime_id', 'limits'}


def validate(recipe):
    if (not isinstance(recipe, dict) or set(recipe) != RECIPE_FIELDS
            or not isinstance(recipe['id'], str) or not re.fullmatch('[a-z0-9][a-z0-9._-]{0,63}', recipe['id'])
            or type(recipe['revision']) is not int or recipe['revision'] < 1
            or recipe['adapter'] != 'npm-extractor-v1' or recipe['registry'] != artifact.REGISTRY
            or not isinstance(recipe['runtime_id'], str) or not re.fullmatch('[0-9a-f]{32}', recipe['runtime_id'])
            or not isinstance(recipe['artifact_sha512'], str) or not re.fullmatch('[0-9a-f]{128}', recipe['artifact_sha512'])
            or not isinstance(recipe['metadata_digest'], str) or not re.fullmatch('[0-9a-f]{64}', recipe['metadata_digest'])
            or recipe['manifest_digest'] != artifact.digest(recipe['manifest'])):
        raise artifact.InvalidArtifact()
    artifact.canonical(recipe)
    artifact.limits(recipe['limits'])
    artifact.metadata({'name': recipe['name'], 'version': recipe['version'], 'bin': recipe['bins']})
    return recipe


def inspect_isolated(blob, metadata, ceilings=None):
    """Isolated stdlib parser, fixed environment, one bounded deadline, fixed errors."""
    try:
        result = subprocess.run([sys.executable, '-I', artifact.__file__],
            input=artifact.canonical({'artifact': base64.b64encode(blob).decode('ascii'), 'metadata': metadata, 'limits': artifact.limits(ceilings)}),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, cwd='/',
            env={'PATH': artifact.TRUSTED_PATH, 'LANG': 'C.UTF-8'}, check=False)
        if result.returncode or len(result.stdout) > 4 * 1024 * 1024:
            raise artifact.InvalidArtifact()
        return artifact.json_object(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        raise artifact.InvalidArtifact() from None


FETCH_SECONDS = 60


def fetch(name, version):
    """Service publication worker only; one deadline across both network requests."""
    artifact.metadata({'name': name, 'version': version})

    async def network():
        total = 0
        async def read(client, url, ceiling):
            nonlocal total
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or parsed.netloc != 'registry.npmjs.org'
                    or parsed.username or parsed.password or parsed.fragment):
                raise artifact.InvalidArtifact()
            chunks, size = [], 0
            async with client.stream('GET', url) as response:
                if (response.status_code != 200
                        or response.headers.get('content-encoding', 'identity').lower() != 'identity'):
                    raise artifact.InvalidArtifact()
                # No implicit HTTP decompressor may allocate outside our byte ceiling.
                async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > ceiling or total > artifact.MAX_COMPRESSED + 4 * 1024 * 1024:
                        raise artifact.InvalidArtifact('resource_limit')
                    chunks.append(chunk)
            return b''.join(chunks)
        async with asyncio.timeout(FETCH_SECONDS):
            async with httpx.AsyncClient(timeout=FETCH_SECONDS, follow_redirects=False, trust_env=False,
                    headers={'Accept-Encoding': 'identity'}) as client:
                packument = await read(client, artifact.REGISTRY + '/' + quote(name, safe='') + '/' + quote(version, safe=''),
                                       4 * 1024 * 1024)
                metadata = artifact.json_object(packument)
                if metadata.get('name') != name or metadata.get('version') != version:
                    raise artifact.InvalidArtifact()
                dist = metadata.get('dist')
                if not isinstance(dist, dict) or not isinstance(dist.get('integrity'), str):
                    raise artifact.InvalidArtifact()
                integrity = dist['integrity']
                if not integrity.startswith('sha512-') or ' ' in integrity:
                    raise artifact.InvalidArtifact()
                expected = base64.b64decode(integrity[7:], validate=True)
                if len(expected) != 64:
                    raise artifact.InvalidArtifact()
                blob = await read(client, dist['tarball'], artifact.MAX_COMPRESSED)
                if not hmac.compare_digest(hashlib.sha512(blob).digest(), expected):
                    raise artifact.InvalidArtifact()
                return metadata, blob, hashlib.sha256(packument).hexdigest()
    try:
        metadata, blob, packument_digest = asyncio.run(network())
        inspected = inspect_isolated(blob, metadata)
        return metadata, blob, inspected, packument_digest
    except (TimeoutError, httpx.TimeoutException):
        raise artifact.InvalidArtifact('resource_limit') from None
    except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
        raise artifact.InvalidArtifact() from None


def retain(db, recipe, metadata, blob, packument_digest, *, reviewer, approver):
    """Called by trusted catalog publication, never agent input or task proposals."""
    from . import approvals
    from .models import InstallRecipe
    validate(recipe)
    try:
        dist = metadata['dist']
        origin = urlsplit(dist['tarball'])
        expected = 'sha512-' + base64.b64encode(hashlib.sha512(blob).digest()).decode('ascii')
        if (origin.scheme != 'https' or origin.netloc != 'registry.npmjs.org' or origin.fragment
                or dist['integrity'] != expected
                or not re.fullmatch('[0-9a-f]{64}', packument_digest)):
            raise artifact.InvalidArtifact()
    except (KeyError, TypeError, ValueError):
        raise artifact.InvalidArtifact() from None
    if reviewer == approver:
        raise artifact.InvalidArtifact('authority_changed')
    inspected = inspect_isolated(blob, metadata, recipe['limits'])
    if (recipe['name'] != metadata['name'] or recipe['version'] != metadata['version']
            or recipe['artifact_sha512'] != inspected['artifact_sha512']
            or recipe['manifest'] != inspected['manifest'] or recipe['bins'] != inspected['bins']
            or recipe['metadata_digest'] != artifact.digest(metadata)):
        raise artifact.InvalidArtifact()
    approvals.security.lock_revision(db)
    approvals.person(db, reviewer)
    owner = approvals.person(db, approver)
    db.refresh(owner)
    if owner.role != 'owner':
        raise artifact.InvalidArtifact('authority_changed')
    identity = artifact.digest(recipe)
    existing = db.get(InstallRecipe, identity)
    if existing:
        return existing
    row = InstallRecipe(digest=identity, recipe_id=recipe['id'], recipe=recipe, metadata_entry=metadata,
        artifact=blob, packument_digest=packument_digest, reviewer=reviewer, approver=approver)
    db.add(row)
    db.flush()
    approvals.audit(db, approver, 'recipe.publish', identity, {'reviewer': reviewer, 'recipe_id': recipe['id']})
    return row


def resolve(db, recipe_id, recipe_digest):
    from .models import InstallRecipe
    row = db.get(InstallRecipe, recipe_digest, populate_existing=True)
    if (row is None or row.withdrawn or row.recipe_id != recipe_id
            or artifact.digest(row.recipe) != recipe_digest
            or artifact.digest(row.metadata_entry) != row.recipe['metadata_digest']
            or hashlib.sha512(row.artifact).hexdigest() != row.recipe['artifact_sha512']):
        raise artifact.InvalidArtifact()
    validate(row.recipe)
    return row


def withdraw(db, recipe_digest, *, actor):
    from . import approvals
    from .models import InstallRecipe
    approvals.security.lock_revision(db)
    if approvals.person(db, actor).role != 'owner':
        raise artifact.InvalidArtifact('authority_changed')
    row = db.get(InstallRecipe, recipe_digest)
    if row is None:
        raise artifact.InvalidArtifact()
    row.withdrawn = True
    approvals.audit(db, actor, 'recipe.withdraw', recipe_digest)


def publish(db_factory, recipe, *, reviewer, approver):
    """Trusted owner publication seam: fetch in service context before any write lock.

    This is not exposed to agents. The reviewed proposal must match the fetched
    metadata/artifact/manifests byte-for-byte; it never supplies executable bytes.
    """
    validate(recipe)
    metadata, blob, _, packument_digest = fetch(recipe['name'], recipe['version'])
    with db_factory() as db:
        row = retain(db, recipe, metadata, blob, packument_digest, reviewer=reviewer, approver=approver)
        identity = row.digest
        db.commit()
        return identity
