"""Company sign-in (OIDC, organizations phase 1) against the local test identity provider in tests/oidc_idp.py."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
from alembic import command
from helpers import TOTP_AT
from oidc_idp import TestIdP
from sqlalchemy import select
from test_auth import ORIGIN, PASSWORD, do_setup

from acc.organization_ids import person_id
from acc.server import auth, oidc
from acc.server.db import alembic_config, migrate
from acc.server.models import AuditLog, AuthSession, ExternalIdentity, OIDCFlow, Setting, SSOInvite, User
from acc.server.oidc import OIDCError, Provider, Providers

ISSUER = "https://idp.example.com/tenant-a"
OTHER_ISSUER = "https://idp.example.com/tenant-b"


# ---------------------------------------------------------------- the relying party on its own

@pytest.fixture
def idp():
    return TestIdP(ISSUER)


@pytest.fixture
def rp(idp):
    return Provider(ISSUER, idp.client_id, transport=idp.transport)


def refused(rp, token, reason, nonce="n-1", access=None):
    with pytest.raises(OIDCError) as caught:
        rp.verify(token, nonce=nonce, access_token=access)
    assert caught.value.reason == reason, caught.value


def test_good_token_verifies(idp, rp):
    claims = rp.verify(idp.sign(idp.claims("alice-sub", "n-1", "at")), nonce="n-1", access_token="at")
    assert claims["sub"] == "alice-sub"


def test_alg_none_and_hs256_with_public_key_are_refused(idp, rp):
    claims = idp.claims("alice-sub", "n-1")
    refused(rp, idp.unsigned(claims), "bad_algorithm")
    refused(rp, idp.hs256_with_public_key(claims), "bad_algorithm")
    # Lower-case or padded variants of "none" are just as unknown.
    for alg in ("None", "NONE", "RS256 ", "rs256", "ES256", "PS256"):
        refused(rp, idp.sign(claims, header={"alg": alg}), "bad_algorithm")


def test_key_material_named_by_the_token_is_never_used(idp, rp):
    attacker = TestIdP(ISSUER)  # its own keys, same kid names
    attacker.rotate()
    token = attacker.sign(attacker.claims("alice-sub", "n-1"), header={"jku": "https://evil.example.com/keys",
                                                                       "kid": "k1"})
    refused(rp, token, "bad_signature")
    refused(rp, idp.sign(idp.claims("alice-sub", "n-1"), header={"crit": ["exp"]}), "bad_algorithm")


@pytest.mark.parametrize("change,reason", [
    ({"aud": "someone-else"}, "wrong_audience"),
    ({"aud": ["someone-else", "acc-client"]}, "wrong_audience"),  # several audiences need azp = us
    ({"aud": "acc-client", "azp": "someone-else"}, "wrong_audience"),
    ({"iss": OTHER_ISSUER}, "wrong_issuer"),
    ({"iss": ISSUER + "/"}, "wrong_issuer"),
    ({"nonce": "n-old"}, "wrong_nonce"),
    ({"sub": ""}, "malformed_token"),
    ({"exp": "soon"}, "malformed_token"),
])
def test_claims_are_checked(idp, rp, change, reason):
    refused(rp, idp.sign(idp.claims("alice-sub", "n-1") | change), reason)


def test_multiple_audiences_with_our_azp_pass(idp, rp):
    rp.verify(idp.sign(idp.claims("alice-sub", "n-1", aud=["acc-client", "api://other"], azp="acc-client")), nonce="n-1")


def test_times_respect_bounded_clock_skew(idp, rp):
    now = int(time.time())
    skew = oidc.CLOCK_SKEW
    rp.verify(idp.sign(idp.claims("s", "n-1", exp=now - skew + 5)), nonce="n-1")  # just expired: within skew
    refused(rp, idp.sign(idp.claims("s", "n-1", exp=now - skew - 5)), "expired")
    rp.verify(idp.sign(idp.claims("s", "n-1", iat=now + skew - 5)), nonce="n-1")
    refused(rp, idp.sign(idp.claims("s", "n-1", iat=now + skew + 30)), "not_yet_valid")
    refused(rp, idp.sign(idp.claims("s", "n-1", nbf=now + skew + 30)), "not_yet_valid")
    refused(rp, idp.sign(idp.claims("s", "n-1", iat=now - 3600, exp=now + 300)), "expired")  # stale sign-in


def test_at_hash_must_match_the_access_token(idp, rp):
    token = idp.sign(idp.claims("s", "n-1", "the-real-access-token"))
    refused(rp, token, "wrong_at_hash", access="another-access-token")
    refused(rp, token, "wrong_at_hash", access=None)


def test_key_rotation_fetches_new_keys_and_retires_removed_ones(idp):
    clock = [1000.0]
    rp = Provider(ISSUER, idp.client_id, transport=idp.transport, clock=lambda: clock[0])
    idp.clock = lambda: clock[0]
    rp.verify(idp.sign(idp.claims("s", "n-1")), nonce="n-1")
    fetches = lambda: sum(r.url.path.endswith("/keys") for r in idp.requests)  # noqa: E731
    assert fetches() == 1
    new_kid = idp.rotate(drop_old=True)
    # Too soon after the last fetch: an unknown kid does not trigger another fetch (tokens can't drive requests).
    clock[0] += 10
    refused(rp, idp.sign(idp.claims("s", "n-1"), kid=new_kid), "unknown_key")
    assert fetches() == 1
    clock[0] += oidc.MIN_REFRESH
    rp.verify(idp.sign(idp.claims("s", "n-1"), kid=new_kid), nonce="n-1")
    assert fetches() == 2
    # The removed key is gone from the refreshed set: an old-key token fails closed.
    old = TestIdP(ISSUER, clock=lambda: clock[0])
    refused(rp, old.sign(old.claims("s", "n-1"), kid="k1"), "unknown_key")


def test_discovery_is_pinned_to_the_issuer_and_https(idp):
    idp.discovery_overrides = {"issuer": "https://evil.example.com"}
    with pytest.raises(OIDCError, match="bad_discovery"):
        Provider(ISSUER, "c", transport=idp.transport).discovery()
    idp.discovery_overrides = {"token_endpoint": "http://idp.example.com/token"}
    with pytest.raises(OIDCError, match="bad_discovery"):
        Provider(ISSUER, "c", transport=idp.transport).discovery()
    idp.discovery_overrides = {"code_challenge_methods_supported": ["plain"]}
    with pytest.raises(OIDCError, match="bad_discovery"):
        Provider(ISSUER, "c", transport=idp.transport).discovery()
    for bad in ("http://idp.example.com", "https://idp.example.com/?x=1", "https://u:p@idp.example.com", "idp.example.com"):
        with pytest.raises(OIDCError):
            oidc.check_issuer(bad)


def test_authorization_request_uses_code_flow_with_pkce(idp, rp):
    url = rp.authorization_url(redirect_uri="https://acc.example.com/api/auth/oidc/callback", state="st", nonce="nn",
                               verifier="v" * 64)
    query = parse_qs(urlsplit(url).query)
    assert query["response_type"] == ["code"]  # never the implicit flow: no tokens in the redirect
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [oidc.pkce_challenge("v" * 64)]
    assert query["state"] == ["st"] and query["nonce"] == ["nn"]
    assert "v" * 64 not in url


# ---------------------------------------------------------------- through the app

@pytest.fixture
def env(env):
    env["settings"] = replace(env["settings"], organizations_enabled=True, allowed_origins=("https://acc.example.com",
                                                                                           "http://acc"))
    return env


@pytest.fixture
async def org(api, env, idp):
    """Owner set up; the provider configured and on; the app talking to the test IdP."""
    secret = await do_setup(api, env)
    api.app.state.ctx.oidc = Providers(transport=idp.transport)
    response = await api.put("/api/organizations/oidc", json={
        "issuer": ISSUER, "client_id": idp.client_id, "client_secret": idp.client_secret, "enabled": True})
    assert response.status_code == 200, response.text
    return {"api": api, "idp": idp, "totp": secret}


def browser(api) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://acc")


async def start(client, **params):
    response = await client.get("/api/auth/oidc/start", params=params)
    assert response.status_code == 303, response.text
    return response, parse_qs(urlsplit(response.headers["location"]).query)


async def sso(client, idp, sub, nonce=None, **params):
    response, query = await start(client, **params)
    assert response.headers["location"].startswith(idp.issuer + "/authorize?"), response.headers["location"]
    code = idp.issue_code(sub, query, nonce=nonce)
    return await client.get("/api/auth/oidc/callback", params={"state": query["state"][0], "code": code})


def landed(response, where="/"):
    assert response.status_code == 303, response.text
    assert response.headers["location"] == where, response.headers["location"]


def db(api):
    return api.app.state.db


async def preprovision(api, username, subject, account=None):
    response = await api.post("/api/organizations/identities",
                              json={"username": username, "subject": subject, "account": account or f"{username}-agent"})
    assert response.status_code == 201, response.text
    return response.json()


async def test_hidden_and_off_without_organizations_mode(env, broker):
    settings = replace(env["settings"], organizations_enabled=False)
    from acc.server.app import create_app
    app = create_app(settings)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://acc",
                                     headers={"Authorization": f"Bearer {settings.api_token()}"}) as client:
            assert (await client.get("/api/organizations/oidc")).status_code == 404
            assert (await client.put("/api/organizations/oidc", json={})).status_code == 404
            assert (await client.get("/api/auth/state")).json()["sso"] is False
            response = await client.get("/api/auth/oidc/start")
            assert response.headers["location"] == "/login?sso=disabled"
    finally:
        await app.state.supervisor.broker.close()
        app.state.db.kw["bind"].dispose()


async def test_sso_is_off_until_an_owner_turns_it_on(api, env, idp):
    await do_setup(api, env)
    api.app.state.ctx.oidc = Providers(transport=idp.transport)
    assert (await api.get("/api/auth/state")).json()["sso"] is False
    assert (await api.get("/api/auth/oidc/start")).headers["location"] == "/login?sso=disabled"
    assert (await api.put("/api/organizations/oidc", json={"enabled": True})).status_code == 422
    async with browser(api) as member:
        assert (await member.get("/api/organizations/oidc")).status_code == 401


async def test_owner_settings_keep_the_secret_write_only(org):
    api, idp = org["api"], org["idp"]
    shown = (await api.get("/api/organizations/oidc")).json()
    assert shown == {"issuer": ISSUER, "client_id": idp.client_id, "has_secret": True, "enabled": True,
                     "session_hours": 12, "mfa_acr": [], "passkey_enrollment": True,
                     "redirect_uri": "https://acc.example.com/api/auth/oidc/callback"}
    with db(api)() as s:
        row = s.scalar(select(oidc_provider_model()))
        assert idp.client_secret not in row.secret_ciphertext
        assert all(idp.client_secret not in str(a.detail) for a in s.scalars(select(AuditLog)))
    # Saving without a secret keeps it; a different issuer drops it (it is sealed to the issuer and client).
    await api.put("/api/organizations/oidc", json={"issuer": ISSUER, "client_id": idp.client_id, "enabled": True})
    assert (await api.get("/api/organizations/oidc")).json()["has_secret"] is True
    await api.put("/api/organizations/oidc", json={"issuer": OTHER_ISSUER, "client_id": idp.client_id, "enabled": False})
    assert (await api.get("/api/organizations/oidc")).json()["has_secret"] is False
    assert (await api.put("/api/organizations/oidc", json={"issuer": "http://idp.example.com", "client_id": "x"})).status_code == 422


def oidc_provider_model():
    from acc.server.models import OIDCProvider
    return OIDCProvider


async def test_test_connection_fetches_discovery_and_keys_only(org):
    api, idp = org["api"], org["idp"]
    idp.requests.clear()
    result = (await api.post("/api/organizations/oidc/test", json={})).json()
    assert result["ok"] is True and result["keys"] == 1, result
    assert sorted(r.url.path for r in idp.requests) == ["/tenant-a/.well-known/openid-configuration", "/tenant-a/keys"]
    idp.down = True
    result = (await api.post("/api/organizations/oidc/test", json={})).json()
    assert result == {"ok": False, "reason": "provider_unavailable", "detail": "ConnectError"}
    result = (await api.post("/api/organizations/oidc/test", json={"issuer": "http://plain.example.com"})).json()
    assert result["ok"] is False and result["reason"] == "bad_issuer"


@pytest.mark.parametrize("status,payload,want", [
    (401, {"error": "invalid_client", "error_description": "AADSTS7000215: {private}"},
     {"oauth_error": "invalid_client", "aadsts_code": "AADSTS7000215"}),
    (400, {"error": "provider_specific-error", "error_description": "\r\nAADSTS9002325: {private}\u202e"},
     {"oauth_error": "provider_specific-error", "aadsts_code": "AADSTS9002325"}),
    (200, {"error": "invalid_grant"}, {"oauth_error": "invalid_grant"}),
    (400, {"error": "invalid_client\n{private}", "error_description": "{private}"}, {}),
    (400, {"error": "x" * 65, "error_description": "AADSTS12345678901: {private}"}, {}),
    (400, {"error": ["invalid_client"], "error_description": {"message": "AADSTS7000215"}}, {}),
    (400, {"error": "{client_secret}", "error_description": "{private}"}, {}),
    (400, {"error": "{code}", "error_description": "{private}"}, {}),
    (400, {"error": "{code_verifier}", "error_description": "{private}"}, {}),
    (400, {"error": "test-access-token", "access_token": "test-access-token"}, {}),
    (400, {"error": "test-refresh-token", "refresh_token": "test-refresh-token"}, {}),
    (400, {"error": "AADSTS7000215", "id_token": "AADSTS7000215",
           "error_description": "AADSTS7000215: {private}"}, {}),
    (401, b"not JSON: private provider response", {}),
    (400, ["invalid_client"], {}),
    (400, b"[" * 1100 + b"]" * 1100, {}),
    (400, {"error_description": "prefixAADSTS7000215Suffix AADSTS70002150x"}, {}),
    (400, b'{"error":"invalid_client","padding":"' + b"x" * oidc.MAX_RESPONSE + b'"}', {}),
], ids=["entra", "extension", "error-with-200", "unsafe-code", "overlong-codes", "wrong-types",
        "reflected-secret", "reflected-code", "reflected-verifier", "access-token", "refresh-token",
        "id-token", "not-json", "not-object", "deep-json", "embedded-code", "oversized"])
async def test_token_refusal_audits_safe_codes_and_local_detail(org, caplog, status, payload, want):
    api, idp = org["api"], org["idp"]
    caplog.set_level(logging.DEBUG, logger="acc")
    private = ["test-access-token", "test-refresh-token", idp.client_secret]

    def reply(request):
        if request.method != "POST" or not request.url.path.endswith("/token"):
            return idp.handle(request)
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        private.extend(form[name] for name in ("code", "code_verifier"))
        if isinstance(payload, dict):
            values = {key: value.format(private=" ".join(private), **form) if isinstance(value, str) else value
                      for key, value in payload.items()}
            values.update(access_token="test-access-token", refresh_token="test-refresh-token")
            return httpx.Response(status, json=values)
        return httpx.Response(status, content=payload) if isinstance(payload, bytes) else httpx.Response(status, json=payload)

    api.app.state.ctx.oidc = Providers(transport=httpx.MockTransport(reply))
    async with browser(api) as person:
        response = await sso(person, idp, "alex-sub")
        landed(response, "/login?sso=failed")
        assert (await person.get("/api/auth/state")).json()["authenticated"] is False
    with db(api)() as s:
        record = s.scalar(select(AuditLog).where(AuditLog.action == "auth.sso_failed"))
        assert {key: value for key, value in record.detail.items() if key not in ("error_code", "error_text")} == {
            "reason": "token_refused", "issuer": ISSUER,
            "detail": "no ID token" if status == 200 else f"HTTP {status} from the token endpoint", **want}
        assert s.scalar(select(OIDCFlow)) is None
        audit = " ".join(str(row.detail) for row in s.scalars(select(AuditLog)))
    for value in private:
        assert value not in audit and value not in caplog.text and value not in response.headers["location"]


@pytest.mark.parametrize("description,expected_code,expected_text", [
    ("AADSTS7000215: invalid client secret. \r\nAuthorization {code}; secret {client_secret}; "
     "verifier {code_verifier}; access {access_token}; refresh {refresh_token}; id {id_token}.\u202e",
     "AADSTS7000215", "AADSTS7000215: invalid client secret. Authorization [redacted]; secret [redacted]; "
     "verifier [redacted]; access [redacted]; refresh [redacted]; id [redacted]."),
    ("AADSTS7000215: " + "x" * 600 + " secret {client_secret}", "AADSTS7000215",
     "AADSTS7000215: " + "x" * 285),
    ("AADSTS7000215: " + "x" * 280 + " {client_secret}", "AADSTS7000215",
     "AADSTS7000215: " + "x" * 280 + " [red"),
    ("AADSTS7000215: secret {split_secret}", "AADSTS7000215", "AADSTS7000215: secret [redacted]"),
    ("AADSTS7000215: secret {encoded_secret}", "AADSTS7000215", "AADSTS7000215: secret [redacted]"),
    ("Invalid client secret.", "invalid_client", "Invalid client secret."),
    ("AADSTS7000215: secret {secret_prefix}... or {secret_upper}", "AADSTS7000215",
     "AADSTS7000215: secret [redacted]... or [redacted]"),
], ids=["redact-and-strip", "bounded", "redact-before-truncating", "control-split-secret", "encoded-secret", "oauth-fallback",
        "partial-and-case-changed-secret"])
async def test_provider_failure_text_is_bounded_redacted_and_owner_only(org, caplog, description, expected_code, expected_text):
    api, idp = org["api"], org["idp"]
    idp.client_secret = "test-secret+/=value"
    response = await api.put("/api/organizations/oidc", json={
        "issuer": idp.issuer, "client_id": idp.client_id, "client_secret": idp.client_secret, "enabled": True})
    assert response.status_code == 200, response.text
    caplog.set_level(logging.DEBUG, logger="acc")
    private = [idp.client_secret, "test-access-token", "test-refresh-token", "test-id-token"]

    def refusal(request):
        if request.method != "POST" or not request.url.path.endswith("/token"):
            return idp.handle(request)
        form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        private.extend(form[name] for name in ("code", "code_verifier"))
        values = {**form, "access_token": private[1], "refresh_token": private[2], "id_token": private[3],
                  "split_secret": "\x00".join(idp.client_secret), "encoded_secret": quote(idp.client_secret, safe=""),
                  "secret_prefix": idp.client_secret[:9], "secret_upper": idp.client_secret.upper()}
        return httpx.Response(401, json={"error": "invalid_client", "error_description": description.format(**values),
                                        **{key: values[key] for key in ("access_token", "refresh_token", "id_token")}})

    api.app.state.ctx.oidc = Providers(transport=httpx.MockTransport(refusal))
    async with browser(api) as person:
        response = await sso(person, idp, "dana-sub")
        landed(response, "/login?sso=failed")
        assert (await person.get("/api/audit")).status_code == 401
    rows = (await api.get("/api/audit", params={"action": "auth.sso_failed"})).json()
    detail = rows[0]["detail"]
    assert detail.get("error_code") == expected_code
    assert detail.get("error_text") == expected_text
    assert detail["detail"] == "HTTP 401 from the token endpoint"
    assert all(value not in str(rows) and value not in caplog.text and value not in response.headers["location"]
               for value in private)


async def test_discovery_refusal_audits_existing_local_detail(org):
    api, idp = org["api"], org["idp"]
    idp.down = True
    async with browser(api) as person:
        response, _ = await start(person)
        landed(response, "/login?sso=unavailable")
    with db(api)() as s:
        record = s.scalar(select(AuditLog).where(AuditLog.action == "auth.sso_failed"))
        assert record.detail == {"reason": "provider_unavailable", "detail": "ConnectError"}


async def test_unknown_subject_is_refused_and_nothing_is_created(org):
    api, idp = org["api"], org["idp"]
    async with browser(api) as person:
        landed(await sso(person, idp, "stranger-sub"), "/login?sso=not_provisioned")
        assert (await person.get("/api/auth/state")).json()["authenticated"] is False
    with db(api)() as s:
        assert s.scalar(select(User).where(User.role == "member")) is None
        assert s.scalar(select(AuditLog).where(AuditLog.action == "auth.sso_failed")).detail["reason"] == "not_provisioned"


async def test_preprovisioned_subject_signs_in_with_a_stable_person_id(org):
    api, idp = org["api"], org["idp"]
    made = await preprovision(api, "alex", "alex-sub")
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
        state = (await person.get("/api/auth/state")).json()
        assert state["authenticated"] is True and state["username"] == "alex" and state["role"] == "member"
        assert state["sign_in_method"] == "oidc"
        assert (await person.get("/api/sessions")).status_code == 200
    with db(api)() as s:
        user = s.scalar(select(User).where(User.username == "alex"))
        namespace = s.get(Setting, "organizations.namespace").value["uuid"]
        assert user.person_id == made["person_id"] == person_id(namespace, user.id)
        record = s.scalar(select(AuthSession).where(AuthSession.user_id == user.id))
        assert record.method == "oidc"
        assert record.expires_at - record.created_at <= auth.timedelta(hours=12, seconds=5)
        # No usable local credentials: the random password is unknown to everyone.
        assert not auth.password_ok(user.password_hash, "")


async def test_email_is_never_the_key(org):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    idp.token_overrides = {"email": "alex@example.com", "preferred_username": "alex", "name": "alex"}
    async with browser(api) as person:
        landed(await sso(person, idp, "intruder-sub"), "/login?sso=not_provisioned")


async def test_claim_collision_same_subject_from_two_issuers(org):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "shared-sub")
    other = TestIdP(OTHER_ISSUER, client_id=idp.client_id, client_secret=idp.client_secret)
    api.app.state.ctx.oidc = Providers(transport=other.transport)
    await api.put("/api/organizations/oidc", json={"issuer": OTHER_ISSUER, "client_id": idp.client_id,
                                                   "client_secret": idp.client_secret, "enabled": True})
    async with browser(api) as person:
        landed(await sso(person, other, "shared-sub"), "/login?sso=not_provisioned")
    # Both bindings can exist side by side: the pair, not the subject, is unique.
    await preprovision(api, "blake", "shared-sub")
    with db(api)() as s:
        assert {(i.issuer, i.subject) for i in s.scalars(select(ExternalIdentity))} == {
            (ISSUER, "shared-sub"), (OTHER_ISSUER, "shared-sub")}
    async with browser(api) as person:
        landed(await sso(person, other, "shared-sub"))
        assert (await person.get("/api/auth/state")).json()["username"] == "blake"
    # The same pair can't be bound twice.
    response = await api.post("/api/organizations/identities",
                              json={"username": "casey2", "subject": "shared-sub", "account": "casey2-agent"})
    assert response.status_code == 409


async def test_invite_creates_the_member_at_first_sign_in_once(org):
    api, idp = org["api"], org["idp"]
    response = await api.post("/api/organizations/sso-invites", json={"username": "dana", "account": "dana-agent",
                                                                           "email": "Dana@Example.com"})
    assert response.status_code == 201, response.text
    token = parse_qs(urlsplit(response.json()["url"]).query)["invite"][0]
    assert response.json()["url"].startswith("https://acc.example.com/api/auth/oidc/start?invite=")
    # The invited directory account (a verified email here; preferred_username counts only from Entra ID)
    idp.token_overrides = {"email": "dana@example.com", "email_verified": True}
    async with browser(api) as person:
        landed(await sso(person, idp, "dana-sub", invite=token))
        assert (await person.get("/api/auth/state")).json()["username"] == "dana"
    async with browser(api) as again:  # the invite is used: another subject can't redeem it
        landed(await again.get("/api/auth/oidc/start", params={"invite": token}), "/login?sso=invite_expired")
    async with browser(api) as person:  # dana is now known: no invite needed
        landed(await sso(person, idp, "dana-sub"))


@pytest.mark.parametrize("existing", [False, True], ids=["new-member", "existing-member"])
async def test_withdrawing_one_invite_revokes_its_flows_and_preserves_other_invites_and_history(org, existing):
    api, idp = org["api"], org["idp"]
    local = None
    if existing:
        with db(api)() as s:
            user = User(username="dana", role="member", account="dana-agent",
                        password_hash=auth.hash_password(PASSWORD), totp_secret=auth.new_totp_secret())
            s.add(user)
            s.flush()
            local, _ = auth.create_session(s, user, "test")
            s.commit()
    made = []
    for account in ("dana-agent", "dana-other-agent"):
        response = await api.post("/api/organizations/sso-invites", json={
            "username": "dana", "account": account, "email": "dana@example.com", "existing": existing})
        assert response.status_code == 201, response.text
        made.append(response.json())
    tokens = [parse_qs(urlsplit(item["url"]).query)["invite"][0] for item in made]
    with db(api)() as s:
        invites = [s.scalar(select(SSOInvite).where(SSOInvite.token_hash == auth.digest(token))) for token in tokens]
        selected, retained = [invite.id for invite in invites]

    async with browser(api) as person, browser(api) as other:
        if local:
            for client in (person, other):
                client.cookies.set("acc_session", local, domain="acc.local")
        _, pending = await start(person, invite=tokens[0])
        _, kept = await start(other, invite=tokens[1])
        withdrawn = await api.delete(f"/api/organizations/sso-invites/{selected}")
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json() == {"ok": True}
        with db(api)() as s:
            assert s.get(SSOInvite, selected) is not None
            assert s.get(SSOInvite, selected).withdrawn_at is not None
            assert s.get(SSOInvite, retained) is not None
            assert s.get(OIDCFlow, auth.digest(pending["state"][0])) is not None
            assert s.get(OIDCFlow, auth.digest(kept["state"][0])) is not None
            records = list(s.scalars(select(AuditLog).where(AuditLog.action == "org.invite_withdrawn")))
            assert len(records) == 1
            assert records[0].target == "dana"
            assert records[0].detail == {"invite": selected, "username": "dana"}
            user = s.scalar(select(User).where(User.username == "dana"))
            assert (user is not None) == existing
        async with browser(api) as again:
            landed(await again.get("/api/auth/oidc/start", params={"invite": tokens[0]}), "/login?sso=invite_withdrawn")
        before = len(idp.requests)
        stale = await person.get("/api/auth/oidc/callback", params={
            "state": pending["state"][0], "code": idp.issue_code("dana-sub", pending)})
        landed(stale, "/login?sso=invite_withdrawn")
        assert len(idp.requests) == before
        idp.token_overrides = {"email": "dana@example.com", "email_verified": True}
        landed(await other.get("/api/auth/oidc/callback", params={
            "state": kept["state"][0], "code": idp.issue_code("dana-sub", kept)}))
    used = await api.delete(f"/api/organizations/sso-invites/{retained}")
    assert used.status_code == 409, used.text
    assert (await api.delete(f"/api/organizations/sso-invites/{selected}")).status_code == 200
    assert (await api.delete("/api/organizations/sso-invites/999999999")).status_code == 404
    assert not (await api.get("/api/organizations/identities")).json()["invites"]
    with db(api)() as s:
        assert s.get(SSOInvite, retained).used_at is not None
        assert len(list(s.scalars(select(AuditLog).where(AuditLog.action == "org.invite_withdrawn")))) == 1


@pytest.mark.parametrize("existing", [False, True], ids=["new-member", "existing-member"])
async def test_invite_rows_identify_agent_account_and_creation_time(org, existing):
    api = org["api"]
    if existing:
        with db(api)() as s:
            s.add(User(username="dana", role="member", account="dana-agent",
                       password_hash=auth.hash_password(PASSWORD), totp_secret=auth.new_totp_secret()))
            s.commit()
    response = await api.post("/api/organizations/sso-invites", json={
        "username": "dana", "email": "dana@example.com", "account": "dana-agent", "existing": existing})
    assert response.status_code == 201, response.text
    created = datetime(2026, 10, 25, 10, 30, tzinfo=timezone.utc)
    with db(api)() as s:
        invite = s.scalar(select(SSOInvite))
        invite.created_at = created
        invite_id = invite.id
        s.commit()
    row, = (await api.get("/api/organizations/identities")).json()["invites"]
    assert row["id"] == invite_id and response.json()["id"] == invite_id
    assert row["username"] == "dana" and row["account"] == "dana-agent"
    assert row.get("email") == "dana@example.com"
    assert datetime.fromisoformat(row["created_at"]) == created


async def test_withdrawing_an_invite_requires_owner_cookie_csrf_and_origin(org):
    api = org["api"]
    made = await api.post("/api/organizations/sso-invites", json={
        "username": "dana", "email": "dana@example.com", "account": "dana-agent"})
    assert made.status_code == 201
    with db(api)() as s:
        invite_id = s.scalar(select(SSOInvite.id))
    path = f"/api/organizations/sso-invites/{invite_id}"
    csrf = api.cookies.get("acc_csrf")
    for headers in [
        {**ORIGIN, "Authorization": ""},
        {**ORIGIN, "Authorization": "", "X-ACC-CSRF": "wrong"},
        {"Origin": "https://outside.example.com", "Authorization": "", "X-ACC-CSRF": csrf},
    ]:
        refused = await api.delete(path, headers=headers)
        assert refused.status_code == 403, refused.text
    async with browser(api) as stranger:
        assert (await stranger.delete(path, headers=ORIGIN)).status_code == 401
    with db(api)() as s:
        assert s.get(SSOInvite, invite_id) is not None
        assert s.scalar(select(AuditLog).where(AuditLog.action == "org.invite_withdrawn")) is None
    withdrawn = await api.delete(path, headers={**ORIGIN, "Authorization": "", "X-ACC-CSRF": csrf})
    assert withdrawn.status_code == 200, withdrawn.text


async def test_withdraw_during_provider_discovery_refuses_the_pending_start(org):
    api, idp = org["api"], org["idp"]
    made = await api.post("/api/organizations/sso-invites", json={
        "username": "dana", "email": "dana@example.com", "account": "dana-agent"})
    token = parse_qs(urlsplit(made.json()["url"]).query)["invite"][0]
    reached, release = threading.Event(), threading.Event()

    def discovery(request):
        if request.url.path.endswith("/.well-known/openid-configuration"):
            reached.set()
            if not release.wait(10):
                raise httpx.ReadTimeout("test discovery was not released")
        return idp.handle(request)

    api.app.state.ctx.oidc = Providers(transport=httpx.MockTransport(discovery))
    async with browser(api) as person:
        pending = asyncio.create_task(start(person, invite=token))
        try:
            assert await asyncio.to_thread(reached.wait, 10)
            response = await api.delete(f"/api/organizations/sso-invites/{made.json()['id']}")
            assert response.status_code == 200, response.text
        finally:
            release.set()
            response, _ = await pending
        landed(response, "/login?sso=invite_withdrawn")
    with db(api)() as s:
        assert s.scalar(select(OIDCFlow)) is None


@pytest.mark.parametrize("provider_refuses", [False, True], ids=["verified-token", "provider-refusal"])
async def test_withdraw_during_token_exchange_refuses_the_callback(org, provider_refuses):
    api, idp = org["api"], org["idp"]
    made = (await api.post("/api/organizations/sso-invites", json={
        "username": "dana", "email": "dana@example.com", "account": "dana-agent"})).json()
    token = parse_qs(urlsplit(made["url"]).query)["invite"][0]
    idp.token_overrides = {"email": "dana@example.com", "email_verified": True}
    reached, release = threading.Event(), threading.Event()

    def exchange(request):
        if request.method == "POST" and request.url.path.endswith("/token"):
            reached.set()
            if not release.wait(10):
                raise httpx.ReadTimeout("test exchange was not released")
            if provider_refuses:
                return httpx.Response(401, json={"error": "invalid_client"})
        return idp.handle(request)

    api.app.state.ctx.oidc = Providers(transport=httpx.MockTransport(exchange))
    async with browser(api) as person:
        pending = asyncio.create_task(sso(person, idp, "dana-sub", invite=token))
        try:
            assert await asyncio.to_thread(reached.wait, 10)
            assert (await api.delete(f"/api/organizations/sso-invites/{made['id']}")).status_code == 200
        finally:
            release.set()
            response = await pending
        landed(response, "/login?sso=invite_withdrawn")
        assert (await person.get("/api/auth/state")).json()["authenticated"] is False
    with db(api)() as s:
        assert s.scalar(select(User).where(User.username == "dana")) is None
        assert s.scalar(select(ExternalIdentity)) is None
        assert s.scalar(select(AuditLog).where(AuditLog.action == "auth.sso_failed")).detail["reason"] == "invite_withdrawn"


def test_invite_revocation_migration_preserves_data_and_cannot_reactivate_links_on_rollback(tmp_path):
    url = f"sqlite:///{tmp_path / 'oidc.db'}"
    config = alembic_config(url)
    command.upgrade(config, "0054")
    with sqlite3.connect(tmp_path / "oidc.db") as connection:
        connection.execute(
            "INSERT INTO sso_invite (id,token_hash,username,email,account,created_by,created_at,expires_at) "
            "VALUES (1,?,?,?,?,?,?,?)",
            ("0" * 64, "dana", "dana@example.com", "dana-agent", "casey",
             "2026-10-25 10:00:00", "2026-11-01 10:00:00"))
        connection.execute(
            "INSERT INTO oidc_flow (state_hash,binding_hash,nonce,verifier,provider_revision,invite_id,expires_at) "
            "VALUES ('state','binding','nonce','verifier',1,1,'2026-10-25 10:10:00')")
    migrate(url)
    with sqlite3.connect(tmp_path / "oidc.db") as connection:
        assert connection.execute("SELECT username,email,withdrawn_at FROM sso_invite").fetchone() == (
            "dana", "dana@example.com", None)
        connection.execute("UPDATE sso_invite SET withdrawn_at='2026-10-25 10:05:00' WHERE id=1")
    command.downgrade(config, "0054")
    with sqlite3.connect(tmp_path / "oidc.db") as connection:
        assert connection.execute("SELECT username,expires_at,used_at FROM sso_invite").fetchone() == (
            "dana", "2026-10-25 10:05:00", None)
        assert connection.execute("SELECT invite_id FROM oidc_flow").fetchall() == [(1,)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    migrate(url)
    with sqlite3.connect(tmp_path / "oidc.db") as connection:
        assert connection.execute("SELECT expires_at,withdrawn_at FROM sso_invite").fetchone() == (
            "2026-10-25 10:05:00", None)


async def test_linking_an_existing_member_needs_their_recent_local_sign_in(org):
    api, idp = org["api"], org["idp"]
    with db(api)() as s:
        s.add(User(username="erin", role="member", account="erin-agent", password_hash=auth.hash_password(PASSWORD),
                   totp_secret=auth.new_totp_secret()))
        s.commit()
    response = await api.post("/api/organizations/sso-invites", json={"username": "erin", "existing": True,
                                                                           "email": "erin@example.com"})
    token = parse_qs(urlsplit(response.json()["url"]).query)["invite"][0]
    async with browser(api) as stranger:
        landed(await stranger.get("/api/auth/oidc/start", params={"invite": token}), "/login?sso=link_needs_local_sign_in")

    def local_sign_in(minutes_ago=0):
        with db(api)() as s:
            user = s.scalar(select(User).where(User.username == "erin"))
            local, _ = auth.create_session(s, user, "test")
            s.flush()
            s.get(AuthSession, auth.digest(local)).created_at = auth.now() - auth.timedelta(minutes=minutes_ago)
            s.commit()
        return local
    async with browser(api) as erin:
        erin.cookies.set("acc_session", local_sign_in(minutes_ago=20), domain="acc.local")  # signed in, but not recently
        landed(await erin.get("/api/auth/oidc/start", params={"invite": token}), "/login?sso=link_needs_local_sign_in")
        local = local_sign_in()
        idp.token_overrides = {"upn": "erin@example.com"}
        erin.cookies.set("acc_session", local, domain="acc.local")
        landed(await sso(erin, idp, "erin-sub", invite=token))
        state = (await erin.get("/api/auth/state")).json()
        assert state["username"] == "erin" and state["sign_in_method"] == "oidc"
        assert erin.cookies.get("acc_session") != local
    with db(api)() as s:
        assert s.get(AuthSession, auth.digest(local)) is None  # the local sign-in it replaced has ended
        erin_row = s.scalar(select(User).where(User.username == "erin"))
        assert auth.password_ok(erin_row.password_hash, PASSWORD)  # local credentials stay for break-glass
        assert s.scalar(select(ExternalIdentity)).person_id == erin_row.person_id
    # The owner can't be linked at all.
    owner_name = (await api.get("/api/auth/state")).json().get("username") or "casey"
    response = await api.post("/api/organizations/sso-invites", json={"username": owner_name, "existing": True,
                                                                           "email": "owner@example.com"})
    assert response.status_code == 409


@pytest.mark.parametrize("attack", ["alg_none", "hs256_public_key", "wrong_audience", "expired", "other_issuer"])
async def test_hostile_tokens_are_refused_end_to_end(org, attack):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    if attack == "alg_none":
        idp.forge = idp.unsigned
    elif attack == "hs256_public_key":
        idp.forge = idp.hs256_with_public_key
    elif attack == "wrong_audience":
        idp.token_overrides = {"aud": "another-app"}
    elif attack == "expired":
        idp.token_overrides = {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 4000}
    else:
        idp.token_overrides = {"iss": OTHER_ISSUER}
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"), "/login?sso=failed")
        assert (await person.get("/api/auth/state")).json()["authenticated"] is False


async def test_replayed_nonce_is_refused(org):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    async with browser(api) as person:
        _, first = await start(person)
        old_nonce = first["nonce"][0]
        # A token minted for an earlier flow (its nonce) presented in a new one.
        landed(await sso(person, idp, "alex-sub", nonce=old_nonce), "/login?sso=failed")
    with db(api)() as s:
        reasons = [a.detail["reason"] for a in s.scalars(select(AuditLog).where(AuditLog.action == "auth.sso_failed"))]
    assert reasons == ["wrong_nonce"]


async def test_mismatched_state_replayed_callback_and_login_csrf(org):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    async with browser(api) as person:
        _, query = await start(person)
        code = idp.issue_code("alex-sub", query)
        landed(await person.get("/api/auth/oidc/callback", params={"state": "made-up", "code": code}), "/login?sso=expired")
        # Login CSRF: an attacker hands the victim their own state and code; the victim's browser lacks the binding.
        async with browser(api) as victim:
            landed(await victim.get("/api/auth/oidc/callback", params={"state": query["state"][0], "code": code}),
                   "/login?sso=expired")
            assert (await victim.get("/api/auth/state")).json()["authenticated"] is False
        # The real browser's flow is untouched by those attempts and works once...
        response = await person.get("/api/auth/oidc/callback", params={"state": query["state"][0], "code": code})
        landed(response)
        # ...and only once.
        replay = await person.get("/api/auth/oidc/callback", params={"state": query["state"][0], "code": code})
        landed(replay, "/login?sso=expired")
    with db(api)() as s:
        assert s.scalar(select(OIDCFlow)) is None


async def test_provider_error_and_expired_flow(org):
    api = org["api"]
    async with browser(api) as person:
        _, query = await start(person)
        landed(await person.get("/api/auth/oidc/callback", params={"state": query["state"][0], "error": "access_denied"}),
               "/login?sso=denied")
        _, query = await start(person)
        with db(api)() as s:
            for flow in s.scalars(select(OIDCFlow)):
                flow.expires_at = auth.now() - auth.timedelta(seconds=1)
            s.commit()
        landed(await person.get("/api/auth/oidc/callback", params={"state": query["state"][0], "code": "x"}),
               "/login?sso=expired")


async def test_settings_change_ends_flows_in_progress(org):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    async with browser(api) as person:
        _, query = await start(person)
        code = idp.issue_code("alex-sub", query)
        await api.put("/api/organizations/oidc", json={"issuer": ISSUER, "client_id": idp.client_id, "enabled": True})
        landed(await person.get("/api/auth/oidc/callback", params={"state": query["state"][0], "code": code}),
               "/login?sso=disabled")


async def test_session_fixation_a_presented_session_is_replaced(org):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    await preprovision(api, "blake", "blake-sub")
    async with browser(api) as person:
        landed(await sso(person, idp, "blake-sub"))
        planted = person.cookies.get("acc_session")
        landed(await sso(person, idp, "alex-sub"))
        fresh = person.cookies.get("acc_session")
        assert fresh and fresh != planted
        assert person.cookies.get("acc_oidc") is None  # the flow binding is cleared
        assert (await person.get("/api/auth/state")).json()["username"] == "alex"
    with db(api)() as s:
        assert s.get(AuthSession, auth.digest(planted)) is None
    async with browser(api) as attacker:
        attacker.cookies.set("acc_session", planted)
        assert (await attacker.get("/api/auth/state")).json()["authenticated"] is False


async def test_logout_ends_the_company_sign_in(org):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
        token = person.cookies.get("acc_session")
        csrf = person.cookies.get("acc_csrf")
        out = await person.post("/api/auth/logout", headers={**ORIGIN, "x-acc-csrf": csrf})
        assert out.status_code == 200
        assert (await person.get("/api/auth/state")).json()["authenticated"] is False
    with db(api)() as s:
        assert s.get(AuthSession, auth.digest(token)) is None
    async with browser(api) as replay:
        replay.cookies.set("acc_session", token)
        assert (await replay.get("/api/sessions")).status_code == 401


async def test_turning_off_or_unbinding_ends_company_sessions(org):
    api, idp = org["api"], org["idp"]
    made = await preprovision(api, "alex", "alex-sub")
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
        await api.put("/api/organizations/oidc", json={"issuer": ISSUER, "client_id": idp.client_id, "enabled": False})
        assert (await person.get("/api/auth/state")).json()["authenticated"] is False
        await api.put("/api/organizations/oidc", json={"issuer": ISSUER, "client_id": idp.client_id,
                                                       "client_secret": idp.client_secret, "enabled": True})
        landed(await sso(person, idp, "alex-sub"))
        listing = (await api.get("/api/organizations/identities")).json()["identities"]
        assert [i["username"] for i in listing] == ["alex"] and listing[0]["person_id"] == made["person_id"]
        removed = await api.delete(f"/api/organizations/identities/{listing[0]['id']}", params={"resources": "transfer"})
        assert removed.status_code == 200
        assert (await person.get("/api/auth/state")).json()["authenticated"] is False
        landed(await sso(person, idp, "alex-sub"), "/login?sso=not_provisioned")


async def test_owner_authority_never_comes_from_the_provider(org):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    with db(api)() as s:
        s.scalar(select(User).where(User.username == "alex")).role = "owner"
        s.commit()
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"), "/login?sso=owner_local_only")


@pytest.mark.parametrize("failure", ["down", "wrong_issuer"])
async def test_break_glass_local_sign_in_works_when_the_provider_does_not(org, monkeypatch, failure):
    api, idp = org["api"], org["idp"]
    if failure == "down":
        idp.down = True
    else:
        idp.discovery_overrides = {"issuer": "https://misconfigured.example.com"}
    async with browser(api) as person:
        landed(await person.get("/api/auth/oidc/start"), "/login?sso=unavailable")
        monkeypatch.setattr(auth.time, "time", lambda: TOTP_AT + 30)
        response = await person.post("/api/auth/login", headers=ORIGIN, json={
            "username": "casey", "password": PASSWORD, "code": auth.totp(org["totp"], TOTP_AT + 30)})
        assert response.status_code == 200, response.text
        state = (await person.get("/api/auth/state")).json()
        assert state["role"] == "owner" and state["sign_in_method"] == "local"
        # ...and can turn company sign-in off from there.
        csrf = person.cookies.get("acc_csrf")
        off = await person.put("/api/organizations/oidc", headers={**ORIGIN, "x-acc-csrf": csrf},
                               json={"issuer": ISSUER, "client_id": idp.client_id, "enabled": False})
        assert off.status_code == 200, off.text


async def test_no_tokens_in_logs_urls_or_audit(org, caplog):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    caplog.set_level(logging.DEBUG)
    seen = []
    original = idp.handle

    def spy(request):
        response = original(request)
        if request.url.path.endswith("/token") and response.status_code == 200:
            seen.extend([response.json()["id_token"], response.json()["access_token"]])
        return response
    idp.transport = httpx.MockTransport(spy)
    api.app.state.ctx.oidc = Providers(transport=idp.transport)
    async with browser(api) as person:
        response = await sso(person, idp, "alex-sub")
        landed(response)
        idp.token_overrides = {"aud": "another-app"}
        failed = await sso(person, idp, "alex-sub")
    assert seen
    with db(api)() as s:
        audit = " ".join(f"{a.target} {a.detail}" for a in s.scalars(select(AuditLog)))
    for secret in seen + [idp.client_secret]:
        assert secret not in caplog.text
        assert secret not in audit
        assert secret not in response.headers["location"] and secret not in failed.headers["location"]


async def test_members_cannot_reach_company_sign_in_settings(org):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    async with browser(api) as member:
        landed(await sso(member, idp, "alex-sub"))
        headers = {**ORIGIN, "x-acc-csrf": member.cookies.get("acc_csrf")}
        for method, path, body in [
            ("GET", "/api/organizations/oidc", None), ("PUT", "/api/organizations/oidc", {"enabled": False}),
            ("POST", "/api/organizations/oidc/test", {}), ("GET", "/api/organizations/identities", None),
            ("POST", "/api/organizations/identities", {"username": "zed", "subject": "zed-sub", "account": "zed-agent"}),
            ("DELETE", "/api/organizations/identities/1", None),
            ("POST", "/api/organizations/sso-invites", {"username": "zed", "account": "zed-agent", "email": "zed@example.com"}),
            ("DELETE", "/api/organizations/sso-invites/1", None),
            ("GET", "/api/audit", None),
        ]:
            response = await member.request(method, path, json=body, headers=headers)
            assert response.status_code == 403, (method, path, response.status_code)


async def test_company_only_members_cannot_add_local_credentials(org):
    """An SSO-only member has no usable password or authenticator, so they can't verify their sign-in to add a
    passkey or change the password: disabling them at the provider can't be sidestepped locally."""
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    async with browser(api) as member:
        landed(await sso(member, idp, "alex-sub"))
        headers = {**ORIGIN, "x-acc-csrf": member.cookies.get("acc_csrf")}
        begin = await member.post("/api/auth/passkeys/register/begin", json={}, headers=headers)
        assert begin.status_code in (401, 403), begin.text
        reauth = await member.post("/api/auth/passkeys/reauth", json={"password": "", "code": "000000"}, headers=headers)
        assert reauth.status_code in (401, 403, 422), reauth.text
        password = await member.post("/api/auth/password", json={"current_password": "x" * 12, "new_password": "y" * 16},
                                     headers=headers)
        assert password.status_code == 403, password.text



