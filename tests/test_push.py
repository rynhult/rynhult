"""Web Push: encryption and VAPID are verified the way a browser and a push service would."""
from __future__ import annotations

import base64
import json
import os

import http_ece
import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from helpers import wait_for, write_db

from acc.server.models import AttentionItem
from acc.server.push import Push, PushError, check_endpoint, unb64url

ENDPOINT = "https://fcm.googleapis.com/fcm/send/device-token-1"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Browser:
    """A device: its own key pair and auth secret, as PushManager.subscribe() would create."""
    def __init__(self, endpoint=ENDPOINT):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.endpoint = endpoint

    def subscription(self) -> dict:
        public = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return {"endpoint": self.endpoint, "keys": {"p256dh": b64(public), "auth": b64(self.auth)}, "label": "Test phone"}

    def read(self, body: bytes) -> dict:
        return json.loads(http_ece.decrypt(body, private_key=self.key, auth_secret=self.auth, version="aes128gcm"))


class PushService:
    def __init__(self, status=201):
        self.received, self.status = [], status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.received.append(request)
        return httpx.Response(self.status)


def verify_vapid(request: httpx.Request, server_public: str) -> dict:
    scheme, _, params = request.headers["authorization"].partition(" ")
    fields = dict(part.strip().split("=", 1) for part in params.split(","))
    assert scheme == "vapid" and fields["k"] == server_public
    header, claims, signature = fields["t"].split(".")
    raw = unb64url(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64url(server_public))
    key.verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if forged
    return json.loads(unb64url(claims))


@pytest.fixture
def service(api, tmp_path):
    svc = PushService()
    supervisor = api.app.state.supervisor
    supervisor.push = Push(tmp_path / "vapid.pem", "https://acc.example.com", transport=httpx.MockTransport(svc.handler))
    return svc


def test_only_browser_push_services_are_allowed():
    for good in (ENDPOINT, "https://web.push.apple.com/abc", "https://updates.push.services.mozilla.com/wpush/v2/x"):
        assert check_endpoint(good) == good
    for bad in ("http://fcm.googleapis.com/x", "https://evil.example.com/x", "https://fcm.googleapis.com.evil.io/x",
                "https://localhost/x", "https://10.0.0.1/x"):
        with pytest.raises(PushError):
            check_endpoint(bad)


async def test_subscribe_then_test_message_is_encrypted_and_signed(api, service):
    browser = Browser()
    state = (await api.get("/api/push")).json()
    assert state["devices"] == [] and len(unb64url(state["public_key"])) == 65
    assert (await api.post("/api/push/subscribe", json=browser.subscription())).status_code == 201
    assert (await api.post("/api/push/subscribe", json=browser.subscription())).status_code == 201  # idempotent
    assert len((await api.get("/api/push")).json()["devices"]) == 1
    sent = await api.post("/api/push/test")
    assert sent.status_code == 200 and sent.json()["delivered"] == 1
    request = service.received[0]
    assert request.headers["content-encoding"] == "aes128gcm" and int(request.headers["ttl"]) > 0
    claims = verify_vapid(request, state["public_key"])
    assert claims["aud"] == "https://fcm.googleapis.com" and claims["sub"] == "https://acc.example.com"
    payload = browser.read(request.content)
    assert payload["title"] == "Rynhult test notification"
    # Settings opens one section at a time; the test comes from Notification devices, so it opens there.
    assert payload["url"] == "/settings#notification-devices"
    assert request.headers['topic'] != payload['tag']
    assert b'Rynhult test notification' not in request.content
    refused = await api.post("/api/push/subscribe", json={**browser.subscription(), "endpoint": "https://evil.example.com/x"})
    assert refused.status_code == 422


async def test_new_inbox_items_notify_and_gone_devices_are_dropped(api, service):
    browser = Browser()
    await api.post("/api/push/subscribe", json=browser.subscription())
    supervisor = api.app.state.supervisor
    await wait_for(lambda: _cursor_set(supervisor))  # the running supervisor loop owns the cursor (starts at the newest item)
    assert service.received == []
    with write_db(supervisor.db) as s:
        s.add(AttentionItem(dedupe_key="needs_input:abc", kind="needs_input", severity="warn", source="claude_hook",
                            title="api-refactor needs your input", detail="Bash: npm test", session_id=None))
        s.add(AttentionItem(dedupe_key="info:x", kind="crash_recovered", severity="info", source="tmux", title="quiet"))
        s.commit()
    await wait_for(lambda: _received(service))
    message = browser.read(service.received[0].content)
    assert message["title"] == "api-refactor needs your input" and message["url"] == "/inbox"
    assert len(service.received) == 1  # info items don't notify
    service.status = 410  # the device unsubscribed at the push service
    assert await supervisor.notify({"title": "x"}) == 0
    assert (await api.get("/api/push")).json()["devices"] == []


