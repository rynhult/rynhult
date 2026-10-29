"""Record the Android enrollment client's JVM test fixtures from the real server code, in process.

Run from a checkout of the server branch (mobile-phase1-server-review, or main once it merges), never against a
running Rynhult:

    cp mobile/android/tools/record_fixtures.py <server checkout>/tests/test_zz_record_android.py
    cd <server checkout>
    ANDROID_FIXTURES=<this repo>/mobile/android/enrollment-core/src/test/resources/fixtures \\
        PYTHONPATH=src python -m pytest -q -p no:cacheprovider tests/test_zz_record_android.py
    rm tests/test_zz_record_android.py

It uses the server's own test fixtures: a throwaway test device CA, the documented edge boundary simulation and an
ASGI transport. The device keys are derived from fixed test labels (KEY_LABELS) so the Kotlin tests can rebuild
them; they are test-only and never valid anywhere else. Nothing here reaches a network or a real signer.
"""
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from helpers import write_db
from sqlalchemy import select
from test_mobile import PREFIX, activate, mobile  # noqa: F401 (fixture)

from acc.server import auth, devices
from acc.server.models import AuthSession, User

KEY_LABELS = ("rynhult android fixture key 1", "rynhult android fixture key 2")
INSTALL = "0123456789abcdef0123456789abcdef"
ORIGIN = "https://acc.example.lan"


def key(label):
    scalar = int.from_bytes(hashlib.sha256(label.encode()).digest(), "big")
    return ec.derive_private_key(scalar, ec.SECP256R1())


def csr_for(private):
    return (x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "rynhult-android")]))
            .sign(private, hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode())


def claim_body(start, private):
    pairing, token = start["link"].split("#", 1)[1].split(":", 1)
    csr = csr_for(private)
    message = "\n".join(["acc-mobile-claim-v1", pairing, token, ORIGIN, INSTALL, "android",
                         hashlib.sha256(csr.encode()).hexdigest()]).encode()
    proof = base64.urlsafe_b64encode(private.sign(message, ec.ECDSA(hashes.SHA256()))).decode().rstrip("=")
    return {"token": token, "installation_id": INSTALL, "platform": "android", "csr": csr, "proof": proof}


def body(response):
    try:
        return {"body": response.json()}
    except ValueError:
        return {"text": response.text}


async def recovery(api, origin):
    """A separate, recently verified private sign-in of the owner (as the server's lost-phone test makes one)."""
    with write_db(api.app.state.db) as s:
        token, csrf = auth.create_session(s, s.scalar(select(User).where(User.username == "casey")), "Recovery")
        s.get(AuthSession, auth.digest(token)).reauthenticated_at = auth.now()
        s.commit()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://acc",
                               cookies={"acc_session": token}, headers={"Host": "acc.example.lan"})
    return client, {"Origin": origin, "X-ACC-CSRF": csrf}