# ---------------------------------------------------------------- owner answers 2026-10-25

async def require_member_passkeys(api):
    from acc.server.models import PasskeyPolicy
    with db(api)() as s:
        row = s.get(PasskeyPolicy, 1) or PasskeyPolicy(id=1, owners="allow")
        row.members = "require"
        s.merge(row)
        s.commit()


def test_mfa_is_proven_only_by_the_token():
    for amr in (["pwd", "mfa"], ["hwk"], ["fido", "user"]):
        assert oidc.mfa_proven({"amr": amr})
    assert not oidc.mfa_proven({})
    assert not oidc.mfa_proven({"amr": ["pwd", "otp"]})  # one factor each; only the provider can say "mfa"
    assert not oidc.mfa_proven({"amr": "mfa"})  # amr is a list; a bare string proves nothing
    assert not oidc.mfa_proven({"amr": [{"mfa": 1}]})
    # acr counts only when the owner named that value for this tenant; none count by default.
    pape = "http://schemas.openid.net/pape/policies/2007/06/multi-factor"
    assert not oidc.mfa_proven({"acr": pape})
    assert oidc.mfa_proven({"acr": pape}, [pape])
    assert oidc.mfa_proven({"acr": "c2"}, ["c1", "c2"])
    assert not oidc.mfa_proven({"acr": "c3"}, ["c1", "c2"])
    assert not oidc.mfa_proven({"acr": ["c1"]}, ["c1"])
    assert not oidc.mfa_proven({"acr": "1"}, ["1"])  # never, even if a list slipped past the settings check