async def _received(service):
    return service.received or None


async def _cursor_set(supervisor):
    return supervisor._push_cursor is not None or None


async def test_remove_and_unsubscribe(api, service):
    browser = Browser()
    device = (await api.post("/api/push/subscribe", json=browser.subscription())).json()
    assert (await api.post(f"/api/push/devices/{device['id']}/remove")).status_code == 200
    await api.post("/api/push/subscribe", json=browser.subscription())
    assert (await api.post("/api/push/unsubscribe", json={"endpoint": ENDPOINT})).status_code == 200
    assert (await api.get("/api/push")).json()["devices"] == []
    assert (await api.post("/api/push/test")).status_code == 409


async def test_subscription_create_rotation_and_transfer_have_safe_audit_targets(api):
    from types import SimpleNamespace

    browser = Browser()
    first = browser.subscription()
    created = (await api.post("/api/push/subscribe", json=first)).json()
    assert "id" in created
    await api.post("/api/push/subscribe", json=first)  # identical subscribe is not another mutation
    rotated = Browser().subscription()
    assert (await api.post("/api/push/subscribe", json=rotated)).status_code == 201
    ctx = api.app.state.ctx
    api.app.dependency_overrides[ctx.principal] = lambda: SimpleNamespace(username="blair")
    try:
        moved = (await api.post("/api/push/subscribe", json=rotated)).json()
        assert moved["id"] == created["id"]
        assert len((await api.get("/api/push")).json()["devices"]) == 1
    finally:
        api.app.dependency_overrides.clear()
    assert (await api.get("/api/push")).json()["devices"] == []
    entries = (await api.get("/api/audit?action=push.")).json()[::-1]
    assert [e["action"] for e in entries] == ["push.subscribe", "push.update", "push.update"]
    assert [e["actor"] for e in entries] == ["@api", "@api", "blair"]
    assert all(e["target"] == str(created["id"]) for e in entries)
    assert entries[1]["detail"] == {"ownership_changed": False, "keys_changed": True}
    assert entries[2]["detail"] == {"ownership_changed": True, "keys_changed": False}
    text = json.dumps(entries)
    for body in (first, rotated):
        assert body["endpoint"] not in text
        assert all(value not in text for value in body["keys"].values())


@pytest.mark.parametrize('ending', ['hidden', 'idle', 'expired'])
async def test_presence_defers_until_away_and_survives_reconnect(api, env, service, monkeypatch, ending):
    from helpers import pause_supervisor
    from test_auth import cookie_client, do_setup
    from test_users import csrf

    from acc.server.models import Setting
    from acc.server.push_presence import Presence

    await do_setup(api, env)
    client = cookie_client(api)
    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    browser = Browser()
    await client.post('/api/push/subscribe', json=browser.subscription(), headers=csrf(client))
    await supervisor.push_attention()
    person = (await client.get('/api/auth/state')).json()['username']
    body = {'tab': 'one', 'scope': '/inbox', 'idle_ms': 0, 'active': True}
    assert (await client.post('/api/push/presence', json=body, headers=csrf(client))).status_code == 200
    with write_db(supervisor.db) as db:
        item = AttentionItem(dedupe_key='presence:test', kind='needs_input', severity='warn', source='test',
                             title='Private prompt', owner=person)
        db.add(item)
        db.commit()
    await supervisor.push_attention()
    assert not service.received
    # Reconstruct ephemeral state as on a server restart; the pending recipient remains durable.
    supervisor._push_cursor = None
    if ending == 'expired':
        supervisor.push_presence = Presence()
    else:
        body.update(active=ending != 'hidden', idle_ms=120000 if ending == 'idle' else 0)
        assert (await client.post('/api/push/presence', json=body, headers=csrf(client))).status_code == 200
    await supervisor.push_attention()
    assert len(service.received) == 1
    assert browser.read(service.received[0].content)['title'] == '1 item still needs you'
    # New lease/reconnect never requeues a claimed delivery.
    body.update(active=True, idle_ms=0)
    await client.post('/api/push/presence', json=body, headers=csrf(client))
    await supervisor.push_attention()
    supervisor.push_presence = Presence()
    await supervisor.push_attention()
    assert len(service.received) == 1
    assert (await client.post('/api/push/preference', json={'always': True}, headers=csrf(client))).status_code == 200
    assert (await client.get('/api/push')).json()['always'] is True
    with supervisor.db() as db:
        assert db.get(Setting, 'push.delivery').value['pending'] == []