class Recorder:
    def __init__(self):
        self.exchanges = {}

    async def __call__(self, name, client, method, path, *, json_body=None, headers=None):
        response = await client.request(method, path, json=json_body, headers=headers)
        kept = {k: v for k, v in response.headers.multi_items() if k.lower() in ("set-cookie", "cache-control")}
        set_cookies = [v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"]
        self.exchanges[name] = {"request": {"method": method, "path": path,
                                            **({"body": json_body} if json_body is not None else {})},
                                "status": response.status_code,
                                "headers": {k: v for k, v in kept.items() if k.lower() != "set-cookie"},
                                **({"set_cookie": set_cookies} if set_cookies else {}),
                                **body(response)}
        return response


async def test_record_android_fixtures(api, mobile):  # noqa: F811
    headers, record = mobile, Recorder()
    first, second = (key(label) for label in KEY_LABELS)

    start = (await record("start", api, "POST", PREFIX + "/pairings", json_body={"label": "Robin phone", "person": "casey"},
                          headers=headers)).json()
    claim = (await record("claim", api, "POST", PREFIX + "/claim", json_body=claim_body(start, first))).json()
    await record("credential_waiting", api, "POST", PREFIX + "/credential", json_body={"receipt": claim["receipt"]})
    await record("confirm", api, "POST", PREFIX + "/pairings/" + start["id"] + "/confirm",
                 json_body={"matching_code": claim["matching_code"]}, headers=headers)
    credential = (await record("credential", api, "POST", PREFIX + "/credential",
                               json_body={"receipt": claim["receipt"]})).json()
    await record("claim_used", api, "POST", PREFIX + "/claim", json_body=claim_body(start, first))
    await activate(api)
    await record("credential_awaiting", api, "POST", PREFIX + "/credential", json_body={"receipt": claim["receipt"]})

    # The public side: the server test's own simulation of the trusted Unix proxy (transport client None), never an
    # edge. Private steps put the LAN peer back.
    leaf = x509.load_pem_x509_certificate(credential["certificate"].encode())
    lan_peer, api._transport.client = api._transport.client, None
    public = {**headers, "Host": "remote.example.com", "X-Acc-Edge-Client": devices.fingerprint(leaf)}
    await record("before_bind", api, "GET", "/api/projects", headers=public)
    await record("auth_state", api, "GET", "/api/auth/state", headers=public)  # usable before binding: the person check
    # Keep only the sign-in fields: the rest describes the recording machine (host name, build), not the contract.
    state = record.exchanges["auth_state"]["body"]
    record.exchanges["auth_state"]["body"] = {k: state[k] for k in (
        "setup_required", "authenticated", "username", "role", "sign_in_method", "step_up_required") if k in state}
    await record("bind", api, "POST", PREFIX + "/session/bind", json_body={}, headers=public)
    public["X-ACC-CSRF"] = api.cookies.get("acc_csrf")
    await record("devices_public", api, "GET", PREFIX + "/devices", headers=public)
    api._transport.client = lan_peer

    # Binding rotated the owner's cookie and bound it to the phone. Owner steps continue from a separate private
    # sign-in, as the server's own lost-phone test does.
    owner, headers = await recovery(api, headers["Origin"])
    await record("devices_private", owner, "GET", PREFIX + "/devices", headers=headers)

    # Rotation: same installation, fresh key; the old key is refused.
    rotation = (await record("rotate", owner, "POST", PREFIX + "/devices/" + credential["device_id"] + "/rotate",
                             json_body={"label": "Robin phone", "person": "casey"}, headers=headers)).json()
    await record("rotate_same_key", owner, "POST", PREFIX + "/claim", json_body=claim_body(rotation, first))
    rotated_claim = (await record("rotate_claim", owner, "POST", PREFIX + "/claim",
                                  json_body=claim_body(rotation, second))).json()
    await record("rotate_confirm", owner, "POST", PREFIX + "/pairings/" + rotation["id"] + "/confirm",
                 json_body={"matching_code": rotated_claim["matching_code"]}, headers=headers)
    await record("rotate_credential", owner, "POST", PREFIX + "/credential", json_body={"receipt": rotated_claim["receipt"]})
    await record("old_receipt_after_rotation", owner, "POST", PREFIX + "/credential", json_body={"receipt": claim["receipt"]})

    # Revocation, then what the phone sees: its receipt is no longer current, and the edge refuses its leaf.
    rotated = x509.load_pem_x509_certificate(record.exchanges["rotate_credential"]["body"]["certificate"].encode())
    await record("revoke", owner, "POST", PREFIX + "/devices/" + credential["device_id"] + "/revoke", json_body={},
                 headers=headers)
    await record("credential_after_revoke", owner, "POST", PREFIX + "/credential",
                 json_body={"receipt": rotated_claim["receipt"]})
    # Revocation ends every session of the device's person: here that is the owner's recovery sign-in too.
    await record("session_after_revoke", owner, "GET", PREFIX + "/devices", headers=headers)
    await owner.aclose()
    owner, headers = await recovery(api, headers["Origin"])
    await record("devices_after_revoke", owner, "GET", PREFIX + "/devices", headers=headers)
    api._transport.client = None
    await record("edge_after_revoke", api, "GET", "/api/auth/state",
                 headers={"Host": "remote.example.com", "X-Acc-Edge-Client": devices.fingerprint(rotated)})
    api._transport.client = lan_peer
    for _ in range(12):  # the device bucket: ten attempts per ten minutes
        last = await api.post(PREFIX + "/credential", json={"receipt": "x" * 43})
    record.exchanges["throttled"] = {"request": {"method": "POST", "path": PREFIX + "/credential",
                                                 "body": {"receipt": "x" * 43}},
                                     "status": last.status_code, "headers": {}, **body(last)}

    await owner.aclose()

    out = Path(os.environ["ANDROID_FIXTURES"])
    out.mkdir(parents=True, exist_ok=True)
    keys = {label: base64.b64encode(key(label).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode() for label in KEY_LABELS}
    (out / "lifecycle.json").write_text(json.dumps({
        "recorded_from": os.popen("git rev-parse HEAD").read().strip(),
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "how": "mobile/android/tools/record_fixtures.py, in process with the server's own test fixtures",
        "private_origin": ORIGIN, "installation_id": INSTALL, "platform": "android",
        # The server test fixture runs with cookie_secure off; production HTTPS uses __Host- names.
        "cookie_secure": api.app.state.ctx.settings.cookie_secure,
        "test_keys": [{"label": label, "public_key_spki_b64": keys[label]} for label in KEY_LABELS],
        "now_for_tests": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "exchanges": record.exchanges,
    }, indent=2, sort_keys=False) + "\n")
    assert auth  # the server package imported cleanly