def test_owner_acr_values_are_checked():
    assert oidc.acr_refusal([]) is None
    assert oidc.acr_refusal(["urn:example:loa:mfa", "c1"]) is None
    for bad in (["0"], ["1"], ["two words"], [""], ["x" * 201], ["tab\there"], ["c%d" % i for i in range(11)]):
        assert oidc.acr_refusal(bad), bad


async def test_without_required_passkeys_the_company_sign_in_is_enough(org):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
        assert (await person.get("/api/auth/state")).json()["step_up_required"] is False


async def test_required_passkeys_need_mfa_proof_or_a_passkey(org):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    await require_member_passkeys(api)
    async with browser(api) as person:  # no MFA claim and no passkey: refused, nothing half-open
        landed(await sso(person, idp, "alex-sub"), "/login?sso=mfa_required")
        assert (await person.get("/api/auth/state")).json()["authenticated"] is False
    idp.token_overrides = {"amr": ["pwd", "mfa"]}
    async with browser(api) as person:  # the token proves MFA: in, no extra step
        landed(await sso(person, idp, "alex-sub"))
        state = (await person.get("/api/auth/state")).json()
        assert state["authenticated"] is True and state["step_up_required"] is False
        assert (await person.get("/api/sessions")).status_code == 200
    with db(api)() as s:
        login = s.scalars(select(AuditLog).where(AuditLog.action == "auth.login")).all()[-1]
        assert login.detail["mfa"] is True and login.detail["passkey_step_up"] is False


