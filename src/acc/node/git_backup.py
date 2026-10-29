"""One git-backup job's transport. Public credentials and the machine key stay on the node.

Nothing here changes control, update, enrollment or preview addressing. Public
server trust uses system CAs for one connection, then only the certificate it
presented for the rest of the job; private trust retains the enrollment configuration.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import inspect
import json
import re
import socket
import ssl
import tempfile
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import machine

TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
SHA256 = re.compile(r'[0-9a-f]{64}')
ORIGINS_PIN = 'git-backup-origins.json'  # in the state dir: what the main named over the private link


class BackupBusy(Exception):
    """The selected token endpoint is temporarily unavailable; send no git data."""


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Git backup requires an HTTPS origin without credentials or paths')
    return value.rstrip('/')


def pinned_origins(state_dir, link_url, job):
    """The backup origins this job may use: only a job over the private control link has any.

    A job arriving over a link through the public address (or with no link) is
    refused: whoever answers for the public name would also choose the job's
    token hash. The pin remembers the last public hostname, so a link through
    a previous public name still counts as public. Only the hostname is
    pinned: the server leaf rotates with ACME and keeps normal validation, and
    the edge revision changes with every paired device.
    """
    offered = {'private_origin': origin(job.get('private_origin') or ''),
               'public_origin': origin(job.get('public_origin') or '')}
    path = Path(state_dir) / ORIGINS_PIN
    try:
        pinned = json.loads(path.read_text())
        pinned = {k: origin(pinned[k]) for k in offered}
    except (OSError, ValueError, KeyError, TypeError):
        pinned = None
    public_hosts = {urlsplit(offered['public_origin']).hostname}
    if pinned:
        public_hosts.add(urlsplit(pinned['public_origin']).hostname)
    link_host = urlsplit(link_url).hostname if link_url else None
    if link_host and link_host not in public_hosts:
        if pinned != offered:
            from .config import write_atomic
            write_atomic(path, json.dumps(offered).encode(), 0o600)
        return offered
    raise ValueError('Git backup jobs are taken only over the private link, not through the public address')


def trusting_only(leaf: bytes) -> ssl.SSLContext:
    """A client context whose only trust anchor is this server certificate (DER): hostname and dates still checked."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # CERT_REQUIRED and check_hostname by default; no system CAs
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=leaf)
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # the leaf itself is the anchor, not its issuer
    return context


async def server_leaf(origin_url: str, context: ssl.SSLContext) -> bytes:
    """The certificate (DER) the origin presents on one connection verified with the given context."""
    parsed = urlsplit(origin_url)
    reader, writer = await asyncio.wait_for(asyncio.open_connection(
        parsed.hostname, parsed.port or 443, ssl=context, server_hostname=parsed.hostname), 10)
    try:
        leaf = writer.get_extra_info('ssl_object').getpeercert(binary_form=True)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    if not leaf:
        raise ValueError('The public address presented no certificate')
    return leaf


def public_context(state_dir, node_id, pinned: bytes | None = None):
    """The node's machine identity as client certificate. Server trust: system CAs, or only `pinned` (a leaf, DER)."""
    raw = machine._read(state_dir / machine.IDENTITY) if state_dir else None
    if not raw:
        raise ValueError('Public backup requires this node\'s machine identity')
    cert = x509.load_pem_x509_certificate(raw)
    now = datetime.now(timezone.utc)
    if (cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value != node_id
            or cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)[0].value != 'acc-machine'
            or ExtendedKeyUsageOID.CLIENT_AUTH not in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            or not cert.not_valid_before_utc <= now < cert.not_valid_after_utc):
        raise ValueError('Public backup requires this node\'s current machine identity')
    context = trusting_only(pinned) if pinned is not None else ssl.create_default_context()
    with tempfile.TemporaryDirectory(prefix='acc-git-identity-') as private:
        from .config import write_atomic
        copy = Path(private) / 'identity.pem'
        write_atomic(copy, raw, 0o600)
        context.load_cert_chain(copy)
    return context


