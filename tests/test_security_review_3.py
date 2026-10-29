"""Review 3 proofs: inert guard strings and bounded, synthetic provider responses."""
import asyncio
import socket

import httpx
import pytest
from test_security_review_2 import guard

from acc.server.issue_sources import IssueSourceError, IssueSources


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.0.1',
    '169.254.169.254', '::1', 'fe80::1', 'fc00::1', '::ffff:127.0.0.1', '100.64.0.1'])
async def test_sr3_01_private_destination_never_receives_credentials(monkeypatch, address):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    delivered = []
    async def network(self, request):
        delivered.append(request)
        return httpx.Response(200, json=[], request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', network)
    with pytest.raises(IssueSourceError, match='public'):
        await IssueSources().request('gitlab', 'https://gitlab.example.com', 'synthetic-canary', '/api/v4/issues')
    assert not delivered


async def test_sr3_01_connection_uses_checked_address(monkeypatch):
    calls = []
    async def resolve(*args, **kwargs):
        calls.append(args)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34' if len(calls) == 1 else '127.0.0.1', 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    async def network(self, request):
        assert request.url.host == '93.184.216.34'
        assert request.headers['host'] == 'gitlab.example.com'
        assert request.extensions['sni_hostname'] == 'gitlab.example.com'
        return httpx.Response(200, json=[], request=request)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', network)
    await IssueSources().request('gitlab', 'https://gitlab.example.com', 'synthetic-canary', '/api/v4/issues')
    assert len(calls) == 1


async def test_sr3_02_provider_response_is_bounded():
    class Large(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(40):
                yield b' ' * 65536
            yield b'[]'
    transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=Large()))
    with pytest.raises(IssueSourceError, match='large'):
        await IssueSources(transport).request('gitlab', 'https://gitlab.example.com', 'synthetic-canary', '/api/v4/issues')


@pytest.mark.parametrize('command', [
    'gh api -X PUT repos/o/r/pulls/12/%6derge',
    'gh api -X PUT repos/o/r/pulls/12/../12/merge',
    "gh api https://api.github.com/graphql -f query='mutation { mergePullRequest(input:{pullRequestId:\"x\"}){clientMutationId}}'",
])
def test_sr3_03_merge_api_spellings_are_refused(command):
    policy = {'workspace': '/srv/ws', 'rules': {'merge_pr': True}, 'custom': []}
    verdict, error = guard.decide(policy, None, 'Bash', {'command': command}, '/srv/ws', '')
    assert error is None
    assert verdict and verdict[0] == 'merge_pr'


def test_sr3_05_ci_log_redacts_before_trimming():
    from acc.server.pulls import checks_message
    from acc.server.vault import Redactor
    secret = 'synthetic-canary-value-' * 20
    message, _ = checks_message([{'name': 'test', 'conclusion': 'failure', 'step': None,
        'log': 'connection credential ' + secret, 'summary': ''}], {'number': 12},
        'example/demo', 'acc/demo', 'alex', Redactor([(secret, 'demo key')]))
    assert 'synthetic-canary' not in message


async def test_sr3_01_mixed_public_private_dns_answers_are_refused(monkeypatch):
    async def resolve(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))
                for ip in ('93.184.216.34', '127.0.0.1')]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    from acc.server.issue_sources import PublicIssueTransport
    async with PublicIssueTransport() as transport:
        with pytest.raises(IssueSourceError, match='public'):
            await transport.handle_async_request(httpx.Request('GET', 'https://gitlab.example.com/api/v4/issues'))


async def test_sr3_02_compression_is_refused_before_decoding():
    class MustNotRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError('compressed body must not be read or decoded')
            yield b''
    transport = httpx.MockTransport(lambda r: httpx.Response(200, headers={'content-encoding': 'gzip'},
                                                           stream=MustNotRead()))
    with pytest.raises(IssueSourceError, match='uncompressed'):
        await IssueSources(transport).request('gitlab', 'https://gitlab.example.com', 'synthetic-canary', '/api/v4/issues')


async def test_sr3_02_total_deadline_closes_trickling_response(monkeypatch):
    from acc.server import issue_sources
    monkeypatch.setattr(issue_sources, 'REQUEST_TIMEOUT', .02, raising=False)
    closed = []
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(1000):
                await asyncio.sleep(.005)
                yield b' '
        async def aclose(self):
            closed.append(True)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, stream=Slow()))
    with pytest.raises(IssueSourceError, match='reach'):
        await asyncio.wait_for(IssueSources(transport).request('gitlab', 'https://gitlab.example.com',
                                                              'synthetic-canary', '/api/v4/issues'), .2)
    assert closed