async def test_without_mfa_proof_a_member_with_a_passkey_must_also_use_it(org):
    from test_passkeys import Authenticator, b64

    from acc.server.models import Passkey
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    await require_member_passkeys(api)
    key = Authenticator()
    import cbor2
    numbers = key.key.public_key().public_numbers()
    cose = cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})
    with db(api)() as s:
        user = s.scalar(select(User).where(User.username == "alex"))
        s.add(Passkey(user_id=user.id, credential_id=b64(key.id), public_key=b64(cose), rp_id="acc.example.com",
                      name="Laptop"))
        s.commit()
    origin = "https://acc.example.com"
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
        state = (await person.get("/api/auth/state")).json()
        assert state["authenticated"] is True and state["step_up_required"] is True
        assert "instance" not in state
        # Nothing else works until the passkey check: not reads, not terminals, not settings.
        assert (await person.get("/api/sessions")).status_code == 401
        assert (await person.get("/api/projects")).status_code == 401
        headers = {"Origin": origin, "x-acc-csrf": person.cookies.get("acc_csrf")}
        begin = await person.post("/api/auth/passkeys/verify/begin", headers=headers, json={})
        assert begin.status_code == 200, begin.text
        options = begin.json()["options"]
        done = await person.post("/api/auth/passkeys/verify/complete", headers=headers, json={
            "challenge_id": begin.json()["challenge_id"], "credential": key.credential(options, origin=origin)})
        assert done.status_code == 200, done.text
        assert (await person.get("/api/auth/state")).json()["step_up_required"] is False
        assert (await person.get("/api/sessions")).status_code == 200
    async with browser(api) as other:  # sign-out works while the check is pending
        landed(await sso(other, idp, "alex-sub"))
        out = await other.post("/api/auth/logout", headers={**ORIGIN, "x-acc-csrf": other.cookies.get("acc_csrf")})
        assert out.status_code == 200
        assert (await other.get("/api/auth/state")).json()["authenticated"] is False


async def test_invite_is_bound_to_the_invited_directory_account(org):
    api, idp = org["api"], org["idp"]
    response = await api.post("/api/organizations/sso-invites",
                              json={"username": "dana", "account": "dana-agent", "email": "dana@example.com"})
    token = parse_qs(urlsplit(response.json()["url"]).query)["invite"][0]
    expires = auth.datetime.fromisoformat(response.json()["expires_at"])
    assert auth.timedelta(days=6, hours=23) < expires.replace(tzinfo=auth.timezone.utc) - auth.now() <= auth.timedelta(days=7)
    # Someone else from the same tenant opens the forwarded link: refused, and the link still works for Dana.
    idp.token_overrides = {"preferred_username": "mallory@example.com"}
    async with browser(api) as mallory:
        landed(await sso(mallory, idp, "mallory-sub", invite=token), "/login?sso=invite_mismatch")
    # An unverified email naming Dana doesn't count either.
    idp.token_overrides = {"email": "dana@example.com", "email_verified": False}
    async with browser(api) as mallory:
        landed(await sso(mallory, idp, "mallory-sub", invite=token), "/login?sso=invite_mismatch")
    with db(api)() as s:
        assert s.scalar(select(User).where(User.username == "dana")) is None
    idp.token_overrides = {"email": "DANA@example.com", "email_verified": True}
    async with browser(api) as dana:
        landed(await sso(dana, idp, "dana-sub", invite=token))
        assert (await dana.get("/api/auth/state")).json()["username"] == "dana"
    # The identity is the subject, not the email: a later token with another email still signs Dana in.
    idp.token_overrides = {"email": "renamed@example.com", "email_verified": True}
    async with browser(api) as dana:
        landed(await sso(dana, idp, "dana-sub"))
    bad = await api.post("/api/organizations/sso-invites", json={"username": "erin", "account": "erin-agent"})
    assert bad.status_code == 422  # the directory account is required


