"""Canonical-origin fetch and integrity tests, entirely in-memory HTTP fixtures."""
import base64
import hashlib
import json

import httpx
import pytest
from test_install_artifact import PACKAGE, archive

from acc import install_artifact as artifact
from acc.server import install_catalog


class Stream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


@pytest.mark.parametrize('change', ['valid', 'redirect', 'origin', 'userinfo', 'http', 'integrity', 'identity', 'multi-sri'])
def test_service_fetch_is_canonical_and_integrity_bound(monkeypatch, change):
    blob = archive()
    integrity = 'sha512-' + base64.b64encode(hashlib.sha512(blob).digest()).decode()
    metadata = PACKAGE | {'dist': {'tarball': 'https://registry.npmjs.org/sample/-/sample-1.2.3.tgz', 'integrity': integrity}}
    if change == 'origin': metadata['dist']['tarball'] = 'https://evil.example/sample.tgz'
    if change == 'userinfo': metadata['dist']['tarball'] = 'https://user@registry.npmjs.org/sample.tgz'
    if change == 'http': metadata['dist']['tarball'] = 'http://registry.npmjs.org/sample.tgz'
    if change == 'integrity': metadata['dist']['integrity'] = 'sha512-' + base64.b64encode(bytes(64)).decode()
    if change == 'multi-sri': metadata['dist']['integrity'] += ' sha256-other'
    if change == 'identity': metadata['version'] = '1.2.4'
    requests = []
    def respond(request):
        requests.append(request)
        if len(requests) == 1:
            if change == 'redirect':
                return httpx.Response(302, headers={'location':'https://evil.example/metadata'})
            return httpx.Response(200, stream=Stream(json.dumps(metadata).encode()))
        return httpx.Response(200, stream=Stream(blob))
    original = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return original(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(install_catalog.httpx, 'AsyncClient', client)
    if change == 'valid':
        actual, retained, inspected, upstream_digest = install_catalog.fetch(PACKAGE['name'], PACKAGE['version'])
        assert actual == metadata and retained == blob
        assert inspected['artifact_sha512'] == hashlib.sha512(blob).hexdigest()
        assert len(upstream_digest) == 64 and len(requests) == 2
    else:
        with pytest.raises(artifact.InvalidArtifact):
            install_catalog.fetch(PACKAGE['name'], PACKAGE['version'])
        if change in ('redirect', 'origin', 'userinfo', 'http', 'identity', 'multi-sri'):
            assert len(requests) == 1


def test_http_content_encoding_is_refused_before_decoding(monkeypatch):
    class NeverDecode(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError('HTTP content decoding must not run')
            yield b''
    original = httpx.AsyncClient
    def respond(request):
        return httpx.Response(200, headers={'content-encoding':'gzip'}, stream=NeverDecode())
    monkeypatch.setattr(install_catalog.httpx, 'AsyncClient', lambda **kw: original(
        transport=httpx.MockTransport(respond), **kw))
    with pytest.raises(artifact.InvalidArtifact):
        install_catalog.fetch(PACKAGE['name'], PACKAGE['version'])


def test_fetch_has_one_overall_deadline(monkeypatch):
    import asyncio
    original = httpx.AsyncClient
    async def delayed(request):
        await asyncio.sleep(0.1)
        raise AssertionError('request exceeded the catalog deadline')
    monkeypatch.setattr(install_catalog, 'FETCH_SECONDS', 0.01)
    monkeypatch.setattr(install_catalog.httpx, 'AsyncClient', lambda **kw: original(
        transport=httpx.MockTransport(delayed), **kw))
    with pytest.raises(artifact.InvalidArtifact, match='resource_limit'):
        install_catalog.fetch(PACKAGE['name'], PACKAGE['version'])
