"""Exercise unchanged public connection caps against one actual native owner."""
import httpx


async def qualify_public_capacity(app, cookie_name, token, csrf, login, call, sid, *, global_limit, baseline):
    clients, identifiers, attachments = [], [], []
    maximum = 512 if global_limit else 16
    try:
        accepted = 0
        for index in range(maximum + 1):
            if global_limit and index % 16 == 0:
                active_token, active_csrf = login()
            elif not global_limit:
                active_token, active_csrf = token, csrf
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
                headers={'Origin': 'http://fixture', 'X-ACC-CSRF': active_csrf}, cookies={cookie_name: active_token})
            clients.append(client)
            identifier = f'capacity-observer-{index}'
            reply = await call(client, 'initialize', {'major': 1, 'minor': 0, 'features': ['terminal'], 'clientId': identifier})
            if 'error' in reply:
                assert reply['error']['data']['code'] == 'LIMIT', reply
                break
            client.headers['X-ACC-Relay-Connection'] = reply['result']['connectionToken']
            attached = await call(client, 'session.attach', {'clientId': identifier, 'connectionSeq': 1})
            assert 'result' in attached, attached
            identifiers.append(identifier)
            attachments.append(attached['result']['attachmentId'])
            accepted += 1
        assert accepted + baseline == maximum, (accepted, baseline, maximum)
        # A real detach reclaims a slot; no timer/eviction or owner restart.
        detached = await call(clients[0], 'session.detach', {'attachmentId': attachments[0], 'connectionSeq': 2})
        assert 'result' in detached, detached
        assert detached['result']['detached']
        reply = await call(clients[0], 'initialize', {'major': 1, 'minor': 0, 'features': ['terminal'], 'clientId': 'recovered-capacity'})
        assert 'result' in reply, reply
        clients[0].headers['X-ACC-Relay-Connection'] = reply['result']['connectionToken']
        attached = (await call(clients[0], 'session.attach', {'clientId': 'recovered-capacity', 'connectionSeq': 1}))['result']
        response = await clients[0].get(f'/api/sessions/{sid}/relay/terminal', params={
            'attachmentId': attached['attachmentId'], 'expectedRunId': attached['snapshot']['runId']})
        assert response.status_code == 200, response.text
        # Reconnecting the same observer at capacity replaces its old token.
        previous = clients[0].headers['X-ACC-Relay-Connection']
        reply = await call(clients[0], 'initialize', {'major': 1, 'minor': 0, 'features': ['terminal'], 'clientId': 'recovered-capacity'})
        assert 'result' in reply, reply
        assert 'error' in await call(clients[0], 'session.snapshot', {})
        assert clients[0].headers['X-ACC-Relay-Connection'] == previous
        clients[0].headers['X-ACC-Relay-Connection'] = reply['result']['connectionToken']
        attached = (await call(clients[0], 'session.attach', {'clientId': 'recovered-capacity', 'connectionSeq': 1}))['result']
        assert attached['snapshot']['terminalMediated'] is False
        return {'capacity': maximum, 'baseline': baseline, 'additional': accepted,
            'detach_recovers': True, 'same_client_replaces_at_capacity': True}
    finally:
        for client in clients:
            await client.aclose()