@pytest.mark.parametrize("entra,claims,returned", [
    (True, {"preferred_username": "Preferred@Example.com", "upn": "Upn@Example.com",
            "email": "Mail@Example.com", "email_verified": True}, "Preferred@Example.com"),
    (False, {"preferred_username": "Untrusted@Example.com", "upn": "Upn@Example.com",
             "email": "Mail@Example.com", "email_verified": True}, "Upn@Example.com"),
    (False, {"email": "Mail@Example.com", "email_verified": True}, "Mail@Example.com"),
    (False, {"preferred_username": "Untrusted@Example.com", "email": "Unverified@Example.com",
             "email_verified": False}, None),
], ids=["entra-preferred", "generic-upn", "verified-email", "no-trusted-account"])
async def test_invite_mismatch_audits_a_matcher_eligible_account_only_for_owners(org, entra, claims, returned):
    api, idp = org["api"], org["idp"]
    if entra:
        idp = TestIdP("https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/v2.0")
        api.app.state.ctx.oidc = Providers(transport=idp.transport)
        response = await api.put("/api/organizations/oidc", json={
            "issuer": idp.issuer, "client_id": idp.client_id, "client_secret": idp.client_secret, "enabled": True})
        assert response.status_code == 200, response.text
    made = (await api.post("/api/organizations/sso-invites", json={
        "username": "dana", "email": "dana@example.com", "account": "dana-agent"})).json()
    token = parse_qs(urlsplit(made["url"]).query)["invite"][0]
    idp.token_overrides = claims
    async with browser(api) as person:
        response = await sso(person, idp, "mallory-sub", invite=token)
        landed(response, "/login?sso=invite_mismatch")
        assert (await person.get("/api/audit")).status_code == 401
        assert not any(value in response.headers["location"] for value in (returned, "dana@example.com") if value)
    rows = (await api.get("/api/audit", params={"action": "auth.sso_failed"})).json()
    assert rows[0]["detail"] == {
        "reason": "invite_mismatch", "issuer": idp.issuer, "subject": "mallory-sub",
        "invite": made["id"], "invited_account": "dana@example.com", "returned_account": returned}
    with db(api)() as s:
        assert s.get(SSOInvite, made["id"]).used_at is None
        assert s.scalar(select(User).where(User.username == "dana")) is None


async def test_invite_expires_after_seven_days(org):
    from acc.server.models import SSOInvite
    api = org["api"]
    response = await api.post("/api/organizations/sso-invites",
                              json={"username": "dana", "account": "dana-agent", "email": "dana@example.com"})
    token = parse_qs(urlsplit(response.json()["url"]).query)["invite"][0]
    with db(api)() as s:
        invite = s.scalar(select(SSOInvite))
        invite.expires_at = auth.now() - auth.timedelta(seconds=1)
        s.commit()
    async with browser(api) as dana:
        landed(await dana.get("/api/auth/oidc/start", params={"invite": token}), "/login?sso=invite_expired")


async def test_session_length_is_an_owner_setting(org):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
    base = {"issuer": ISSUER, "client_id": idp.client_id, "enabled": True}
    for bad in (0, 25):
        assert (await api.put("/api/organizations/oidc", json={**base, "session_hours": bad})).status_code == 422
    response = await api.put("/api/organizations/oidc", json={**base, "session_hours": 2})
    assert response.status_code == 200 and response.json()["session_hours"] == 2
    with db(api)() as s:  # the sign-in already open is shortened too
        record = s.scalar(select(AuthSession).where(AuthSession.method == "oidc"))
        assert record.expires_at - record.created_at <= auth.timedelta(hours=2)
    async with browser(api) as person:
        response = await sso(person, idp, "alex-sub")
        landed(response)
        assert "Max-Age=7200" in response.headers.get("set-cookie", "")
    with db(api)() as s:
        newest = s.scalars(select(AuthSession).where(AuthSession.method == "oidc").order_by(AuthSession.created_at)).all()[-1]
        assert auth.timedelta(hours=1, minutes=59) < newest.expires_at - newest.created_at <= auth.timedelta(hours=2)


async def test_scopes_ask_for_the_directory_account(org):
    api = org["api"]
    async with browser(api) as person:
        _, query = await start(person)
    assert query["scope"] == ["openid profile email"]


# ---------------------------------------------------------------- security review 2026-10-25

async def test_promotion_to_owner_ends_the_authority_of_company_sign_ins(org):
    """Owners are local-only: a member's company sign-in must not carry owner authority after a promotion (the
    callback refuses new ones, but an open session kept working with the owner role until it expired)."""
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    async with browser(api) as alex:
        landed(await sso(alex, idp, "alex-sub"))
        assert (await alex.get("/api/organizations/oidc")).status_code == 403  # a member
        with db(api)() as s:
            s.scalar(select(User).where(User.username == "alex")).role = "owner"
            s.commit()
        assert (await alex.get("/api/organizations/oidc")).status_code == 401
        assert (await alex.get("/api/auth/state")).json()["authenticated"] is False


def test_preferred_username_counts_only_where_the_directory_sets_it():
    """OIDC Core: preferred_username is any string the RP must not rely on; outside Entra (where it is the
    administrator-set UPN) some providers let people choose it. A verified email or upn still counts everywhere."""
    generic = {"iss": "https://idp.example.com/tenant-a", "preferred_username": "dana@example.com"}
    assert oidc.directory_names(generic) == set()
    entra = {"iss": "https://login.microsoftonline.com/0000-tenant/v2.0", "preferred_username": "Dana@Example.com"}
    assert oidc.directory_names(entra) == {"dana@example.com"}
    assert oidc.directory_names({"iss": generic["iss"], "upn": "dana@example.com"}) == {"dana@example.com"}
    assert oidc.directory_names({"iss": generic["iss"], "email": "dana@example.com", "email_verified": True}) == {"dana@example.com"}


def test_directory_names_fold_ascii_case_only():
    """str.lower() maps look-alikes onto ASCII: KELVIN SIGN (U+212A) becomes "k", so "Kim@example.com" matched
    an invite for kim@example.com. Only ASCII letters are case-folded; anything else must match exactly."""
    claims = {"iss": "https://idp.example.com/tenant-a", "email": "Kim@example.com", "email_verified": True}
    assert "kim@example.com" not in oidc.directory_names(claims)
    assert oidc.directory_names({**claims, "email": "KIM@Example.COM"}) == {"kim@example.com"}


# ---------------------------------------------------------------- open sign-in attempts (review finding 4)

CROSS_SITE = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image"}


def refused_start(response):
    """The shared Origin/Sec-Fetch middleware (403) or the start route itself (cross_site): either way nothing is stored."""
    assert (response.status_code == 403 or response.headers.get("location") == "/login?sso=cross_site"), response.text


def open_flows(api):
    with db(api)() as s:
        return len(list(s.scalars(select(OIDCFlow))))


