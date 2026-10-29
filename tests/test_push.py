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
