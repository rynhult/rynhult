"""Node-only fallback; TLS/HTTP failures must not spill credentials to other paths."""
import hashlib
import importlib
import ssl

import httpx
import pytest


@pytest.fixture
def transport():
    return importlib.import_module('acc.node.git_backup')


TOKEN_SHA256 = hashlib.sha256(b't' * 43).hexdigest()  # what the main sends over the signed link


async def select(transport, private_handler, public_handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(private_handler)) as private:
        async with httpx.AsyncClient(transport=httpx.MockTransport(public_handler)) as public:
            return await transport.request_token('https://acc.example.lan', 'https://remote.example.com',
                                                 7, 'g' * 43, private, lambda: public, token_sha256=TOKEN_SHA256)


async def test_private_path_wins_and_public_is_never_contacted(transport):
    def private(request):
        assert str(request.url) == 'https://acc.example.lan/api/git/7.git/token'
        assert request.headers['authorization'] == 'BackupGrant ' + 'g' * 43
        return httpx.Response(200, json={'token': 't' * 43})
    def public(request):
        pytest.fail('private success must not contact public address')
    assert await select(transport, private, public) == ('https://acc.example.lan', 't' * 43, 'private')


@pytest.mark.parametrize('failure', [httpx.ConnectError, httpx.ConnectTimeout])
async def test_unreachable_private_path_falls_back_for_token_request(transport, failure):
    def private(request):
        raise failure('unreachable')
    def public(request):
        assert request.url.host == 'remote.example.com'
        return httpx.Response(200, json={'token': 't' * 43})
    assert await select(transport, private, public) == ('https://remote.example.com', 't' * 43, 'public')


@pytest.mark.parametrize('status', [301, 302, 307, 308, 401, 403, 500, 503])
async def test_private_http_refusal_or_redirect_never_triggers_fallback(transport, status):
    def private(request):
        return httpx.Response(status, headers={'Location': 'https://evil.example.com'})
    def public(request):
        pytest.fail('HTTP failures and redirects are not network unreachability')
    # A 503 is a deferral (capture busy), not a refusal: still no fallback and no token.
    with pytest.raises(transport.BackupBusy if status == 503 else ValueError):
        await select(transport, private, public)


async def test_private_tls_validation_failure_never_triggers_fallback(transport):
    def private(request):
        raise httpx.ConnectError('TLS validation failed') from ssl.SSLCertVerificationError('wrong CA')
    def public(request):
        pytest.fail('a failed trust check must not trigger fallback')
    with pytest.raises(httpx.ConnectError):
        await select(transport, private, public)


@pytest.mark.parametrize('path', ['private', 'public'])
async def test_a_server_that_did_not_issue_the_token_gets_no_push(transport, path):
    # Public server trust is any system CA. A server holding a publicly trusted
    # certificate for the public name, or a TLS-inspection root on the machine,
    # can answer the token request; only the real main knows the token whose
    # hash the signed job carries, so the node must not push to anyone else.
    def impostor(request):
        return httpx.Response(200, json={'token': 'x' * 43})
    def private(request):
        if path == 'public':
            raise httpx.ConnectError('offline')
        return impostor(request)
    with pytest.raises(ValueError, match='did not come from'):
        await select(transport, private, impostor)


async def test_public_redirect_is_refused_without_following_it(transport):
    def private(request):
        raise httpx.ConnectError('offline')
    def public(request):
        assert request.url.host == 'remote.example.com'
        return httpx.Response(307, headers={'Location': 'https://evil.example.com'})
    with pytest.raises(ValueError):
        await select(transport, private, public)


@pytest.mark.parametrize('origin', ['http://remote.example.com', 'https://u:p@remote.example.com',
                                  'https://remote.example.com/elsewhere', 'https://remote.example.com?x=1'])
def test_public_origin_must_be_an_authenticated_https_origin(transport, origin):
    with pytest.raises(ValueError):
        transport.origin(origin)


def test_public_context_requires_machine_certificate(transport, tmp_path):
    with pytest.raises(ValueError, match='machine identity'):
        transport.public_context(tmp_path, 'a' * 32)