async def test_a_cross_site_page_cannot_start_sign_ins_or_use_up_the_address(org):
    """<img src=…/start> from any page a person behind the office NAT visits: refused, nothing stored or counted."""
    api, idp = org["api"], org["idp"]
    async with browser(api) as page:
        for _ in range(40):
            refused_start(await page.get("/api/auth/oidc/start", headers=CROSS_SITE))
        # A top-level navigation from another page (window.location) is refused by the route.
        navigate = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
        landed(await page.get("/api/auth/oidc/start", headers=navigate), "/login?sso=cross_site")
        # Same-site counts as foreign too: published project sites can share Rynhult's registrable domain.
        landed(await page.get("/api/auth/oidc/start", headers={**navigate, "sec-fetch-site": "same-site"}),
               "/login?sso=cross_site")
        refused_start(await page.get("/api/auth/oidc/start", headers={"origin": "https://evil.example.net"}))
    assert open_flows(api) == 0
    await preprovision(api, "alex", "alex-sub")
    async with browser(api) as alex:  # own login page (same-origin) or typed address (none) still work
        alex.headers["sec-fetch-site"] = "same-origin"
        landed(await sso(alex, idp, "alex-sub"))
    # 40 forged callbacks without this browser's flow cookie don't lock the address either.
    async with browser(api) as page:
        for _ in range(40):
            await page.get("/api/auth/oidc/callback", params={"state": "forged", "code": "x"},
                           headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"})
    async with browser(api) as alex:
        alex.headers["sec-fetch-site"] = "none"
        landed(await sso(alex, idp, "alex-sub"))


async def test_sign_in_links_work_from_mail_but_not_from_a_foreign_origin(org):
    api, idp = org["api"], org["idp"]
    response = await api.post("/api/organizations/sso-invites",
                              json={"username": "dana", "account": "dana-agent", "email": "dana@example.com"})
    token = parse_qs(urlsplit(response.json()["url"]).query)["invite"][0]
    async with browser(api) as mallory:
        refused_start(await mallory.get("/api/auth/oidc/start", params={"invite": token},
                                        headers={"origin": "https://evil.example.net"}))
    idp.token_overrides = {"email": "dana@example.com", "email_verified": True}
    async with browser(api) as dana:  # clicked in a mail client or web mail: a cross-site navigation
        dana.headers.update({"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"})
        landed(await sso(dana, idp, "dana-sub", invite=token))


async def test_open_attempts_are_capped_per_browser_address_and_link(org):
    from acc.server.api import organizations
    api = org["api"]
    async with browser(api) as one:  # one browser restarting supersedes its own open attempt
        for _ in range(organizations.MAX_OPEN_PER_ADDRESS + 5):
            await start(one)
    assert open_flows(api) == 1
    response = await api.post("/api/organizations/sso-invites",
                              json={"username": "dana", "account": "dana-agent", "email": "dana@example.com"})
    token = parse_qs(urlsplit(response.json()["url"]).query)["invite"][0]
    for _ in range(organizations.MAX_OPEN_PER_INVITE):
        async with browser(api) as other:
            await start(other, invite=token)
    async with browser(api) as other:  # one link cannot hold more open attempts than this
        landed(await other.get("/api/auth/oidc/start", params={"invite": token}), "/login?sso=throttled")
    while open_flows(api) < organizations.MAX_OPEN_PER_ADDRESS:
        async with browser(api) as other:  # clients without cookies (scripts on this address)
            await start(other)
    async with browser(api) as other:
        landed(await other.get("/api/auth/oidc/start"), "/login?sso=throttled")


async def test_the_providers_redirect_back_is_a_cross_site_navigation_and_completes(org):
    """Browsers mark the IdP's redirect to the callback Sec-Fetch-Site: cross-site, Sec-Fetch-Mode: navigate. The shared
    middleware refused every cross-site request under /api/, so no real browser could finish a company sign-in."""
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    async with browser(api) as alex:
        alex.headers["sec-fetch-site"] = "same-origin"  # the button on Rynhult's sign-in page
        response, query = await start(alex)
        code = idp.issue_code("alex-sub", query)
        landed(await alex.get("/api/auth/oidc/callback", params={"state": query["state"][0], "code": code},
                              headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate",
                                       "sec-fetch-dest": "document"}))
        assert (await alex.get("/api/auth/state")).json()["username"] == "alex"
    # Other cross-site requests to these routes, and to the rest of /api/, stay refused.
    async with browser(api) as page:
        for path in ("/api/auth/oidc/callback", "/api/auth/state"):
            response = await page.get(path, headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors"})
            assert response.status_code == 403, path
        response = await page.post("/api/auth/oidc/callback", headers={"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"})
        assert response.status_code == 403


# ---------------------------------------------------------------- provider addresses (review finding 5)

def dns(table: dict):
    """A resolver for tests: host -> addresses; anything else does not resolve."""
    calls = []

    def resolve(host, port):
        calls.append((host, port))
        if host not in table:
            raise OIDCError("provider_unavailable", "cannot resolve the provider's address")
        return sorted(table[host])
    resolve.calls = calls
    return resolve


@pytest.mark.parametrize("address", ["10.0.0.5", "192.168.1.9", "127.0.0.1", "::1", "169.254.169.254",
                                     "fd00:ec2::254", "100.100.100.200", "0.0.0.0", "::ffff:10.0.0.5"])
def test_private_loopback_link_local_and_metadata_providers_are_refused(idp, address):
    rp = Provider(ISSUER, idp.client_id, transport=idp.transport, resolve=dns({"idp.example.com": [address]}))
    with pytest.raises(OIDCError) as caught:
        rp.discovery()
    assert caught.value.reason == "private_address"
    assert idp.requests == []  # refused before any connection


def test_an_endpoint_on_another_host_is_checked_too(idp):
    idp.discovery_overrides = {"jwks_uri": "https://keys.internal.example/keys"}
    rp = Provider(ISSUER, idp.client_id, transport=idp.transport,
                  resolve=dns({"idp.example.com": ["93.184.216.34"], "keys.internal.example": ["10.9.9.9"]}))
    rp.discovery()
    with pytest.raises(OIDCError, match="private_address"):
        rp.refresh_keys()


def test_an_owner_allowed_lan_host_works_until_its_dns_changes(idp):
    table = {"idp.example.com": ["10.1.2.3"]}
    allowed = ({"host": "idp.example.com", "port": 443, "addresses": ["10.1.2.3"]},)
    rp = Provider(ISSUER, idp.client_id, transport=idp.transport, resolve=dns(table), allowed=allowed)
    assert rp.refresh_keys()
    table["idp.example.com"] = ["10.1.2.4"]
    with pytest.raises(OIDCError, match="address_changed"):
        Provider(ISSUER, idp.client_id, transport=idp.transport, resolve=dns(table), allowed=allowed).discovery()
    # An allowlist entry never extends to loopback, link-local or metadata addresses.
    for address in ("127.0.0.1", "169.254.169.254"):
        entry = ({"host": "idp.example.com", "port": 443, "addresses": [address]},)
        with pytest.raises(OIDCError):
            Provider(ISSUER, idp.client_id, transport=idp.transport, resolve=dns({"idp.example.com": [address]}),
                     allowed=entry).discovery()


def test_real_connections_are_pinned_to_the_checked_address():
    seen = []

    def network(request):
        seen.append(request)
        return httpx.Response(200, json={})
    transport = oidc.PinnedTransport(dns({"idp.example.com": ["93.184.216.34"]}), (), network=httpx.MockTransport(network))
    with httpx.Client(transport=transport) as client:
        client.get("https://idp.example.com/tenant-a/.well-known/openid-configuration")
    assert seen[0].url.host == "93.184.216.34" and seen[0].headers["host"] == "idp.example.com"
    assert seen[0].extensions["sni_hostname"] == "idp.example.com"
    # Without a test double, providers always resolve and check (fail closed).
    assert Provider(ISSUER, "c").resolve is oidc.system_resolve
    assert Providers().get(ISSUER, "c", 1).resolve is oidc.system_resolve


async def test_owner_allowlist_for_a_private_provider_is_confirmed_and_audited(org):
    api, idp = org["api"], org["idp"]
    table = {"idp.example.com": ["10.1.2.3"], "loop.example.com": ["127.0.0.1"], "meta.example.com": ["169.254.169.254"]}
    api.app.state.ctx.oidc = Providers(transport=idp.transport, resolve=dns(table))
    refused_probe = (await api.post("/api/organizations/oidc/test", json={"issuer": ISSUER})).json()
    assert refused_probe["ok"] is False and refused_probe["reason"] == "private_address"
    async with browser(api) as person:  # sign-in refuses it too, without reaching the provider
        landed(await person.get("/api/auth/oidc/start"), "/login?sso=unavailable")
    for host in ("loop.example.com", "meta.example.com"):
        bad = await api.post("/api/organizations/oidc/private-hosts/resolve", json={"url": f"https://{host}/"})
        assert bad.status_code == 422, bad.text
    resolved = (await api.post("/api/organizations/oidc/private-hosts/resolve", json={"url": ISSUER})).json()
    assert resolved == {"host": "idp.example.com", "port": 443, "addresses": ["10.1.2.3"]}
    stale = await api.post("/api/organizations/oidc/private-hosts", json={"url": ISSUER, "addresses": ["10.1.2.99"]})
    assert stale.status_code == 422  # DNS must still say what the owner confirmed
    added = await api.post("/api/organizations/oidc/private-hosts", json={"url": ISSUER, "addresses": ["10.1.2.3"]})
    assert added.status_code == 201, added.text
    assert (await api.post("/api/organizations/oidc/test", json={"issuer": ISSUER})).json()["ok"] is True
    hosts = (await api.get("/api/organizations/oidc/private-hosts")).json()["hosts"]
    assert [(h["host"], h["port"], h["addresses"]) for h in hosts] == [("idp.example.com", 443, ["10.1.2.3"])]
    assert (await api.delete(f"/api/organizations/oidc/private-hosts/{hosts[0]['id']}")).status_code == 200
    assert (await api.post("/api/organizations/oidc/test", json={"issuer": ISSUER})).json()["reason"] == "private_address"
    with db(api)() as s:
        actions = [a.action for a in s.scalars(select(AuditLog).where(AuditLog.action.like("organizations.oidc_private_host%")))]
    assert actions == ["organizations.oidc_private_host_allowed", "organizations.oidc_private_host_removed"]


# ------------------------------------------------------------ follow-ups: tenant acr values, passkey enrollment


async def provider_settings(api, idp, headers=None, **changes):
    body = {"issuer": ISSUER, "client_id": idp.client_id, "enabled": True, **changes}
    return await api.put("/api/organizations/oidc", json=body, headers=headers)


async def verified_owner(api, totp, monkeypatch):
    """Headers for the owner's own sign-in (not the API token), verified again with password and a fresh code."""
    monkeypatch.setattr(auth.time, "time", lambda: TOTP_AT + 30)
    headers = {**ORIGIN, "X-ACC-CSRF": api.cookies.get("acc_csrf"), "Authorization": ""}
    reauth = await api.post("/api/auth/passkeys/reauth", headers=headers,
                            json={"password": PASSWORD, "code": auth.totp(totp, TOTP_AT + 30)})
    assert reauth.status_code == 200, reauth.text
    return headers


async def test_owner_named_acr_values_prove_mfa_for_this_tenant(org, monkeypatch):
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    await require_member_passkeys(api)
    for bad in (["1"], ["0"], ["two words"], ["c%d" % i for i in range(11)]):
        assert (await provider_settings(api, idp, mfa_acr=bad)).status_code == 422, bad
    headers = await verified_owner(api, org["totp"], monkeypatch)
    response = await provider_settings(api, idp, headers=headers, mfa_acr=[" c2 ", "c2", "urn:example:loa:mfa"])
    assert response.status_code == 200, response.text
    assert response.json()["mfa_acr"] == ["c2", "urn:example:loa:mfa"]  # trimmed, duplicates dropped, order kept
    idp.token_overrides = {"acr": "c1"}  # a level the owner didn't name: no proof, no passkey, refused
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"), "/login?sso=mfa_required")
    idp.token_overrides = {"acr": "c2"}
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
        assert (await person.get("/api/auth/state")).json()["step_up_required"] is False
    idp.token_overrides = {"amr": ["hwk"]}  # amr proof needs no owner setting
    assert (await provider_settings(api, idp, mfa_acr=[])).status_code == 200
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
    with db(api)() as s:
        changed = s.scalars(select(AuditLog).where(AuditLog.action == "organizations.oidc_changed")).all()
        assert changed[-2].detail["mfa_acr"] == ["c2", "urn:example:loa:mfa"]


async def register(client, key, origin="https://acc.example.com", name="Laptop"):
    headers = {"Origin": origin, "x-acc-csrf": client.cookies.get("acc_csrf")}
    begin = await client.post("/api/auth/passkeys/register/begin", headers=headers, json={})
    if begin.status_code != 200:
        return begin
    options = begin.json()["options"]
    return await client.post("/api/auth/passkeys/register/complete", headers=headers, json={
        "challenge_id": begin.json()["challenge_id"], "name": name,
        "credential": key.credential(options, registration=True, origin=origin)})


@pytest.mark.parametrize("company_only,has_key,expected", [
    (True, True, 200), (True, False, 409), (False, True, 409), (False, False, 409),
])
async def test_required_member_policy_exempts_only_company_only_recovery(
        org, monkeypatch, company_only, has_key, expected):
    from test_passkeys import Authenticator

    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    idp.token_overrides = {"amr": ["mfa"], "auth_time": int(time.time())}
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
        if has_key:
            assert (await register(person, Authenticator())).status_code == 200
    with db(api)() as s:
        user = s.scalar(select(User).where(User.username == "alex"))
        user.company_only = company_only
        assert user.recovery_hash is None
        user_id = user.id
        s.commit()
    headers = {**await verified_owner(api, org["totp"], monkeypatch), "Origin": "https://acc.example.com"}
    response = await api.post("/api/auth/passkeys/policy", headers=headers,
                              json={"owners": "allow", "members": "require"})
    assert response.status_code == expected, response.text
    assert (await api.get("/api/auth/passkeys", headers=headers)).json()["policy"]["members"] == (
        "require" if expected == 200 else "allow")
    if expected == 200:
        updated = await api.post(f"/api/users/{user_id}/role", headers=headers,
                                 json={"role": "member"})
        assert updated.status_code == 200, updated.text


@pytest.mark.parametrize("enabled", [True, False])
async def test_enrollment_sign_in_requests_recent_authentication(org, enabled):
    api, idp = org["api"], org["idp"]
    assert (await provider_settings(api, idp, passkey_enrollment=enabled)).status_code == 200
    async with browser(api) as person:
        _, query = await start(person)
    assert query.get("max_age") == (["600"] if enabled else None)


@pytest.mark.parametrize("age_or_claim,opens", [
    (0, True), (600, True), (601, False), (-1, False), (None, False),
    ("invalid", False), (True, False), (float("nan"), False), (float("inf"), False),
], ids=["fresh", "boundary", "stale", "future", "missing", "text", "boolean", "nan", "infinite"])
async def test_enrollment_window_requires_recent_auth_time(org, monkeypatch, age_or_claim, opens):
    from test_passkeys import Authenticator

    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    now = auth.now().replace(microsecond=0)
    monkeypatch.setattr(auth, "now", lambda: now)
    idp.token_overrides = {"amr": ["mfa"]}
    if age_or_claim is not None:
        idp.token_overrides["auth_time"] = (
            now.timestamp() - age_or_claim if type(age_or_claim) is int else age_or_claim)
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))  # stale/missing authentication only withholds enrollment
        listing = (await person.get("/api/auth/passkeys")).json()
        assert (listing["enrollment_until"] is not None) is opens
        assert (await register(person, Authenticator())).status_code == (200 if opens else 403)


async def test_company_only_member_adds_a_passkey_right_after_an_mfa_proven_sign_in(org):
    from test_passkeys import Authenticator

    from acc.server.models import Passkey
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    async with browser(api) as person:  # no MFA proof: nothing to stand in for verifying again
        landed(await sso(person, idp, "alex-sub"))
        listing = (await person.get("/api/auth/passkeys")).json()
        assert listing["company_only"] is True and listing["enrollment_until"] is None
        assert (await register(person, Authenticator())).status_code == 403
    idp.token_overrides = {"amr": ["pwd", "mfa"], "auth_time": int(time.time())}
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
        until = (await person.get("/api/auth/passkeys")).json()["enrollment_until"]
        assert until is not None
        left = auth.datetime.fromisoformat(until).replace(tzinfo=auth.timezone.utc) - auth.now()
        assert auth.timedelta(minutes=4) < left <= auth.timedelta(minutes=5)
        done = await register(person, Authenticator())
        assert done.status_code == 200, done.text
        # One passkey per window: the next needs another MFA-proven sign-in (or this passkey, to verify again).
        assert (await register(person, Authenticator(), name="Spare")).status_code == 403
        assert (await person.get("/api/auth/passkeys")).json()["enrollment_until"] is None
        # The window stands in for adding a passkey only: other changes still need verifying again.
        removal = await person.post(f"/api/auth/passkeys/{done.json()['id']}/remove",
                                    headers={"Origin": "https://acc.example.com", "x-acc-csrf": person.cookies.get("acc_csrf")})
        assert removal.status_code == 403
    with db(api)() as s:
        user = s.scalar(select(User).where(User.username == "alex"))
        assert [p.name for p in s.scalars(select(Passkey).where(Passkey.user_id == user.id))] == ["Laptop"]
        registered = s.scalar(select(AuditLog).where(AuditLog.action == "auth.passkey_registered"))
        assert registered.actor == "alex" and registered.detail == {"via": "company_mfa"}
        login = s.scalars(select(AuditLog).where(AuditLog.action == "auth.login")).all()[-1]
        assert login.detail["passkey_enrollment"] is True
    async with browser(api) as person:  # the window closes after five minutes
        landed(await sso(person, idp, "alex-sub"))
        with db(api)() as s:
            for record in s.scalars(select(AuthSession).where(AuthSession.passkey_enroll_until.is_not(None))):
                record.passkey_enroll_until = auth.now() - auth.timedelta(seconds=1)
            s.commit()
        assert (await person.get("/api/auth/passkeys")).json()["enrollment_until"] is None
        assert (await register(person, Authenticator(), name="Late")).status_code == 403


async def test_owners_can_turn_passkey_enrollment_off(org):
    from test_passkeys import Authenticator
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    idp.token_overrides = {"amr": ["mfa"], "auth_time": int(time.time())}
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
        assert (await person.get("/api/auth/passkeys")).json()["enrollment_until"] is not None
        response = await provider_settings(api, idp, passkey_enrollment=False)
        assert response.status_code == 200 and response.json()["passkey_enrollment"] is False
        assert (await person.get("/api/auth/state")).json()["authenticated"] is True  # the sign-in stays
        assert (await person.get("/api/auth/passkeys")).json()["enrollment_until"] is None  # its window closed
        assert (await register(person, Authenticator())).status_code == 403
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
        assert (await person.get("/api/auth/passkeys")).json()["enrollment_until"] is None
    with db(api)() as s:
        assert s.scalars(select(AuditLog).where(AuditLog.action == "auth.login")).all()[-1].detail["passkey_enrollment"] is False


async def test_weakening_company_mfa_needs_the_owner_to_verify_again(org, monkeypatch):
    # Counting another acr value as MFA, or letting company-only members add passkeys again, relaxes sign-in the way
    # relaxing the passkey policy does: the same fresh verification, never a copied owner cookie or the API token.
    api, idp = org["api"], org["idp"]
    cookie_only = {**ORIGIN, "X-ACC-CSRF": api.cookies.get("acc_csrf"), "Authorization": ""}
    assert (await provider_settings(api, idp, mfa_acr=["c2"])).status_code == 403  # the API token
    assert (await provider_settings(api, idp, headers=cookie_only, mfa_acr=["c2"])).status_code == 403  # not verified
    assert (await api.get("/api/organizations/oidc")).json()["mfa_acr"] == []
    # Tightening needs nothing extra, and re-saving what is already allowed isn't a change.
    assert (await provider_settings(api, idp, passkey_enrollment=False)).status_code == 200
    assert (await provider_settings(api, idp, passkey_enrollment=False, session_hours=8)).status_code == 200
    assert (await provider_settings(api, idp, passkey_enrollment=True)).status_code == 403
    with db(api)() as s:
        refusals = s.scalars(select(AuditLog).where(AuditLog.action == "organizations.oidc_change_refused")).all()
        assert [r.detail["reason"] for r in refusals] == ["verification_required"] * 3
        assert refusals[0].detail["mfa_acr_added"] == ["c2"] and refusals[-1].detail["passkey_enrollment"] is True
    headers = await verified_owner(api, org["totp"], monkeypatch)
    response = await provider_settings(api, idp, headers=headers, mfa_acr=["c2"], passkey_enrollment=True)
    assert response.status_code == 200, response.text
    assert response.json()["mfa_acr"] == ["c2"] and response.json()["passkey_enrollment"] is True
    assert (await provider_settings(api, idp, mfa_acr=["c2"])).status_code == 200  # unchanged: the API token may save
    assert (await provider_settings(api, idp, mfa_acr=[])).status_code == 200  # removing a value tightens