def unreachable(error):
    import httpx
    if not isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return False
    current = error
    while current:
        if isinstance(current, ssl.SSLError):
            return False
        current = current.__cause__ or current.__context__
    return True


async def request_token(private_origin, public_origin, project_id, grant, private, public_client, token_sha256, key=None):
    """Only a failure to establish the private connection permits the public attempt.

    public_client constructs the mTLS client lazily; missing/wrong identities
    refuse before any grant/token can be sent to the public address. The token
    must match the hash the main sent over the signed link, and public_client
    (sync or async) trusts only the leaf one verified connection presented, so
    no git data goes to a server that did not issue the token, on any connection.
    """
    import httpx
    private_origin, public_origin = origin(private_origin), origin(public_origin)
    if not TOKEN.fullmatch(grant):
        raise ValueError('Invalid git backup job grant')
    if not isinstance(token_sha256, str) or not SHA256.fullmatch(token_sha256):
        raise ValueError('Invalid git backup token commitment')
    suffix = f'/api/git/{int(project_id)}.git/token'

    def headers(channel):
        """The grant, with this machine's signature over it for this path (the main refuses a grant without one)."""
        from ..server.gitbackup import PROOF_HEADER, grant_proof_message
        sent = {'Authorization': 'BackupGrant ' + grant}
        if key is not None:
            sent[PROOF_HEADER] = base64.b64encode(key.sign(grant_proof_message(channel, project_id, grant))).decode()
        return sent
    selected, via, client = private_origin, 'private', private
    try:
        response = await private.post(private_origin + suffix, headers=headers('private'), follow_redirects=False,
                                      timeout=httpx.Timeout(30, connect=5))
    except (httpx.ConnectError, httpx.ConnectTimeout) as error:
        if not unreachable(error):
            raise
        client = public_client()
        if inspect.isawaitable(client):
            client = await client
        selected, via = public_origin, 'public'
        response = await client.post(public_origin + suffix, headers=headers('public'), follow_redirects=False,
                                     timeout=httpx.Timeout(30, connect=5))
    if response.status_code == 503:
        raise BackupBusy()
    if response.status_code != 200:
        raise ValueError('Git backup token request refused (HTTP ' + str(response.status_code) + ')')
    token = response.json().get('token')
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ValueError('Invalid git backup push token')
    if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), token_sha256):
        raise ValueError(f'The git backup token from the {via} address did not come from this Rynhult; nothing was pushed')
    return selected, token, via