@pytest.mark.parametrize('case', ['other-person', 'other-session', 'always', 'critical', 'answered'])
async def test_presence_recipient_scope_and_bypasses(api, service, case):
    from helpers import pause_supervisor

    from acc.server.models import API_PRINCIPAL, Setting

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    await api.post('/api/push/subscribe', json=Browser().subscription())
    await supervisor.push_attention()
    supervisor.push_presence.update('someone-else' if case == 'other-person' else API_PRINCIPAL,
                                    'login', 'tab', '/sessions/elsewhere' if case == 'other-session' else '/inbox', 0, True)
    with write_db(supervisor.db) as db:
        if case == 'always':
            db.merge(Setting(key=f'push.active.{API_PRINCIPAL}', value={'always': True}))
        db.add(AttentionItem(dedupe_key='presence:scope', kind='needs_input', source='test',
                             severity='critical' if case == 'critical' else 'warn', title='Prompt'))
        db.commit()
    await supervisor.push_attention()
    if case == 'answered':
        assert not service.received
        with write_db(supervisor.db) as db:
            from acc.server.attention import now
            item = db.query(AttentionItem).filter_by(dedupe_key='presence:scope').one()
            item.acked_at = now()
            db.commit()
        supervisor.push_presence.tabs.clear()
        await supervisor.push_attention()
        assert not service.received
    else:
        assert len(service.received) == 1


async def test_installation_token_cannot_set_presence_or_preference(api):
    assert (await api.post('/api/push/presence', json={
        'tab': 'token', 'scope': '/inbox', 'idle_ms': 0, 'active': True})).status_code == 403
    assert (await api.post('/api/push/preference', json={'always': True})).status_code == 403


def test_presence_multiple_devices_and_idle_deadline(monkeypatch):
    from acc.server.push_presence import Presence

    clock = [10.0]
    monkeypatch.setattr('acc.server.push_presence.time.monotonic', lambda: clock[0])
    presence = Presence()
    presence.update('alex', 'login-one', 'tab', '/sessions/s1', 110000, True)
    presence.update('alex', 'login-two', 'tab', '/inbox', 0, True)
    presence.update('alex', 'login-one', 'tab', '/sessions/s1', 0, False)
    assert presence.watching('alex', 's1')
    assert not presence.watching('blair', 's1')
    clock[0] += 46
    assert not presence.watching('alex', 's1')


def test_delayed_active_heartbeat_cannot_undo_hidden_tab():
    from acc.server.push_presence import Presence

    presence = Presence()
    presence.update('alex', 'login', 'tab', '/inbox', 0, True, 1)
    presence.update('alex', 'login', 'tab', '/inbox', 0, False, 3)
    presence.update('alex', 'login', 'tab', '/inbox', 0, True, 2)
    assert not presence.watching('alex', None)


@pytest.mark.parametrize('held,count', [(True, 40), (False, 100)])
async def test_burst_becomes_one_summary_without_reconnect_replay(api, service, held, count):
    from helpers import pause_supervisor

    from acc.server.models import API_PRINCIPAL

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    await api.post('/api/push/subscribe', json=Browser().subscription())
    await supervisor.push_attention()
    browser = Browser('https://fcm.googleapis.com/fcm/send/summary-test')
    await api.post('/api/push/unsubscribe', json={'endpoint': ENDPOINT})
    await api.post('/api/push/subscribe', json=browser.subscription())
    if held:
        supervisor.push_presence.update(API_PRINCIPAL, 'login', 'tab', '/inbox', 0, True)
    with write_db(supervisor.db) as db:
        db.add_all([AttentionItem(dedupe_key=f'tool:burst-{n}', kind='tool_request', severity='warn', source='test',
                                 title=f'Prompt {n}') for n in range(count)])
        db.commit()
    await supervisor.push_attention()
    if held:
        assert not service.received
        supervisor.push_presence.update(API_PRINCIPAL, 'login', 'tab', '/inbox', 0, False)
        await supervisor.push_attention()
    assert len(service.received) == 1
    message = browser.read(service.received[0].content)
    assert message['title'] == f'{count} items still need you'
    assert message['url'] == '/inbox'
    supervisor._push_cursor = None
    await supervisor.push_attention()
    await supervisor.push_attention()
    assert len(service.received) == 1
    from acc.server.models import Setting
    with supervisor.db() as db:
        state = db.get(Setting, 'push.delivery').value
        assert state['pending'] == state['inflight'] == []
        assert db.query(AttentionItem).filter(AttentionItem.dedupe_key.startswith('tool:burst-'),
                                             AttentionItem.resolved_at.is_(None)).count() == count