async def test_members_with_local_credentials_get_no_enrollment_window(org):
    api, idp = org["api"], org["idp"]
    made = await preprovision(api, "alex", "alex-sub")
    with db(api)() as s:  # as for a linked member, who keeps a password and authenticator to verify with
        s.scalar(select(User).where(User.username == made["username"])).company_only = False
        s.commit()
    idp.token_overrides = {"amr": ["mfa"], "auth_time": int(time.time())}
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
        listing = (await person.get("/api/auth/passkeys")).json()
        assert listing["company_only"] is False and listing["enrollment_until"] is None


async def test_reset_links_clear_company_only(org, monkeypatch):
    """A completed reset gives the member a password and authenticator of their own, so they are no longer company-only
    (#326: removing their company sign-in then keeps them). opf first pinned the opposite, unchanged behaviour."""
    from test_users import join

    api = org["api"]
    await preprovision(api, "alex", "alex-sub")
    with db(api)() as s:
        user_id = s.scalar(select(User.id).where(User.username == "alex"))
    headers = await verified_owner(api, org["totp"], monkeypatch)
    reset = await api.post(f"/api/users/{user_id}/reset", headers=headers)
    assert reset.status_code == 201, reset.text
    async with browser(api) as person:
        response = await join(person, reset.json()["link"], None)
        assert response.status_code == 200, response.text
    with db(api)() as s:
        assert s.get(User, user_id).company_only is False


def test_migration_marks_members_that_company_sign_in_created(tmp_path):
    import json
    import sqlite3

    from alembic import command

    from acc.server.db import alembic_config
    path = tmp_path / "upgrade.db"
    config = alembic_config(f"sqlite:///{path}")
    command.upgrade(config, "0053")
    with sqlite3.connect(path) as conn:
        for name in ("made", "invited", "linked", "local"):
            conn.execute("INSERT INTO user (username, password_hash, totp_secret, role, created_at) "
                         "VALUES (?, 'x', 'x', 'member', '2026-10-25')", (name,))
        for name, how in (("made", "preprovisioned"), ("invited", "invited"), ("linked", "linked")):
            conn.execute("INSERT INTO audit_log (ts, actor, action, target, detail) VALUES "
                         "('2026-10-25', 'casey', 'organizations.identity_bound', ?, ?)", (name, json.dumps({"how": how})))
        conn.execute("INSERT INTO oidc_provider (id, issuer, client_id, enabled, revision, session_hours, updated_at) "
                     "VALUES (1, 'https://idp.example.com', 'rynhult', 1, 1, 12, '2026-10-25')")
    command.upgrade(config, "head")
    with sqlite3.connect(path) as conn:
        assert dict(conn.execute("SELECT username, company_only FROM user").fetchall()) == {
            "made": 1, "invited": 1, "linked": 0, "local": 0}
        assert conn.execute("SELECT mfa_acr, passkey_enrollment FROM oidc_provider").fetchone() == ("[]", 1)
    command.downgrade(config, "0053")
    command.upgrade(config, "head")


@pytest.mark.parametrize("company_only", [True, False])
def test_0054_downgrade_protects_company_only_passkeys(tmp_path, company_only):
    import sqlite3

    from alembic import command

    from acc.server.db import alembic_config

    path = tmp_path / "downgrade.db"
    config = alembic_config(f"sqlite:///{path}")
    command.upgrade(config, "0054")
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO user (id, username, password_hash, totp_secret, role, created_at, company_only) "
                     "VALUES (1, 'alex', 'x', 'x', 'member', '2026-10-25', ?)", (company_only,))
        conn.execute("INSERT INTO passkey (user_id, credential_id, public_key, rp_id, name, sign_count, created_at) "
                     "VALUES (1, 'credential', 'key', 'acc.example.com', 'Laptop', 0, '2026-10-25')")
        before = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
    if company_only:
        with pytest.raises(RuntimeError, match="company-only.*passkeys"):
            command.downgrade(config, "0053")
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall() == before
            assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0054",)
            assert conn.execute("SELECT company_only FROM user").fetchone() == (1,)
            assert conn.execute("SELECT count(*) FROM passkey").fetchone() == (1,)
            conn.execute("DELETE FROM passkey")  # safe downgrade is possible after explicitly removing the credentials
    command.downgrade(config, "0053")
    command.upgrade(config, "head")


async def test_a_company_only_members_passkey_never_signs_in_on_its_own(org):
    from test_passkeys import Authenticator
    api, idp = org["api"], org["idp"]
    await preprovision(api, "alex", "alex-sub")
    idp.token_overrides = {"amr": ["mfa"], "auth_time": int(time.time())}
    key, origin = Authenticator(), "https://acc.example.com"
    async with browser(api) as person:
        landed(await sso(person, idp, "alex-sub"))
        assert (await register(person, key)).status_code == 200
    with db(api)() as s:
        handle = s.scalar(select(User).where(User.username == "alex")).passkey_handle
    async with browser(api) as person:  # removing the company sign-in, or disabling them upstream, must lock them out
        begin = await person.post("/api/auth/passkeys/login/begin", headers={"Origin": origin}, json={})
        assert begin.status_code == 200, begin.text
        done = await person.post("/api/auth/passkeys/login/complete", headers={"Origin": origin}, json={
            "challenge_id": begin.json()["challenge_id"],
            "credential": key.credential(begin.json()["options"], origin=origin, handle=handle)})
        assert done.status_code == 403 and "company account" in done.json()["detail"]
        assert (await person.get("/api/auth/state")).json()["authenticated"] is False
    await require_member_passkeys(api)
    idp.token_overrides = {}
    async with browser(api) as person:  # but it does confirm a company sign-in without MFA proof
        landed(await sso(person, idp, "alex-sub"))
        assert (await person.get("/api/auth/state")).json()["step_up_required"] is True
        headers = {"Origin": origin, "x-acc-csrf": person.cookies.get("acc_csrf")}
        begin = await person.post("/api/auth/passkeys/verify/begin", headers=headers, json={})
        done = await person.post("/api/auth/passkeys/verify/complete", headers=headers, json={
            "challenge_id": begin.json()["challenge_id"], "credential": key.credential(begin.json()["options"], origin=origin)})
        assert done.status_code == 200, done.text
        assert (await person.get("/api/sessions")).status_code == 200
    with db(api)() as s:
        refused = s.scalars(select(AuditLog).where(AuditLog.action == "auth.passkey_failed")).all()
        assert refused[-1].detail == {"reason": "company_only"}


@pytest.mark.parametrize('field', ['exp', 'iat', 'nbf'])
@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_break_it_6_nonfinite_dates_are_not_authentication_evidence(idp, rp, field, value):
    refused(rp, idp.sign(idp.claims('member', 'n-1') | {field: value}), 'malformed_token')


@pytest.mark.parametrize('field', ['sub', 'nonce', 'at_hash'])
def test_break_it_6_surrogate_claims_are_refused_without_encoding_errors(idp, rp, field):
    token = idp.sign(idp.claims('member', 'n-1') | {field: '\ud800'})
    refused(rp, token, 'malformed_token')


@pytest.mark.parametrize('acr', [[], {}, ['phr'], {'level': 'phr'}])
async def test_break_it_6_unknown_acr_shapes_do_not_crash_member_sign_in(org, acr):
    api, idp = org['api'], org['idp']
    await preprovision(api, 'member', 'member-sub')
    idp.token_overrides = {'acr': acr}
    async with browser(api) as person:
        landed(await sso(person, idp, 'member-sub'))
        assert (await person.get('/api/auth/state')).json()['authenticated']
    with db(api)() as session:
        audit = session.scalar(select(AuditLog).where(AuditLog.action == 'auth.login').order_by(AuditLog.id.desc()))
        assert audit.detail['mfa'] is False


@pytest.mark.parametrize('status', [200, 401])  # a refusal's body is read too, for the provider's error code
def test_break_it_6_token_response_read_is_bounded(idp, status):
    class HugeTokenResponse(httpx.SyncByteStream):
        reads = 0
        def __iter__(self):
            for _ in range(100):
                self.reads += 1
                yield b' ' * 65536

    stream = HugeTokenResponse()
    def respond(request):
        if request.method == 'POST':
            return httpx.Response(status, stream=stream)
        return idp.handle(request)
    provider = Provider(ISSUER, idp.client_id, transport=httpx.MockTransport(respond))
    with pytest.raises(OIDCError, match='token_refused') as refused:
        provider.exchange(code='test-code', redirect_uri='https://acc.example.com/callback',
                          verifier='test-verifier', client_secret=None)
    assert stream.reads <= oidc.MAX_RESPONSE // 65536 + 1
    assert refused.value.provider_detail == {}


@pytest.mark.parametrize('issuer', ['https://[', 'https://idp.example.com:bad', 'https://idp.example.com:99999'])
def test_break_it_6_malformed_issuer_is_a_controlled_refusal(issuer):
    with pytest.raises(OIDCError, match='bad_issuer'):
        oidc.check_issuer(issuer)


@pytest.mark.parametrize('endpoint', ['https://[', 'https://idp.example.com:bad', 'https://:secret@idp.example.com/token'])
def test_break_it_6_malformed_provider_endpoint_is_a_controlled_refusal(idp, rp, endpoint):
    idp.discovery_overrides = {'token_endpoint': endpoint}
    with pytest.raises(OIDCError, match='bad_discovery'):
        rp.discovery()


@pytest.mark.parametrize('change', ['disable', 'replace_client'])
async def test_break_it_6_inflight_callback_cannot_outlive_owner_provider_change(org, change):
    import asyncio
    import threading

    api, idp = org['api'], org['idp']
    await preprovision(api, 'member', 'member-sub')
    entered, release = threading.Event(), threading.Event()
    def slow_provider(request):
        if request.method == 'POST':
            entered.set()
            assert release.wait(10), 'test did not release its fake IdP'
        return idp.handle(request)
    api.app.state.ctx.oidc.transport = httpx.MockTransport(slow_provider)
    async with browser(api) as person:
        callback = asyncio.create_task(sso(person, idp, 'member-sub'))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            changed = await api.put('/api/organizations/oidc', json={
                'issuer': ISSUER,
                'client_id': 'replacement-client' if change == 'replace_client' else idp.client_id,
                'enabled': change != 'disable'})
            assert changed.status_code == 200, changed.text
        finally:
            release.set()
        response = await callback
        landed(response, '/login?sso=disabled')
        assert (await person.get('/api/auth/state')).json()['authenticated'] is False
    with db(api)() as session:
        assert session.scalar(select(AuthSession).where(AuthSession.method == 'oidc')) is None


# ---- review follow-ups (docs/security/2026-10-25-sso-followups-review.md) -----------------------------------------

SECRET = "Zq8~vX3pL0wN-secret-value"


@pytest.mark.parametrize("text,expected", [
    (f"secret {SECRET[:8]} is wrong", "secret [redacted] is wrong"),             # any 8-character piece
    (f"secret {SECRET[3:15]}!", "secret [redacted]!"),
    (f"secret {SECRET.upper()}", "secret [redacted]"),                           # case-insensitive
    (f"secret {SECRET.lower()[2:12]}", "secret [redacted]"),
    (f"secret {SECRET[:7]} only", f"secret {SECRET[:7]} only"),                  # under 8: not a credential run
    (f"two {SECRET[:8]} and {SECRET[-9:]}", "two [redacted] and [redacted]"),
])
def test_f1_redacts_any_8_character_piece_of_a_credential_in_any_case(text, expected):
    assert oidc.diagnostic_text(text, (SECRET,)) == expected


def test_f1_a_short_attacker_chosen_code_neither_garbles_the_text_nor_drops_the_code():
    """The callback's code comes from the query string: `code=v` used to turn every v into [redacted]."""
    details = oidc._token_error_details(
        {"error": "invalid_grant", "error_description": "AADSTS70008: The provided authorization code is invalid."},
        {"code": "v", "code_verifier": "x" * 86, "client_secret": SECRET})
    assert details == {"oauth_error": "invalid_grant", "aadsts_code": "AADSTS70008", "error_code": "AADSTS70008",
                       "error_text": "AADSTS70008: The provided authorization code is invalid."}


def test_f1_an_error_code_carrying_a_piece_of_a_credential_is_dropped():
    details = oidc._token_error_details({"error": SECRET[4:13].upper()},  # VX3PL0WN-: identifier-shaped, case-changed
                                        {"code": "c" * 40, "client_secret": SECRET})
    assert "oauth_error" not in details and "error_code" not in details


def test_f2_line_and_paragraph_separators_are_removed_and_odd_spaces_are_plain():
    text = "line1\u2028line2\u2029para\u00a0nbsp\u3000ideographic\u202e\u200b."
    assert oidc.diagnostic_text(text) == "line1line2para nbsp ideographic."