@pytest.mark.parametrize('method,path', [('GET', '/api/nodes/connect'), ('GET', '/api/git/8.git/info/refs?service=git-receive-pack'),
                                        ('POST', '/api/git/7.git/git-upload-pack'), ('GET', '/api/git/7.git/info/refs?service=git-upload-pack')])
async def test_forwarder_is_scoped_to_one_projects_push_protocol(transport, method, path):
    async def upstream(request):
        pytest.fail('out-of-scope request reached the public path')
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = transport.forward_app(remote, 'https://remote.example.com', 7, 't' * 43)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1') as local:
            response = await local.request(method, path, headers={'Authorization': 'Bearer ' + 't' * 43})
            assert response.status_code == 403


async def test_forwarder_requires_push_token_and_streams_git_bytes_only(transport):
    seen = []
    async def upstream(request):
        seen.append((str(request.url), request.headers['Authorization'], await request.aread()))
        return httpx.Response(200, content=b'git-response', headers={'Content-Type': 'application/x-git-receive-pack-result'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = transport.forward_app(remote, 'https://remote.example.com', 7, 't' * 43)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1') as local:
            path = '/api/git/7.git/git-receive-pack'
            assert (await local.post(path, content=b'no-secret')).status_code == 403
            response = await local.post(path, headers={'Authorization': 'Bearer ' + 't' * 43,
                                         'Content-Type': 'application/x-git-receive-pack-request'}, content=b'git-pack')
            assert response.status_code == 200 and response.content == b'git-response'
    assert seen == [('https://remote.example.com/api/git/7.git/git-receive-pack', 'Bearer ' + 't' * 43, b'git-pack')]


async def test_push_redirect_is_refused_and_not_exposed_to_git(transport):
    seen = []
    def upstream(request):
        seen.append(request.url.host)
        return httpx.Response(307, headers={'Location': 'https://evil.example.com/api/git/7.git/git-receive-pack'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = transport.forward_app(remote, 'https://remote.example.com', 7, 't' * 43)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1') as local:
            response = await local.post('/api/git/7.git/git-receive-pack', headers={'Authorization': 'Bearer ' + 't' * 43})
            assert response.status_code == 502 and 'Location' not in response.headers
    assert seen == ['remote.example.com']


async def test_oversized_push_is_bounded_at_forwarder(transport):
    async def upstream(request):
        await request.aread()
        return httpx.Response(200)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as remote:
        app = transport.forward_app(remote, 'https://remote.example.com', 7, 't' * 43, max_push=8)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://127.0.0.1') as local:
            response = await local.post('/api/git/7.git/git-receive-pack', content=b'x' * 9,
                                        headers={'Authorization': 'Bearer ' + 't' * 43})
            assert response.status_code == 413


async def test_signed_node_link_delivers_one_backup_job_and_returns_its_result():
    import asyncio
    import json

    from acc.server.nodes import NodeLink

    sent = []
    async def send(raw):
        message = json.loads(raw)
        sent.append(message)
        link.git_backup_result({'t': 'git-backup-result', 'id': message['id'], 'ok': True,
                                'result': {'via': 'public', 'digest': 'abc', 'updated': [], 'kept': []}})
    link = NodeLink('a' * 32, 'roamer', send, {'capabilities': ['git-backup-v1']}, [])
    try:
        result = await link.git_backup({'project_id': 7}, timeout=1)
        assert result['via'] == 'public'
        assert [m['t'] for m in sent] == ['git-backup']
    finally:
        link.close()
        await asyncio.gather(link._sender, link._watchdog, return_exceptions=True)


async def test_node_dispatch_does_not_change_control_or_update_address(transport, tmp_path, monkeypatch):
    import asyncio
    import json

    from acc.node.client import Node, connect_url
    from acc.node.config import NodeConfig
    from acc.node.identity import generate_key

    node = Node(NodeConfig(primary='https://acc.example.lan', node_id='a' * 32), generate_key(), tmp_path, packages=None)
    seen = []
    async def job(config, state, payload, link_url, key):
        assert link_url == node.url  # the node learns backup origins only over its private control link
        assert key is node.key  # it signs the grant with its own link key (SR7-2)
        return {'via': 'public', 'digest': 'abc'}
    async def send(raw):
        seen.append(json.loads(raw))
    monkeypatch.setattr(transport, 'run_job', job)
    from types import SimpleNamespace
    await node.handle_text(SimpleNamespace(send=send), json.dumps({'t': 'git-backup', 'id': 'job1'}))
    await asyncio.gather(*node._tool_tasks)
    assert seen == [{'t': 'git-backup-result', 'id': 'job1', 'ok': True,
                     'result': {'via': 'public', 'digest': 'abc'}}]
    assert node.url == connect_url('https://acc.example.lan')
    assert node.config.alternate_primaries == ()


def test_repository_url_rewrite_cannot_send_push_token_to_another_host(env, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from test_gitbackup import git, make_repo

    from acc.broker import ops

    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.headers.get('Authorization'))
            self.send_response(401)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source = make_repo(env['workspaces'] / 'rewritten')
    wanted = 'https://acc.example.lan/api/git/7.git'
    evil = f'http://127.0.0.1:{server.server_port}/'
    git(source, 'config', f'url.{evil}.insteadOf', 'https://acc.example.lan/')
    try:
        with pytest.raises(ops.OpError):
            ops.git_backup(env['broker'], str(source), wanted, 't' * 43)
        assert received and all(value is None for value in received)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


async def test_forwarder_scope_exit_cancels_inflight_public_requests(transport):
    import asyncio

    started, stopped = asyncio.Event(), asyncio.Event()
    handlers = set()
    async def blocked(request):
        handlers.add(asyncio.current_task())
        started.set()
        try:
            await asyncio.sleep(300)
        finally:
            stopped.set()
        return httpx.Response(200)
    request = None
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as upstream:
            async with httpx.AsyncClient() as local:
                async with transport.local_forwarder(transport.forward_app(upstream, 'https://remote.example.com', 7, 't' * 43)) as base:
                    request = asyncio.create_task(local.get(base + '/api/git/7.git/info/refs?service=git-receive-pack',
                                                            headers={'Authorization': 'Bearer ' + 't' * 43}))
                    await asyncio.wait_for(started.wait(), 2)
                assert stopped.is_set(), 'An in-flight public request outlived the scoped backup forwarder'
    finally:
        for task in [*handlers, *([request] if request else [])]:
            task.cancel()
        await asyncio.gather(*handlers, *([request] if request else []), return_exceptions=True)


PRIVATE_LINK = 'wss://acc.example.lan/api/nodes/connect'
PUBLIC_LINK = 'wss://remote.example.com/api/nodes/connect'
ORIGINS = {'private_origin': 'https://acc.example.lan', 'public_origin': 'https://remote.example.com'}


def test_backup_origins_are_learned_over_the_private_link_and_pinned(transport, tmp_path):
    import stat
    assert transport.pinned_origins(tmp_path, PRIVATE_LINK, ORIGINS) == ORIGINS
    assert stat.S_IMODE((tmp_path / transport.ORIGINS_PIN).stat().st_mode) == 0o600
    # A job over a link through the public address is refused even when it names the pinned origins: whoever
    # answers for the public name would also have chosen the job's token hash.
    for changed in (ORIGINS, {**ORIGINS, 'public_origin': 'https://other.example.com'},
                    {**ORIGINS, 'public_origin': 'https://remote.example.com:8443'},
                    {**ORIGINS, 'private_origin': 'https://other.example.com'}):
        with pytest.raises(ValueError, match='private link'):
            transport.pinned_origins(tmp_path, PUBLIC_LINK, changed)
    # The owner changes the public hostname: the next job over the private link re-learns it, and a link through
    # the old public name still counts as public.
    moved = {**ORIGINS, 'public_origin': 'https://roam.example.com'}
    assert transport.pinned_origins(tmp_path, PRIVATE_LINK, moved) == moved
    moved_again = {**ORIGINS, 'public_origin': 'https://third.example.com'}
    with pytest.raises(ValueError, match='private link'):
        transport.pinned_origins(tmp_path, 'wss://roam.example.com/api/nodes/connect', moved_again)


@pytest.mark.parametrize('link', [PUBLIC_LINK, None])
def test_nothing_is_learned_without_the_private_link(transport, tmp_path, link):
    with pytest.raises(ValueError, match='private link'):
        transport.pinned_origins(tmp_path, link, ORIGINS)
    assert not (tmp_path / transport.ORIGINS_PIN).exists()


def _tls_pair(tmp_path, names=('first', 'second')):
    """One CA and two server certificates for the same address (127.0.0.1), like a real edge and an impostor holding
    another trusted certificate for its name."""
    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    def build(subject, issuer, key, signer, ca):
        builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
                   .not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if not ca:
            builder = builder.add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),
                                            critical=False)
        return builder.sign(signer, hashes.SHA256())
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'test root')])
    ca = build(ca_name, ca_name, ca_key, ca_key, True)
    (tmp_path / 'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    out = {}
    for name in names:
        key = ec.generate_private_key(ec.SECP256R1())
        cert = build(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]), ca_name, key, ca_key, False)
        path = tmp_path / f'{name}.pem'
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        out[name] = path
    return tmp_path / 'ca.pem', out