@pytest.mark.parametrize('failure', [503, 429, 'timeout'])
async def test_inflight_retries_after_lease_then_stops_on_accept(api, service, monkeypatch, failure):
    from helpers import pause_supervisor

    from acc.server.models import Setting

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    await api.post('/api/push/subscribe', json=Browser().subscription())
    await supervisor.push_attention()
    clock = [1000.0]
    monkeypatch.setattr('acc.server.polling.time.time', lambda: clock[0])
    if failure == 'timeout':
        def timeout(request):
            service.received.append(request)
            raise httpx.ReadTimeout('test timeout', request=request)
        supervisor.push.client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    else:
        service.status = failure
    with write_db(supervisor.db) as db:
        db.add(AttentionItem(dedupe_key='retry:test', kind='needs_input', severity='warn', source='test', title='Prompt'))
        db.commit()
    await supervisor.push_attention()
    with supervisor.db() as db:
        claim = db.get(Setting, 'push.delivery').value['inflight'][0]
        assert claim['claimed_at'] == 1000 and claim['attempts'] == 1
    supervisor._push_cursor = None  # restarted poller uses persisted inflight state
    clock[0] += 119
    await supervisor.push_attention()
    assert len(service.received) == 1
    service.status = 201
    supervisor.push.client = httpx.AsyncClient(transport=httpx.MockTransport(service.handler))
    clock[0] += 2
    await supervisor.push_attention()
    assert len(service.received) == 2
    with supervisor.db() as db:
        assert db.get(Setting, 'push.delivery').value['inflight'] == []
    clock[0] += 200
    await supervisor.push_attention()
    assert len(service.received) == 2


async def test_inflight_exhaustion_warns_and_drops_after_three_attempts(api, service, monkeypatch, caplog):
    from helpers import pause_supervisor

    from acc.server.models import Setting

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    await api.post('/api/push/subscribe', json=Browser().subscription())
    await supervisor.push_attention()
    clock = [1000.0]
    monkeypatch.setattr('acc.server.polling.time.time', lambda: clock[0])
    service.status = 503
    with write_db(supervisor.db) as db:
        db.add(AttentionItem(dedupe_key='retry:exhaust', kind='needs_input', severity='warn', source='test', title='Prompt'))
        db.commit()
    for attempt in range(3):
        await supervisor.push_attention()
        with supervisor.db() as db:
            assert db.get(Setting, 'push.delivery').value['inflight'][0]['attempts'] == attempt + 1
        clock[0] += 121
    await supervisor.push_attention()
    assert len(service.received) == 3
    assert 'exhausted after 3 attempts' in caplog.text
    with supervisor.db() as db:
        assert db.get(Setting, 'push.delivery').value['inflight'] == []


async def test_first_device_accept_clears_claim_before_second_device(api, service):
    from helpers import pause_supervisor

    from acc.server.models import Setting

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    await api.post('/api/push/subscribe', json=Browser().subscription())
    await api.post('/api/push/subscribe', json=Browser(ENDPOINT + '-two').subscription())
    await supervisor.push_attention()
    def relay(request):
        with supervisor.db() as db:
            inflight = db.get(Setting, 'push.delivery').value['inflight']
            assert bool(inflight) == (not service.received)
        service.received.append(request)
        return httpx.Response(201 if len(service.received) == 1 else 503)
    supervisor.push.client = httpx.AsyncClient(transport=httpx.MockTransport(relay))
    with write_db(supervisor.db) as db:
        db.add(AttentionItem(dedupe_key='accept:first', kind='needs_input', severity='warn', source='test', title='Prompt'))
        db.commit()
    await supervisor.push_attention()
    assert len(service.received) == 2
    with supervisor.db() as db:
        assert db.get(Setting, 'push.delivery').value['inflight'] == []


@pytest.mark.parametrize('kind,key', [('sign_in', 'policy:warn'), ('backup', 'backup:test'),
                                      ('machine_offline', 'machine_offline:node')])