def forward_app(client, selected_origin, project_id, token, max_push=2 << 30):
    """A transient loopback endpoint: one token, one repo, receive-pack only.

    The broker never receives a certificate/key path or a public credential URL.
    Requests are streamed, not buffered in the node's memory.
    """
    import secrets

    import httpx
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import Response, StreamingResponse
    from starlette.routing import Route

    prefix = f'/api/git/{project_id}.git'

    async def handle(request: Request):
        allowed = ((request.method == 'GET' and request.url.path == prefix + '/info/refs'
                    and request.url.query == 'service=git-receive-pack') or
                   (request.method == 'POST' and request.url.path == prefix + '/git-receive-pack'
                    and not request.url.query))
        if not allowed or not secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
            return Response(status_code=403)

        async def body():
            count = 0
            async for chunk in request.stream():
                count += len(chunk)
                if count > max_push:
                    raise OverflowError('git push exceeds the backup limit')
                yield chunk
        url = selected_origin + request.url.path + ('?' + request.url.query if request.url.query else '')
        headers = {'Authorization': 'Bearer ' + token}
        if content_type := request.headers.get('content-type'):
            headers['Content-Type'] = content_type
        try:
            upstream = await client.send(client.build_request(request.method, url, headers=headers, content=body()),
                                         stream=True, follow_redirects=False)
        except asyncio.CancelledError:
            return Response(status_code=503)
        except OverflowError:
            return Response(status_code=413)
        except httpx.HTTPError:
            return Response(status_code=502)
        if 300 <= upstream.status_code < 400:
            await upstream.aclose()
            return Response(status_code=502)  # never hand git a redirect or any destination supplied by it

        async def answer():
            count = 0
            try:
                async for chunk in upstream.aiter_bytes():
                    count += len(chunk)
                    if count > 64 << 20:
                        raise ValueError('git backup response exceeds the limit')
                    yield chunk
            finally:
                await upstream.aclose()
        return StreamingResponse(answer(), status_code=upstream.status_code,
                                 media_type=upstream.headers.get('content-type', 'application/octet-stream'),
                                 headers={'Cache-Control': 'no-store'})

    return Starlette(routes=[Route('/{path:path}', handle, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH'])])



@asynccontextmanager
async def local_forwarder(app):
    import uvicorn

    class Server(uvicorn.Server):
        def capture_signals(self):
            return nullcontext()  # the node, not each transient forwarder, owns process signals

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    sock.listen(8)
    server = Server(uvicorn.Config(app, lifespan='off', access_log=False, log_config=None,
                                   timeout_keep_alive=1, limit_concurrency=4))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise ValueError('Git backup forwarder did not start')
                await asyncio.sleep(0.01)
        yield f'http://127.0.0.1:{sock.getsockname()[1]}'
    finally:
        server.should_exit = True
        # Closing a signed link ends its capability-bearing HTTP work too. Do
        # not leave an accepted connection/task alive after the listener ends.
        for connection in list(server.server_state.connections):
            connection.transport.close()
        active = list(server.server_state.tasks)
        for request in active:
            request.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        try:
            await asyncio.wait_for(task, 5)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        sock.close()


async def run_job(config, state_dir, job, link_url=None, key=None):
    """Called only by the signed control link, with a main-issued grant for this project."""
    from contextlib import AsyncExitStack

    import httpx

    from ..server.broker_client import BrokerClient, uid_of
    from .identity import tls_context

    account = job.get('account')
    if not isinstance(account, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', account):
        raise ValueError('Invalid git backup account')
    project_id = job.get('project_id')
    if type(project_id) is not int or not 0 < project_id < 10**12:
        raise ValueError('Invalid git backup project')
    if not isinstance(job.get('path'), str) or len(job['path']) > 4096:
        raise ValueError('Invalid git backup path')
    uid = uid_of(account)
    if uid is None:
        raise ValueError('Git backup broker account is unavailable')
    timeout = httpx.Timeout(1800, connect=5)
    async with AsyncExitStack() as stack:
        private = await stack.enter_async_context(httpx.AsyncClient(
            verify=tls_context(config.trust, config.ca_file), trust_env=False, timeout=timeout))
        public = None
        origins = pinned_origins(state_dir, link_url, job)

        async def public_client():
            # The token hash proves which server answered the token request, but httpx may open new connections for
            # the push. So every public connection of this job, the token request included, trusts only the leaf
            # one verified connection presented (security review 7, SR7-1): a later connection to any other
            # server fails before the token or git data is sent.
            nonlocal public
            leaf = await server_leaf(origins['public_origin'], public_context(state_dir, config.node_id))
            public = httpx.AsyncClient(verify=public_context(state_dir, config.node_id, pinned=leaf), trust_env=False,
                                       timeout=timeout)
            stack.push_async_callback(public.aclose)
            return public
        try:
            selected, token, via = await request_token(origins['private_origin'], origins['public_origin'], project_id,
                                                       job['grant'], private, public_client, job.get('token_sha256'), key=key)
        except BackupBusy:
            return {'retry': True}
        async with local_forwarder(forward_app(public if via == 'public' else private,
                                              selected, project_id, token)) as loopback:
            broker = BrokerClient(config.socket_dir / f'{account}.sock', timeout=1800, trusted_uids=frozenset({0, uid}))
            try:
                result = await broker.call('git_backup', path=job['path'],
                                           url=f'{loopback}/api/git/{project_id}.git', token=token)
            finally:
                await broker.close()
        return {**result, 'via': via}