async def test_f3_refused_and_repeated_withdrawals_are_audited(org):
    api, idp = org["api"], org["idp"]
    made = []
    for account in ("dana-agent", "dana-other-agent"):
        response = await api.post("/api/organizations/sso-invites", json={
            "username": "dana", "account": account, "email": "dana@example.com"})
        made.append(response.json())
    used_token = parse_qs(urlsplit(made[1]["url"]).query)["invite"][0]
    idp.token_overrides = {"email": "dana@example.com", "email_verified": True}
    async with browser(api) as person:
        landed(await sso(person, idp, "dana-sub", invite=used_token))
    assert (await api.delete(f"/api/organizations/sso-invites/{made[0]['id']}")).status_code == 200
    assert (await api.delete(f"/api/organizations/sso-invites/{made[0]['id']}")).status_code == 200
    assert (await api.delete(f"/api/organizations/sso-invites/{made[1]['id']}")).status_code == 409
    assert (await api.delete("/api/organizations/sso-invites/999999999")).status_code == 404
    with db(api)() as s:
        rows = [(row.action, row.detail) for row in s.scalars(select(AuditLog).where(
            AuditLog.action.like("org.invite_withdraw%")).order_by(AuditLog.id))]
    assert rows == [
        ("org.invite_withdrawn", {"invite": made[0]["id"], "username": "dana"}),
        ("org.invite_withdraw_repeated", {"invite": made[0]["id"], "username": "dana"}),
        ("org.invite_withdraw_refused", {"invite": made[1]["id"], "username": "dana", "reason": "used"}),
        ("org.invite_withdraw_refused", {"invite": 999999999, "reason": "missing"}),
    ]


async def test_f4_a_withdrawn_invite_keeps_the_providers_error_for_the_owner(org):
    api, idp = org["api"], org["idp"]
    made = (await api.post("/api/organizations/sso-invites", json={
        "username": "dana", "email": "dana@example.com", "account": "dana-agent"})).json()
    token = parse_qs(urlsplit(made["url"]).query)["invite"][0]
    reached, release = threading.Event(), threading.Event()

    def exchange(request):
        if request.method == "POST" and request.url.path.endswith("/token"):
            reached.set()
            if not release.wait(10):
                raise httpx.ReadTimeout("test exchange was not released")
            return httpx.Response(401, json={"error": "invalid_client",
                                             "error_description": "AADSTS7000222: The provided client secret keys are expired."})
        return idp.handle(request)

    api.app.state.ctx.oidc = Providers(transport=httpx.MockTransport(exchange))
    async with browser(api) as person:
        pending = asyncio.create_task(sso(person, idp, "dana-sub", invite=token))
        try:
            assert await asyncio.to_thread(reached.wait, 10)
            assert (await api.delete(f"/api/organizations/sso-invites/{made['id']}")).status_code == 200
        finally:
            release.set()
            response = await pending
        landed(response, "/login?sso=invite_withdrawn")
    with db(api)() as s:
        detail = s.scalar(select(AuditLog).where(AuditLog.action == "auth.sso_failed")).detail
    assert detail["reason"] == "invite_withdrawn" and detail["invite"] == made["id"]
    assert detail["provider_reason"] == "token_refused" and detail["detail"] == "HTTP 401 from the token endpoint"
    assert detail["error_code"] == "AADSTS7000222" and detail["oauth_error"] == "invalid_client"
    assert detail["error_text"] == "AADSTS7000222: The provided client secret keys are expired."


# ---- a spent invite link is audited at the start, not just refused (owner's Entra test, 2026-10-26) ----------------

async def spent_invite(api, idp, how):
    made = (await api.post("/api/organizations/sso-invites", json={
        "username": "dana", "account": "dana-agent", "email": "dana@example.com"})).json()
    token = parse_qs(urlsplit(made["url"]).query)["invite"][0]
    if how == "used":
        idp.token_overrides = {"email": "dana@example.com", "email_verified": True}
        async with browser(api) as dana:
            landed(await sso(dana, idp, "dana-sub", invite=token))
    elif how == "withdrawn":
        assert (await api.delete(f"/api/organizations/sso-invites/{made['id']}")).status_code == 200
    else:
        with db(api)() as s:
            s.get(SSOInvite, made["id"]).expires_at = auth.now() - auth.timedelta(seconds=1)
            s.commit()
    return made["id"], token


def start_refusals(api, token):
    with db(api)() as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action == "auth.sso_failed")))
        assert not any(token in f"{row.target} {row.detail}" for row in s.scalars(select(AuditLog)))
    return [(row.target, row.detail) for row in rows]


@pytest.mark.parametrize("how", ["used", "expired", "withdrawn"])
async def test_a_spent_invite_link_is_audited_without_its_token(org, how):
    api, idp = org["api"], org["idp"]
    invite_id, token = await spent_invite(api, idp, how)
    answer = "invite_withdrawn" if how == "withdrawn" else "invite_expired"
    async with browser(api) as person:
        landed(await person.get("/api/auth/oidc/start", params={"invite": token}), f"/login?sso={answer}")
    assert start_refusals(api, token) == [("127.0.0.1", {"reason": f"invite_{how}", "invite": invite_id})]


@pytest.mark.parametrize("how", ["used", "expired", "withdrawn"])
async def test_an_invite_spent_during_discovery_is_audited_too(org, how):
    api, idp = org["api"], org["idp"]
    made = (await api.post("/api/organizations/sso-invites", json={
        "username": "dana", "account": "dana-agent", "email": "dana@example.com"})).json()
    token = parse_qs(urlsplit(made["url"]).query)["invite"][0]
    reached, release = threading.Event(), threading.Event()

    def discovery(request):
        if request.url.path.endswith("/.well-known/openid-configuration"):
            reached.set()
            if not release.wait(10):
                raise httpx.ReadTimeout("test discovery was not released")
        return idp.handle(request)

    api.app.state.ctx.oidc = Providers(transport=httpx.MockTransport(discovery))
    async with browser(api) as person:
        pending = asyncio.create_task(person.get("/api/auth/oidc/start", params={"invite": token}))
        try:
            assert await asyncio.to_thread(reached.wait, 10)
            with db(api)() as s:
                invite = s.get(SSOInvite, made["id"])
                if how == "used":
                    invite.used_at = auth.now()
                elif how == "withdrawn":
                    invite.withdrawn_at = auth.now()
                else:
                    invite.expires_at = auth.now() - auth.timedelta(seconds=1)
                s.commit()
        finally:
            release.set()
            response = await pending
        landed(response, f"/login?sso={'invite_withdrawn' if how == 'withdrawn' else 'invite_expired'}")
    assert start_refusals(api, token) == [("127.0.0.1", {"reason": f"invite_{how}", "invite": made["id"]})]


async def test_an_unknown_invite_token_is_refused_without_an_audit_row(org):
    """Nothing to name, and anyone can send one: it is not an owner-useful record (and not an anonymous write)."""
    async with browser(org["api"]) as person:
        landed(await person.get("/api/auth/oidc/start", params={"invite": "not-a-real-invite"}),
               "/login?sso=invite_expired")
    assert start_refusals(org["api"], "not-a-real-invite") == []


# ---- removing a company-only member from company sign-in removes the person (owner decision, 2026-10-26) ---------

def unbind_audit(api):
    with db(api)() as s:
        return [(row.action, row.target, row.detail) for row in s.scalars(select(AuditLog).where(
            AuditLog.action.in_(("organizations.identity_unbound", "user.remove"))).order_by(AuditLog.id))]


async def test_removing_a_company_only_member_removes_the_person(org):
    from acc.server.models import Project, RetiredUser
    api, idp = org["api"], org["idp"]
    made = await preprovision(api, "bob", "bob-sub")
    with db(api)() as s:
        s.add(Project(slug="bobs", name="bobs", owner="bob", account="bob-agent", workspace_path=str(api.app.state.ctx.settings.workspaces_root / "bobs")))
        s.commit()
    async with browser(api) as bob:
        landed(await sso(bob, idp, "bob-sub"))
        identity = (await api.get("/api/organizations/identities")).json()["identities"][0]
        assert identity["username"] == "bob" and identity["removes_person"] is True
        # Without the owner's choice for what bob owns, nothing happens.
        refused = await api.delete(f"/api/organizations/identities/{identity['id']}")
        assert refused.status_code == 422 and "transfer or archive" in refused.text
        assert (await bob.get("/api/auth/state")).json()["username"] == "bob"
        response = await api.delete(f"/api/organizations/identities/{identity['id']}", params={"resources": "archive"})
        assert response.status_code == 200, response.text
        assert response.json()["removed"] is True and response.json()["projects"] == 1
        assert (await bob.get("/api/auth/state")).json()["authenticated"] is False
        landed(await sso(bob, idp, "bob-sub"), "/login?sso=not_provisioned")
    with db(api)() as s:
        assert s.scalar(select(User).where(User.username == "bob")) is None
        assert s.scalar(select(ExternalIdentity)) is None
        assert s.scalar(select(RetiredUser).where(RetiredUser.username == "bob")).resources == "archive"
        assert s.scalar(select(Project).where(Project.slug == "bobs")).archived is True
    assert unbind_audit(api) == [
        ("organizations.identity_unbound", "bob", {"issuer": ISSUER, "subject": "bob-sub", "person_id": made["person_id"],
                                                   "person_removed": True}),
        ("user.remove", "bob", {"resources": "archive", "projects": 1, "sessions": 0, "keys": 0, "devices": 0}),
    ]


async def test_removing_a_linked_member_only_stops_company_sign_in(org):
    api, idp = org["api"], org["idp"]
    with db(api)() as s:
        s.add(User(username="erin", role="member", account="erin-agent", password_hash=auth.hash_password(PASSWORD),
                   totp_secret=auth.new_totp_secret()))
        s.commit()
    response = await api.post("/api/organizations/sso-invites", json={"username": "erin", "existing": True,
                                                                           "email": "erin@example.com"})
    token = parse_qs(urlsplit(response.json()["url"]).query)["invite"][0]
    with db(api)() as s:
        local, _ = auth.create_session(s, s.scalar(select(User).where(User.username == "erin")), "test")
        s.commit()
    async with browser(api) as erin:
        erin.cookies.set("acc_session", local, domain="acc.local")
        idp.token_overrides = {"upn": "erin@example.com"}
        landed(await sso(erin, idp, "erin-sub", invite=token))
        identity = (await api.get("/api/organizations/identities")).json()["identities"][0]
        assert identity["removes_person"] is False
        # A choice for what they own is ignored: nothing of theirs moves.
        response = await api.delete(f"/api/organizations/identities/{identity['id']}", params={"resources": "archive"})
        assert response.status_code == 200 and response.json() == {"ok": True, "removed": False}
        assert (await erin.get("/api/auth/state")).json()["authenticated"] is False
    with db(api)() as s:
        assert s.scalar(select(User).where(User.username == "erin")) is not None
    assert [action for action, _, _ in unbind_audit(api)] == ["organizations.identity_unbound"]


async def test_a_company_only_member_given_a_password_by_reset_is_no_longer_removed_with_it(org):
    from helpers import verified_post
    from test_auth import cookie_client
    from test_users import join
    api = org["api"]
    await preprovision(api, "bob", "bob-sub")
    with db(api)() as s:
        bob_id = s.scalar(select(User).where(User.username == "bob")).id
    bearer = api.headers["Authorization"]
    reset = await verified_post(cookie_client(api), f"/api/users/{bob_id}/reset")  # the owner's verified browser
    assert reset.status_code == 201, reset.text
    api.headers["Authorization"] = bearer
    async with browser(api) as bob:
        assert (await join(bob, reset.json()["link"], None)).status_code == 200
    with db(api)() as s:
        assert s.get(User, bob_id).company_only is False
    identity = (await api.get("/api/organizations/identities")).json()["identities"][0]
    assert identity["removes_person"] is False
    assert (await api.delete(f"/api/organizations/identities/{identity['id']}")).json() == {"ok": True, "removed": False}
    with db(api)() as s:
        assert s.get(User, bob_id) is not None


async def test_an_owner_is_never_removed_by_removing_their_company_sign_in(org):
    """A company-only member promoted to owner keeps their place: owners never sign in through the provider, so the
    binding is only removed (review M-1). People > Remove stays the one way to remove an owner."""
    api = org["api"]
    await preprovision(api, "bob", "bob-sub")
    with db(api)() as s:
        bob_id = s.scalar(select(User).where(User.username == "bob")).id
    promoted = await api.post(f"/api/users/{bob_id}/role", json={"role": "owner"})
    assert promoted.status_code == 200, promoted.text
    identity = (await api.get("/api/organizations/identities")).json()["identities"][0]
    assert identity["removes_person"] is False and identity["owner"] is True
    response = await api.delete(f"/api/organizations/identities/{identity['id']}", params={"resources": "archive"})
    assert response.status_code == 200 and response.json() == {"ok": True, "removed": False}
    with db(api)() as s:
        bob = s.get(User, bob_id)
        assert bob is not None and bob.role == "owner"
        assert s.scalar(select(ExternalIdentity)) is None
    assert [action for action, _, _ in unbind_audit(api)] == ["organizations.identity_unbound"]


async def test_an_owner_promoted_between_the_check_and_the_removal_is_kept(org, monkeypatch):
    """The removal's own transaction checks the role again (review M-1)."""
    api = org["api"]
    await preprovision(api, "bob", "bob-sub")
    identity = (await api.get("/api/organizations/identities")).json()["identities"][0]
    remove_person = api.app.state.ctx.remove_person

    async def promoted_meanwhile(user_id, resources, who, also=None):
        with db(api)() as s:
            s.get(User, user_id).role = "owner"
            s.commit()
        return await remove_person(user_id, resources, who, also=also)
    monkeypatch.setattr(api.app.state.ctx, "remove_person", promoted_meanwhile)
    response = await api.delete(f"/api/organizations/identities/{identity['id']}", params={"resources": "archive"})
    assert response.status_code == 409, response.text
    with db(api)() as s:
        assert s.scalar(select(User).where(User.username == "bob")).role == "owner"
        assert s.scalar(select(ExternalIdentity)) is not None
    assert unbind_audit(api) == []