async def test_security_backup_and_persistent_alerts_bypass_presence(api, service, kind, key):
    from helpers import pause_supervisor

    from acc.server.models import API_PRINCIPAL

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    await api.post('/api/push/subscribe', json=Browser().subscription())
    await supervisor.push_attention()
    supervisor.push_presence.update(API_PRINCIPAL, 'login', 'tab', '/inbox', 0, True)
    with write_db(supervisor.db) as db:
        db.add(AttentionItem(dedupe_key=key, kind=kind, severity='warn', source='test', title='Security alert'))
        db.commit()
    await supervisor.push_attention()
    assert len(service.received) == 1


def test_presence_cap_and_person_throttle(monkeypatch):
    from acc.server.push_presence import Presence

    clock = [100.0]
    monkeypatch.setattr('acc.server.push_presence.time.monotonic', lambda: clock[0])
    presence = Presence()
    for index in range(30):
        assert presence.update('alex', 'login', str(index), '/inbox', 0, True)
    assert len(presence.tabs) == 16
    assert ('alex', 'login', '0') not in presence.tabs
    assert not presence.update('alex', 'second-login', 'tab', '/inbox', 0, True)
    assert presence.update('blair', 'other-login', 'tab', '/inbox', 0, True)
    clock[0] += 16
    assert presence.update('alex', 'second-login', 'tab', '/inbox', 0, True)
    assert len([key for key in presence.tabs if key[:2] == ('alex', 'login')]) == 16


def test_presence_concurrent_heartbeats_and_reads():
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from acc.server.push_presence import Presence

    presence = Presence()
    start = threading.Barrier(2)
    def heartbeat():
        start.wait()
        for index in range(4000):
            presence.update(f'person-{index // 16}', 'login', str(index), '/inbox', 0, True)
    def poll():
        start.wait()
        for _ in range(4000):
            presence.watching('person-0', None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(heartbeat), pool.submit(poll)]
        for job in jobs:
            job.result(timeout=20)
    assert presence.watching('person-249', None)


async def test_topics_are_install_keyed_and_stable_across_restart(tmp_path):
    service = PushService()
    browser = Browser()
    subscription = browser.subscription()
    async def send(key):
        push = Push(key, 'https://example.com', transport=httpx.MockTransport(service.handler))
        await push.send(browser.endpoint, **subscription['keys'], payload={'title': 'private', 'tag': 'host:memory'})
        await push.close()
        return service.received[-1].headers['topic']
    first = await send(tmp_path / 'first.pem')
    assert first == await send(tmp_path / 'first.pem')
    assert first != await send(tmp_path / 'second.pem')
    import hashlib
    assert first != b64(hashlib.sha256(b'host:memory').digest())[:32]


async def test_critical_items_stay_individual_next_to_ordinary_summary(api, service):
    from helpers import pause_supervisor

    from acc.server.models import API_PRINCIPAL

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    browser = Browser()
    await api.post('/api/push/subscribe', json=browser.subscription())
    await supervisor.push_attention()
    supervisor.push_presence.update(API_PRINCIPAL, 'login', 'tab', '/inbox', 0, True)
    with write_db(supervisor.db) as db:
        db.add_all([AttentionItem(dedupe_key=f'held:{i}', kind='needs_input', severity='warn', source='test', title='Held')
                    for i in range(4)])
        db.add_all([AttentionItem(dedupe_key=f'urgent:{i}', kind='needs_input', severity='critical', source='test',
                                 title=f'Urgent {i}') for i in range(5)])
        db.commit()
    await supervisor.push_attention()
    assert len(service.received) == 5
    assert all(browser.read(request.content)['critical'] for request in service.received)
    supervisor.push_presence.update(API_PRINCIPAL, 'login', 'tab', '/inbox', 0, False)
    await supervisor.push_attention()
    assert len(service.received) == 6
    assert browser.read(service.received[-1].content)['title'] == '4 items still need you'


async def test_heartbeat_endpoint_throttles_per_person(api, env):
    from helpers import pause_supervisor
    from test_auth import cookie_client, do_setup
    from test_users import csrf

    await do_setup(api, env)
    await pause_supervisor(api)
    client = cookie_client(api)
    body = {'tab': 'tab', 'scope': '/inbox', 'idle_ms': 0, 'active': True}
    for _ in range(30):
        assert (await client.post('/api/push/presence', json=body, headers=csrf(client))).status_code == 200
    response = await client.post('/api/push/presence', json=body, headers=csrf(client))
    assert response.status_code == 429 and response.headers['retry-after'] == '15'
    person = (await client.get('/api/auth/state')).json()['username']
    presence = api.app.state.supervisor.push_presence
    assert presence.watching(person, None)
    body['active'] = False
    assert (await client.post('/api/push/presence', json=body, headers=csrf(client))).status_code == 200
    assert not presence.watching(person, None)
    body['active'] = True
    assert (await client.post('/api/push/presence', json=body, headers=csrf(client))).status_code == 429
    assert not presence.watching(person, None)