async def test_a_reconnect_to_another_trusted_server_is_refused_before_anything_is_sent(transport, tmp_path):
    """Security review 7, SR7-1: the push may need new connections; each must reach the server that answered the
    token request, not any server with a trusted certificate for the name."""
    import asyncio
    import ssl

    ca, certs = _tls_pair(tmp_path)
    received = []

    async def serve(reader, writer):
        received.append(await reader.read(100))
        writer.close()
    servers = {}
    for name, path in certs.items():
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(path)
        servers[name] = await asyncio.start_server(serve, '127.0.0.1', 0, ssl=context)
    try:
        port = {name: server.sockets[0].getsockname()[1] for name, server in servers.items()}
        system = ssl.create_default_context(cafile=str(ca))  # stands for "any system CA"
        leaf = await transport.server_leaf(f'https://127.0.0.1:{port["first"]}', system)
        pinned = transport.trusting_only(leaf)
        assert pinned.cert_store_stats()['x509'] == 1  # that leaf and nothing else (no system CAs)
        reader, writer = await asyncio.open_connection('127.0.0.1', port['first'], ssl=pinned, server_hostname='127.0.0.1')
        writer.write(b'to the real server')
        await writer.drain()
        writer.close()
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.open_connection('127.0.0.1', port['second'], ssl=pinned, server_hostname='127.0.0.1')
        await asyncio.sleep(0.1)
        assert b'to the real server' in received and len([r for r in received if r]) == 1  # nothing reached the second
    finally:
        for server in servers.values():
            server.close()
            await server.wait_closed()


