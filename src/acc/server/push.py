"""Web Push to the operator's devices (RFC 8030 delivery, RFC 8291 aes128gcm encryption, RFC 8292 VAPID).

ACC holds one P-256 key (data/secrets/vapid_private.pem). Each message is encrypted for one device, so the
push service (Apple, Google, Mozilla, Microsoft) relays it without being able to read it. Subscriptions may only
point at those services: a subscription can't make ACC post to an arbitrary address.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import http_ece
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

# Hosts of the browser vendors' push services. Anything else is refused at subscribe time.
PUSH_HOSTS = ("fcm.googleapis.com", "updates.push.services.mozilla.com", "push.services.mozilla.com")
PUSH_HOST_SUFFIXES = (".push.apple.com", ".notify.windows.com")


class PushError(ValueError):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def check_endpoint(endpoint: str) -> str:
    try:
        url = urlparse(endpoint)
        host = (url.hostname or "").lower()
    except ValueError as error:  # e.g. a malformed bracketed host
        raise PushError("not a browser push service endpoint") from error
    if url.scheme != "https" or not (host in PUSH_HOSTS or host.endswith(PUSH_HOST_SUFFIXES)):
        raise PushError("not a browser push service endpoint")
    return endpoint


class Push:
    def __init__(self, key_path: Path, subject: str, transport: httpx.AsyncBaseTransport | None = None):
        self.subject = subject  # a contact the push services may use: an https URL or mailto:
        self.key = self._load_or_create(key_path)
        self.client = httpx.AsyncClient(timeout=15, transport=transport)

    @staticmethod
    def _load_or_create(path: Path) -> ec.EllipticCurvePrivateKey:
        if path.exists():
            return serialization.load_pem_private_key(path.read_bytes(), password=None)
        key = ec.generate_private_key(ec.SECP256R1())
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
        os.chmod(path, 0o600)
        return key

    @property
    def public_key(self) -> str:
        """The application server key browsers subscribe with (uncompressed point, base64url)."""
        return b64url(self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                          serialization.PublicFormat.UncompressedPoint))

    def _vapid(self, endpoint: str) -> str:
        url = urlparse(endpoint)
        header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        claims = b64url(json.dumps({"aud": f"{url.scheme}://{url.netloc}", "exp": int(time.time()) + 12 * 3600,
                                    "sub": self.subject}, separators=(",", ":")).encode())
        r, s = decode_dss_signature(self.key.sign(f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
        token = f"{header}.{claims}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        return f"vapid t={token}, k={self.public_key}"

    def encrypt(self, p256dh: str, auth: str, payload: dict) -> bytes:
        return http_ece.encrypt(json.dumps(payload).encode(), private_key=ec.generate_private_key(ec.SECP256R1()),
                                dh=unb64url(p256dh), auth_secret=unb64url(auth), version="aes128gcm")

    async def send(self, endpoint: str, p256dh: str, auth: str, payload: dict, ttl: int = 6 * 3600,
                   urgency: str = "high") -> int:
        """Deliver one message; returns the push service's HTTP status (201 = accepted, 404/410 = gone)."""
        check_endpoint(endpoint)
        response = await self.client.post(endpoint, content=self.encrypt(p256dh, auth, payload), headers={
            "Authorization": self._vapid(endpoint), "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream", "TTL": str(ttl), "Urgency": urgency,
            # Topic: a newer message with the same topic replaces an undelivered older one (base64url chars, max 32)
            **({"Topic": re.sub(r"[^A-Za-z0-9_-]", "-", payload["tag"])[:32]} if payload.get("tag") else {}),
        })
        return response.status_code

    async def close(self) -> None:
        await self.client.aclose()


def link_for(item) -> str:
    """Where tapping a notification should land."""
    if item.session_id:
        return f"/sessions/{item.session_id}"
    return {"auth": "/providers", "publish_request": "/publishing", "host": "/host"}.get(item.kind, "/inbox")