@pytest.mark.parametrize('change', ['acknowledged', 'ownership'])
async def test_retry_rechecks_item_authority_and_answer(api, service, monkeypatch, change):
    from helpers import pause_supervisor

    from acc.server.attention import now
    from acc.server.models import Setting

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    await api.post('/api/push/subscribe', json=Browser().subscription())
    await supervisor.push_attention()
    clock = [1000.0]
    monkeypatch.setattr('acc.server.polling.time.time', lambda: clock[0])
    service.status = 503
    with write_db(supervisor.db) as db:
        item = AttentionItem(dedupe_key='retry:recheck', kind='needs_input', severity='warn', source='test', title='Prompt')
        db.add(item)
        db.commit()
    await supervisor.push_attention()
    with write_db(supervisor.db) as db:
        item = db.query(AttentionItem).filter_by(dedupe_key='retry:recheck').one()
        if change == 'acknowledged':
            item.acked_at = now()
        else:
            item.owner = 'another-person'
        db.commit()
    clock[0] += 121
    await supervisor.push_attention()
    assert len(service.received) == 1
    with supervisor.db() as db:
        assert db.get(Setting, 'push.delivery').value['inflight'] == []


async def test_crash_after_claim_requeues_summary_as_one_push(api, service, monkeypatch):
    import asyncio

    from helpers import pause_supervisor

    from acc.server.models import Setting

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    browser = Browser()
    await api.post('/api/push/subscribe', json=browser.subscription())
    await supervisor.push_attention()
    clock = [1000.0]
    monkeypatch.setattr('acc.server.polling.time.time', lambda: clock[0])
    with write_db(supervisor.db) as db:
        db.add_all([AttentionItem(dedupe_key=f'crash:{i}', kind='tool_request', severity='warn', source='test',
                                 title='Tool approval') for i in range(100)])
        db.commit()
    real_notify = supervisor.notify
    async def crash(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(supervisor, 'notify', crash)
    with pytest.raises(asyncio.CancelledError):
        await supervisor.push_attention()
    with supervisor.db() as db:
        claim = db.get(Setting, 'push.delivery').value['inflight'][0]
        assert len(claim['items']) == 100 and claim['attempts'] == 1
    monkeypatch.setattr(supervisor, 'notify', real_notify)
    clock[0] += 121
    await supervisor.push_attention()
    await supervisor.push_attention()
    assert len(service.received) == 1
    assert browser.read(service.received[0].content)['title'] == '100 items still need you'


@pytest.mark.parametrize('ending', ['hidden', 'idle', 'other-page'])
def test_release_updates_bypass_throttle_and_keep_sequence_fencing(ending):
    from acc.server.push_presence import Presence

    presence = Presence()
    for sequence in range(1, 31):
        assert presence.update('alex', 'login', 'tab', '/inbox', 0, True, sequence)
    assert not presence.update('alex', 'login', 'tab', '/inbox', 0, True, 31)
    assert presence.watching('alex', None)
    assert presence.update('alex', 'login', 'tab', '/settings' if ending == 'other-page' else '/inbox',
                           120000 if ending == 'idle' else 0, ending != 'hidden', 32)
    assert not presence.watching('alex', None)
    # An out-of-order leave cannot clear a newer active lease, once the budget resets.
    presence = Presence()
    assert presence.update('alex', 'login', 'tab', '/inbox', 0, True, 3)
    assert presence.update('alex', 'login', 'tab', '/inbox', 0, False, 2)
    assert presence.watching('alex', None)


@pytest.mark.parametrize('count', [5, 6, 40])
async def test_critical_overflow_is_one_summary_per_person(api, service, count):
    from helpers import pause_supervisor

    from acc.server.models import PushSubscription, Setting

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    browser, other = Browser(), Browser(ENDPOINT + '-other-person')
    await api.post('/api/push/subscribe', json=browser.subscription())
    await supervisor.push_attention()
    with write_db(supervisor.db) as db:
        sub = other.subscription()
        db.add(PushSubscription(endpoint=sub['endpoint'], **sub['keys'], username='blair', label='Other person'))
        db.add_all([AttentionItem(dedupe_key=f'critical:{i}', kind='needs_input', severity='critical', source='test',
                                 title=f'Critical {i}') for i in range(count)])
        db.add_all([AttentionItem(dedupe_key=f'other-critical:{i}', kind='needs_input', severity='critical', source='test',
                                 title=f'Other critical {i}', owner='blair') for i in range(2)])
        db.commit()
    await supervisor.push_attention()
    mine = [browser.read(request.content) for request in service.received if str(request.url) == browser.endpoint]
    theirs = [other.read(request.content) for request in service.received if str(request.url) == other.endpoint]
    assert len(mine) == min(5, count) + (count > 5)
    assert len(theirs) == 2 and all(message['tag'].startswith('other-critical:') for message in theirs)
    assert all(message['critical'] for message in mine + theirs)
    assert len([message for message in mine if message['tag'].startswith('critical:')]) == 5
    if count > 5:
        summary = next(message for message in mine if message['tag'] == 'critical-summary')
        assert summary['url'] == '/inbox'
        assert summary['title'] == ('1 critical item still needs you' if count == 6 else '35 critical items still need you')
    await supervisor.push_attention()
    assert len(service.received) == len(mine) + len(theirs)
    with supervisor.db() as db:
        assert db.get(Setting, 'push.delivery').value['inflight'] == []


@pytest.mark.parametrize('severity', ['warn', 'critical'])
async def test_exempt_security_titles_never_join_held_or_critical_summaries(api, service, severity):
    from helpers import pause_supervisor

    from acc.server.models import API_PRINCIPAL

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    browser = Browser()
    await api.post('/api/push/subscribe', json=browser.subscription())
    await supervisor.push_attention()
    supervisor.push_presence.update(API_PRINCIPAL, 'login', 'tab', '/inbox', 0, True)
    with write_db(supervisor.db) as db:
        db.add_all([AttentionItem(dedupe_key=f'held-security:{i}', kind='tool_request', severity='warn', source='test',
                                 title='Tool request') for i in range(4)])
        db.commit()
    await supervisor.push_attention()
    assert not service.received
    supervisor.push_presence.update(API_PRINCIPAL, 'login', 'tab', '/inbox', 0, False)
    security = [('sign_in', 'sign-in'), ('backup', 'backup'), ('machine_offline', 'machine_offline:')]
    with write_db(supervisor.db) as db:
        for index in range(6):
            kind, key = security[index % 3]
            db.add(AttentionItem(dedupe_key=f'{key}{index}', kind=kind, severity=severity, source='test',
                                 title=f'Security alert {index}'))
        db.commit()
    await supervisor.push_attention()
    messages = [browser.read(request.content) for request in service.received]
    assert len(messages) == 7
    assert {message['title'] for message in messages} == {
        '4 items still need you', *(f'Security alert {index}' for index in range(6))}
    assert not any(message['tag'] == 'critical-summary' for message in messages)


async def test_critical_overflow_retry_stays_a_singular_critical_summary(api, service, monkeypatch):
    from helpers import pause_supervisor

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    browser = Browser()
    await api.post('/api/push/subscribe', json=browser.subscription())
    await supervisor.push_attention()
    clock = [1000.0]
    monkeypatch.setattr('acc.server.polling.time.time', lambda: clock[0])
    def relay(request):
        message = browser.read(request.content)
        service.received.append(request)
        return httpx.Response(503 if message['tag'] == 'critical-summary' else 201)
    supervisor.push.client = httpx.AsyncClient(transport=httpx.MockTransport(relay))
    with write_db(supervisor.db) as db:
        db.add_all([AttentionItem(dedupe_key=f'retry-critical:{i}', kind='needs_input', severity='critical', source='test',
                                 title='Urgent') for i in range(6)])
        db.commit()
    await supervisor.push_attention()
    assert len(service.received) == 6
    clock[0] += 121
    supervisor.push.client = httpx.AsyncClient(transport=httpx.MockTransport(service.handler))
    await supervisor.push_attention()
    assert len(service.received) == 7
    message = browser.read(service.received[-1].content)
    assert message == {'title': '1 critical item still needs you', 'url': '/inbox',
                       'tag': 'critical-summary', 'critical': True}
    clock[0] += 121
    await supervisor.push_attention()
    assert len(service.received) == 7


@pytest.mark.parametrize('kind,key', [('machine_offline', 'machine_offline:demo-cloud'),
                                      ('backup', 'backup:failed:local'), ('sign_in', 'sign-in:policy')])
async def test_owner_security_broadcast_bypasses_presence_and_excludes_members(api, service, kind, key):
    from helpers import pause_supervisor

    from acc.server.models import API_PRINCIPAL, PushSubscription, Setting, User

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    owner, member = Browser(), Browser(ENDPOINT + '-member')
    await api.post('/api/push/subscribe', json=owner.subscription())
    with write_db(supervisor.db) as db:
        db.add(User(username='blair', role='member', account='blair-agent', password_hash='unused', totp_secret='unused'))
        sub = member.subscription()
        db.add(PushSubscription(endpoint=sub['endpoint'], **sub['keys'], username='blair', label='Member'))
        db.commit()
    await supervisor.push_attention()
    supervisor.push_presence.update(API_PRINCIPAL, 'login', 'tab', '/inbox', 0, True)
    with write_db(supervisor.db) as db:
        db.add(AttentionItem(dedupe_key=key, kind=kind, severity='warn', source='test', title='Security notice'))
        db.commit()
    await supervisor.push_attention()
    assert len(service.received) == 1
    assert str(service.received[0].url) == owner.endpoint
    assert owner.read(service.received[0].content)['title'] == 'Security notice'
    await supervisor.push_attention()
    assert len(service.received) == 1
    with supervisor.db() as db:
        assert db.get(Setting, 'push.delivery').value['inflight'] == []


async def test_owner_broadcast_retry_is_not_acknowledged_by_concurrent_test_send(api, service, monkeypatch):
    import asyncio

    from helpers import pause_supervisor

    from acc.server.models import API_PRINCIPAL, Setting

    await pause_supervisor(api)
    supervisor = api.app.state.supervisor
    browser = Browser()
    await api.post('/api/push/subscribe', json=browser.subscription())
    await supervisor.push_attention()
    clock = [1000.0]
    monkeypatch.setattr('acc.server.polling.time.time', lambda: clock[0])
    with write_db(supervisor.db) as db:
        db.add(AttentionItem(dedupe_key='machine_offline:concurrent', kind='machine_offline', severity='warn',
                             source='test', title='Machine offline'))
        db.commit()
    started, release = asyncio.Event(), asyncio.Event()
    async def relay(request):
        payload = browser.read(request.content)
        service.received.append(request)
        if payload['title'] == 'Machine offline':
            started.set()
            await release.wait()
            return httpx.Response(503)
        return httpx.Response(201)
    await supervisor.push.client.aclose()
    supervisor.push.client = httpx.AsyncClient(transport=httpx.MockTransport(relay))
    delivery = asyncio.create_task(supervisor.push_attention())
    try:
        await asyncio.wait_for(started.wait(), timeout=10)
        assert await supervisor.notify({'title': 'Direct test'}, username=API_PRINCIPAL) == 1
        with supervisor.db() as db:
            claims = db.get(Setting, 'push.delivery').value['inflight']
            assert len(claims) == 1 and claims[0]['person'] is None
    finally:
        release.set()
        await delivery
    await supervisor.push.client.aclose()
    supervisor.push.client = httpx.AsyncClient(transport=httpx.MockTransport(service.handler))
    clock[0] += 121
    await supervisor.push_attention()
    assert len(service.received) == 3  # failed broadcast, unrelated test, retried broadcast
    with supervisor.db() as db:
        assert db.get(Setting, 'push.delivery').value['inflight'] == []


async def test_auth_presence_requires_this_person_push_device(api, env, service):
    from test_auth import cookie_client, do_setup
    from test_users import csrf

    await do_setup(api, env)
    assert (await api.get('/api/auth/state')).json()['push_presence_enabled'] is False
    # Token-owned devices do not enable a different cookie person's heartbeat.
    assert (await api.post('/api/push/subscribe', json=Browser().subscription())).status_code == 201
    client = cookie_client(api)
    assert (await client.get('/api/auth/state')).json()['push_presence_enabled'] is False
    browser = Browser(ENDPOINT + '-person')
    response = await client.post('/api/push/subscribe', json=browser.subscription(), headers=csrf(client))
    assert response.status_code == 201
    assert (await client.get('/api/auth/state')).json()['push_presence_enabled'] is True
    await client.post('/api/push/unsubscribe', json={'endpoint': browser.endpoint}, headers=csrf(client))
    assert (await client.get('/api/auth/state')).json()['push_presence_enabled'] is False
    await client.aclose()