@pytest.mark.parametrize('private_up', [True, False])
async def test_the_grant_is_signed_for_the_path_it_is_sent_on(transport, private_up):
    """SR7-2: each attempt carries the machine's signature over the grant, its project and that path."""
    from acc.node.identity import generate_key, public_key_b64
    from acc.server.gitbackup import PROOF_HEADER, grant_proof_message
    from acc.server.nodes import verify_signature

    key, sent = generate_key(), []
    def answer(channel):
        def handler(request):
            sent.append((channel, request.headers.get(PROOF_HEADER)))
            if channel == 'private' and not private_up:
                raise httpx.ConnectError('unreachable')
            return httpx.Response(200, json={'token': 't' * 43})
        return handler
    async with httpx.AsyncClient(transport=httpx.MockTransport(answer('private'))) as private:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer('public'))) as public:
            _, _, via = await transport.request_token('https://acc.example.lan', 'https://remote.example.com', 7, 'g' * 43,
                                                      private, lambda: public, token_sha256=TOKEN_SHA256, key=key)
    assert via == ('private' if private_up else 'public')
    assert [channel for channel, _ in sent] == (['private'] if private_up else ['private', 'public'])
    for channel, proof in sent:
        assert verify_signature(public_key_b64(key), grant_proof_message(channel, 7, 'g' * 43), proof)
        other = 'public' if channel == 'private' else 'private'
        assert not verify_signature(public_key_b64(key), grant_proof_message(other, 7, 'g' * 43), proof)
