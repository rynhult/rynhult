"""OpenID Connect relying party for company sign-in (docs/ORGANIZATIONS.md, phase 1).

Authorization code flow with PKCE (S256), one-use state and nonce, one exact redirect URI. ID tokens are checked
here, with `cryptography` only: RS256 signatures from the issuer's JWKS (no alg=none, no HMAC, no keys or key URLs
taken from the token), exact issuer, audience/azp, exp/iat/nbf within a bounded clock skew, nonce and at_hash.
Discovery is fetched only from the configured https issuer and must name that same issuer; endpoints must be https.
Every request resolves its host first and refuses private, loopback, link-local and metadata addresses unless an
owner allowed that host and its exact DNS answer (LAN ranges only); connections go to the checked address.
Raw provider responses and tokens never go to logs or URLs: callers get an OIDCError with a fixed reason,
local detail and bounded, redacted provider diagnostics for the owner audit.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import math
import re
import secrets
import socket
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote, quote_plus, urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .issue_sources import allowed_address

ALGORITHMS = ("RS256",)  # Entra ID, Google, Okta and Keycloak all sign ID tokens with it by default
CLOCK_SKEW = 120  # seconds either way for exp/iat/nbf
MAX_TOKEN_AGE = 600  # an ID token issued longer ago than this is not a fresh sign-in
ENROLLMENT_MAX_AUTH_AGE = 600  # provider authentication age, not token issuance age
CACHE_SECONDS = 3600  # discovery and keys are refetched at most this long after the last fetch
MIN_REFRESH = 60  # an unknown key id refetches the JWKS at most this often (rotation, without letting tokens drive fetches)
MAX_RESPONSE = 1 << 20
TIMEOUT = 10.0
FLOW_SECONDS = 600
B64URL = re.compile(r"^[A-Za-z0-9_-]*$")


class OIDCError(Exception):
    """A refused sign-in or provider problem. `reason` is a fixed code, safe for audit and the login page."""

    def __init__(self, reason: str, detail: str = "", *, provider_detail: dict[str, str] | None = None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail
        self.provider_detail = provider_detail or {}


SECRET_RUN = 8  # any piece of a known credential at least this long is redacted, in any case


def _plain(text: str) -> str:
    """No controls, format characters (bidi, zero-width) or line/paragraph separators; other spaces become ' '."""
    out = []
    for c in text:
        category = unicodedata.category(c)
        if category[0] == "C" or category in ("Zl", "Zp"):
            continue
        out.append(" " if category == "Zs" else c)
    return "".join(out)


def _lower(text: str) -> str:
    # One code point in, one out (str.lower turns some into two), so positions match the original text.
    return "".join(low if len(low := c.lower()) == 1 else c for c in text)


def _pieces(sensitive) -> set[str]:
    """Every SECRET_RUN-character piece of each credential, as sent, without controls and URL-encoded, lowercased.
    A shorter value can't be a credential (the callback's code is whatever the caller typed) and is ignored."""
    pieces = set()
    for secret in sensitive:
        for text in {secret, _plain(secret)} if secret else ():
            for variant in {text, quote(text, safe=""), quote_plus(text)}:
                low = _lower(variant)
                pieces.update(low[i:i + SECRET_RUN] for i in range(len(low) - SECRET_RUN + 1))
    return pieces


def _runs(text: str, pieces: set[str]) -> list[list[int]]:
    low, runs = _lower(text), []
    for i in range(len(low) - SECRET_RUN + 1):
        if low[i:i + SECRET_RUN] in pieces:
            if runs and i <= runs[-1][1]:
                runs[-1][1] = i + SECRET_RUN
            else:
                runs.append([i, i + SECRET_RUN])
    return runs


def diagnostic_text(value: str, sensitive: tuple[str, ...] = (), *, limit: int = 300) -> str:
    pieces = _pieces(sensitive)
    text = _plain(value)[:16384]
    # Redact before truncating: a credential may straddle the retained boundary.
    for start, end in reversed(_runs(text, pieces)):
        text = text[:start] + "[redacted]" + text[end:]
    text = text[:limit].strip()
    return "" if _runs(text, pieces) else text


def _token_error_details(payload: object, form: dict[str, str]) -> dict[str, str]:
    if not isinstance(payload, dict):
        return {}
    sensitive = [
        value for source in (form, payload) for key, value in source.items()
        if key in ("code", "code_verifier", "client_secret", "access_token", "id_token", "refresh_token")
        and isinstance(value, str) and value
    ]
    codes = {}
    error = payload.get("error")
    if isinstance(error, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", error):
        codes["oauth_error"] = error
    description = payload.get("error_description")
    if isinstance(description, str):
        match = re.search(r"(?<!\w)AADSTS[0-9]{1,10}(?!\w)", description, flags=re.ASCII)
        if match:
            codes["aadsts_code"] = match[0]
    # Even an identifier-shaped value is not safe if the provider reflected a credential into it.
    pieces = _pieces(sensitive)
    details = {key: value for key, value in codes.items()
               if not _runs(value, pieces) and not any(_lower(value) in _lower(secret)
                                                       for secret in sensitive if len(secret) >= SECRET_RUN)}
    if code := details.get("aadsts_code") or details.get("oauth_error"):
        details["error_code"] = code
    if isinstance(description, str) and (text := diagnostic_text(description, tuple(sensitive))):
        details["error_text"] = text
    return details


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    if not isinstance(text, str) or not B64URL.match(text) or len(text) % 4 == 1:
        raise OIDCError("malformed_token")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def pkce_challenge(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode()).digest())


def check_issuer(issuer: str) -> str:
    """An issuer an owner typed: an https URL with a host, no query, fragment or credentials. Compared exactly later."""
    issuer = (issuer or "").strip()
    try:
        parts = urlsplit(issuer)
        valid = (parts.scheme == "https" and parts.hostname and parts.port != 0
                 and not parts.query and not parts.fragment and parts.username is None and parts.password is None
                 and len(issuer) <= 300 and not any(c.isspace() for c in issuer))
    except ValueError:
        valid = False
    if not valid:
        raise OIDCError("bad_issuer", "Enter the issuer as an https address, without a query, fragment or user name.")
    return issuer


def _https(url) -> str:
    try:
        parts = urlsplit(url) if isinstance(url, str) else None
        valid = (parts is not None and parts.scheme == "https" and parts.hostname and parts.port != 0
                 and not parts.fragment and parts.username is None and parts.password is None)
    except ValueError:
        valid = False
    if not valid:
        raise OIDCError("bad_discovery", "the provider named an endpoint that is not https")
    return url


def system_resolve(host: str, port: int) -> list[str]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = sorted({str(ipaddress.ip_address(answer[4][0])) for answer in answers})
    except (OSError, ValueError, UnicodeError):
        raise OIDCError("provider_unavailable", "Rynhult couldn't look up the provider's address. Check the issuer address.") from None
    if not addresses:
        raise OIDCError("provider_unavailable", "Rynhult couldn't look up the provider's address. Check the issuer address.")
    return addresses


def private_entry_refusal(addresses: list[str]) -> str | None:
    """Why these addresses can't be owner-allowed, or None: only ordinary LAN/ULA ranges, never loopback,
    link-local, metadata or other special-use addresses (issue_sources.allowed_address with private=True)."""
    if any(not allowed_address(a, True) for a in addresses):
        return "Only addresses on your local network (10/8, 172.16/12, 192.168/16, fc00::/7) can be allowed. Loopback, " \
               "link-local, metadata and other special addresses can't."
    return None


class PinnedTransport(httpx.BaseTransport):
    """Checks each provider request's address, then connects to that checked address (no second DNS lookup).
    `inner` is an in-memory provider for tests; `network` the real transport (httpx's, unless a test gives one)."""

    def __init__(self, resolve: Callable[[str, int], list[str]], allowed: tuple = (),
                 inner: httpx.BaseTransport | None = None, network: httpx.BaseTransport | None = None):
        self.resolve, self.allowed, self.inner = resolve, allowed, inner
        self.network = network if network is not None or inner is not None else httpx.HTTPTransport(retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host, port = request.url.host, request.url.port or 443
        if request.url.scheme != "https":
            raise OIDCError("bad_discovery", "the provider named an endpoint that is not https")
        addresses = self.resolve(host, port)
        entry = next((e for e in self.allowed if (e["host"], e["port"]) == (host, port)), None)
        if entry is None:
            if any(not allowed_address(a) for a in addresses):
                raise OIDCError("private_address", f"{host} has a private, loopback, link-local or metadata address. "
                                                   "An owner can allow a provider on your network under Provider addresses on your network.")
        elif addresses != sorted(entry["addresses"]) or private_entry_refusal(addresses):
            raise OIDCError("address_changed", f"{host} no longer has the addresses an owner allowed. Check and confirm them again under Provider addresses on your network.")
        if self.inner is not None:
            return self.inner.handle_request(request)
        pinned = httpx.Request(request.method, request.url.copy_with(host=addresses[0]), headers=request.headers,
                               stream=request.stream, extensions={**request.extensions, "sni_hostname": host})
        return self.network.handle_request(pinned)

    def close(self) -> None:
        if self.network is not None:
            self.network.close()


@dataclass(frozen=True)
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


def jwk_to_key(jwk: dict) -> rsa.RSAPublicKey | None:
    """An RSA signing key of at least 2048 bits, else None (other keys in the set are ignored, never trusted)."""
    if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or jwk.get("use", "sig") != "sig":
        return None
    if jwk.get("alg", "RS256") not in ALGORITHMS:
        return None
    try:
        n = int.from_bytes(b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(b64url_decode(jwk["e"]), "big")
        key = rsa.RSAPublicNumbers(e, n).public_key()
    except (KeyError, OIDCError, ValueError, TypeError):
        return None
    return key if key.key_size >= 2048 else None


@dataclass
class Provider:
    """Cached discovery and signing keys for one (issuer, client) configuration."""
    issuer: str
    client_id: str
    transport: httpx.BaseTransport | None = None
    clock: object = time.time
    # Address policy: always on the real network; a test's in-memory transport gets it only with a resolver.
    resolve: Callable[[str, int], list[str]] | None = None
    allowed: tuple = ()  # owner-allowed LAN hosts: ({"host", "port", "addresses"}, ...)
    _discovery: Discovery | None = None
    _discovery_at: float = 0.0
    _keys: dict = field(default_factory=dict)
    _keys_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        if self.resolve is None and self.transport is None:
            self.resolve = system_resolve

    def _client(self) -> httpx.Client:
        transport = PinnedTransport(self.resolve, self.allowed, inner=self.transport) if self.resolve else self.transport
        return httpx.Client(transport=transport, timeout=TIMEOUT, follow_redirects=False,
                            headers={"Accept": "application/json"})

    def _get_json(self, url: str) -> dict:
        try:
            with self._client() as client, client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise OIDCError("provider_unavailable", f"HTTP {response.status_code} from the provider")
                body = b""
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE:
                        raise OIDCError("provider_unavailable", "the provider's answer is too large")
        except httpx.HTTPError as error:
            raise OIDCError("provider_unavailable", type(error).__name__) from None
        try:
            data = json.loads(body)
        except ValueError:
            raise OIDCError("provider_unavailable", "the provider's answer is not JSON") from None
        if not isinstance(data, dict):
            raise OIDCError("provider_unavailable", "the provider's answer is not a JSON object")
        return data

    def discovery(self, refresh: bool = False) -> Discovery:
        with self._lock:
            if not refresh and self._discovery and self.clock() - self._discovery_at < CACHE_SECONDS:
                return self._discovery
        data = self._get_json(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
        if data.get("issuer") != self.issuer:
            raise OIDCError("bad_discovery", "the provider's discovery names a different issuer")
        for name, needed in (("response_types_supported", "code"), ("code_challenge_methods_supported", "S256"),
                             ("id_token_signing_alg_values_supported", "RS256")):
            if name in data and needed not in (data[name] or ()):
                raise OIDCError("bad_discovery", f"the provider does not offer {needed} ({name})")
        found = Discovery(self.issuer, _https(data.get("authorization_endpoint")), _https(data.get("token_endpoint")),
                          _https(data.get("jwks_uri")))
        with self._lock:
            self._discovery, self._discovery_at = found, self.clock()
        return found

    def refresh_keys(self) -> dict:
        data = self._get_json(self.discovery().jwks_uri)
        keys = {}
        for jwk in data.get("keys") or ():
            key = jwk_to_key(jwk)
            if key is not None:
                keys[jwk.get("kid") if isinstance(jwk.get("kid"), str) else None] = key
        if not keys:
            raise OIDCError("bad_keys", "the provider's key set has no usable RS256 signing key")
        with self._lock:
            self._keys, self._keys_at = keys, self.clock()
        return keys

    def key(self, kid: str | None) -> rsa.RSAPublicKey:
        with self._lock:
            keys, age = dict(self._keys), self.clock() - self._keys_at
        if not keys or age >= CACHE_SECONDS or (kid not in keys and age >= MIN_REFRESH):
            keys = self.refresh_keys()  # rotation: a new kid is fetched, a removed one stops working
        if kid is None and len(keys) == 1:
            return next(iter(keys.values()))
        if kid not in keys:
            raise OIDCError("unknown_key", "the token was signed with a key the provider does not publish")
        return keys[kid]

    # ------------------------------------------------------------ the flow

    def authorization_url(self, *, redirect_uri: str, state: str, nonce: str, verifier: str,
                          max_age: int | None = None) -> str:
        params = {"response_type": "code", "client_id": self.client_id, "redirect_uri": redirect_uri,
                  "scope": "openid profile email", "state": state, "nonce": nonce,
                  "code_challenge": pkce_challenge(verifier), "code_challenge_method": "S256",
                  "response_mode": "query"}
        if max_age is not None:
            params["max_age"] = str(max_age)
        query = urlencode(params)
        endpoint = self.discovery().authorization_endpoint
        return f"{endpoint}{'&' if urlsplit(endpoint).query else '?'}{query}"

    def exchange(self, *, code: str, redirect_uri: str, verifier: str, client_secret: str | None) -> dict:
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "code_verifier": verifier, "client_id": self.client_id}
        if client_secret:
            form["client_secret"] = client_secret
        try:
            with self._client() as client, client.stream("POST", self.discovery().token_endpoint, data=form) as response:
                status = response.status_code
                # A refusal's body is read too (under the same bound) for the provider's error code.
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE:
                        raise OIDCError("token_refused", "the token endpoint's answer is too large"
                                        if status == 200 else f"HTTP {status} from the token endpoint")
        except httpx.HTTPError as error:
            raise OIDCError("provider_unavailable", type(error).__name__) from None
        http_detail = f"HTTP {status} from the token endpoint"
        try:
            tokens = json.loads(body)
        except (ValueError, RecursionError):
            detail = http_detail if status != 200 else "the token endpoint's answer is not JSON"
            raise OIDCError("token_refused", detail) from None
        if status != 200:
            raise OIDCError("token_refused", http_detail, provider_detail=_token_error_details(tokens, form))
        if not isinstance(tokens, dict) or not isinstance(tokens.get("id_token"), str):
            raise OIDCError("token_refused", "no ID token", provider_detail=_token_error_details(tokens, form))
        return tokens

    def verify(self, id_token: str, *, nonce: str, access_token: str | None = None) -> dict:
        """The verified claims of an ID token, or OIDCError."""
        parts = id_token.split(".") if isinstance(id_token, str) and len(id_token) < 16384 else []
        if len(parts) != 3:
            raise OIDCError("malformed_token")
        try:
            header = json.loads(b64url_decode(parts[0]))
            claims = json.loads(b64url_decode(parts[1]))
            # Escaped unpaired surrogates are accepted by Python's JSON decoder,
            # but are not usable UTF-8 identities, nonces or audit fields.
            json.dumps([header, claims], ensure_ascii=False).encode("utf-8")
        except (ValueError, UnicodeError):
            raise OIDCError("malformed_token") from None
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise OIDCError("malformed_token")
        if header.get("alg") not in ALGORITHMS:  # alg=none and HS256-with-the-public-key end here
            raise OIDCError("bad_algorithm", "only RS256 ID tokens are accepted")
        if "crit" in header:
            raise OIDCError("bad_algorithm", "critical header extensions are not supported")
        kid = header.get("kid")
        if kid is not None and not isinstance(kid, str):
            raise OIDCError("malformed_token")
        try:
            self.key(kid).verify(b64url_decode(parts[2]), f"{parts[0]}.{parts[1]}".encode("ascii"),
                                 padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            raise OIDCError("bad_signature") from None
        self.check_claims(claims, nonce=nonce, access_token=access_token)
        return claims

    def check_claims(self, claims: dict, *, nonce: str, access_token: str | None) -> None:
        now = self.clock()
        if claims.get("iss") != self.issuer:
            raise OIDCError("wrong_issuer")
        aud = claims.get("aud")
        audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        if self.client_id not in audiences or not all(isinstance(a, str) for a in audiences):
            raise OIDCError("wrong_audience")
        azp = claims.get("azp")
        if (len(audiences) > 1 or azp is not None) and azp != self.client_id:
            raise OIDCError("wrong_audience", "authorized party is not this client")
        exp, iat, nbf = claims.get("exp"), claims.get("iat"), claims.get("nbf")
        if not _number(exp) or not _number(iat) or (nbf is not None and not _number(nbf)):
            raise OIDCError("malformed_token", "exp and iat must be numbers")
        if exp <= now - CLOCK_SKEW:
            raise OIDCError("expired")
        if iat > now + CLOCK_SKEW or (nbf is not None and nbf > now + CLOCK_SKEW):
            raise OIDCError("not_yet_valid")
        if iat < now - MAX_TOKEN_AGE - CLOCK_SKEW:
            raise OIDCError("expired", "issued too long ago")
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce.encode(), nonce.encode()):
            raise OIDCError("wrong_nonce")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not 0 < len(sub) <= 255 or any(ord(c) < 32 for c in sub):
            raise OIDCError("malformed_token", "no usable subject")
        at_hash = claims.get("at_hash")
        if at_hash is not None:
            if not isinstance(access_token, str) or not isinstance(at_hash, str):
                raise OIDCError("wrong_at_hash")
            digest = hashlib.sha256(access_token.encode("ascii", "replace")).digest()
            if not hmac.compare_digest(b64url(digest[:16]).encode(), at_hash.encode()):
                raise OIDCError("wrong_at_hash")


# acr values that assert more than one factor: OpenID PAPE multi-factor, and Okta's phishing-resistant levels.
# amr values (RFC 8176) that prove more than one factor by themselves: "mfa" (Entra ID sends it when Conditional Access
# required MFA), "hwk" (a hardware-bound key) and "fido" (a FIDO authenticator). acr values mean whatever each provider
# and tenant decides, so none count unless an owner names them (OIDCProvider.mfa_acr, decision 0061).
MFA_AMR = frozenset({"mfa", "hwk", "fido"})
MAX_ACR_VALUES, MAX_ACR_LENGTH = 10, 200
# acr values that never prove MFA: "0" is "no assurance" (OIDC Core 2), and Entra ID's v1 tokens send "1" after any
# password sign-in.
NOT_MFA_ACR = frozenset({"0", "1"})


def acr_refusal(values: list[str]) -> str | None:
    """Why an owner's list of tenant acr values can't be saved, or None."""
    if len(values) > MAX_ACR_VALUES:
        return f"Enter at most {MAX_ACR_VALUES} acr values."
    for value in values:
        if not value or len(value) > MAX_ACR_LENGTH or not value.isprintable() or any(c.isspace() for c in value):
            return "Each acr value is a single word of printable characters, at most 200 long."
        if value in NOT_MFA_ACR:
            return f'The acr value "{value}" never proves multi-factor sign-in. Remove it.'
    return None


def mfa_proven(claims: dict, acr_values=()) -> bool:
    """The verified ID token itself says the person used several factors: amr names one of MFA_AMR, or acr is one of
    the values an owner configured for this tenant. Absent or malformed claims prove nothing."""
    amr = claims.get("amr")
    if isinstance(amr, list) and any(isinstance(v, str) and v in MFA_AMR for v in amr):
        return True
    acr = claims.get("acr")
    return isinstance(acr, str) and acr not in NOT_MFA_ACR and acr in set(acr_values)


def enrollment_authentication_recent(claims: dict, now: float) -> bool:
    authenticated = claims.get("auth_time")
    return _number(authenticated) and now - ENROLLMENT_MAX_AUTH_AGE <= authenticated <= now


# Entra ID (and its national clouds): there preferred_username is the UPN an administrator sets. Elsewhere it is any
# string the provider (sometimes the person) chooses, which OIDC Core says a relying party must not rely on.
ENTRA_HOSTS = {"login.microsoftonline.com", "login.microsoftonline.us", "login.chinacloudapi.cn",
               "login.partner.microsoftonline.cn", "sts.windows.net"}
ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def fold(name: str) -> str:
    """ASCII case only: str.lower() maps look-alikes such as KELVIN SIGN onto ASCII letters."""
    return name.strip().translate(ASCII_LOWER)


def directory_names(claims: dict) -> set[str]:
    """Directory account names in a verified ID token, ASCII lower case: email only when email_verified is true, upn,
    and Entra's preferred_username. These only check an invitation; identity stays (issuer, subject)."""
    names = set()
    if claims.get("email_verified") is True and isinstance(claims.get("email"), str):
        names.add(claims["email"])
    keys = ("upn", "preferred_username") if urlsplit(str(claims.get("iss", ""))).hostname in ENTRA_HOSTS else ("upn",)
    for key in keys:
        if isinstance(claims.get(key), str):
            names.add(claims[key])
    return {fold(n) for n in names if 0 < len(n.strip()) <= 320}


def _number(value) -> bool:
    return not isinstance(value, bool) and (isinstance(value, int) or isinstance(value, float) and math.isfinite(value))


class Providers:
    """accd's provider cache: one Provider per (issuer, client id, revision); tests inject a transport and clock."""

    def __init__(self, transport: httpx.BaseTransport | None = None, clock=time.time,
                 resolve: Callable[[str, int], list[str]] | None = None):
        self.transport, self.clock, self.resolve = transport, clock, resolve
        self._cache: dict[tuple, Provider] = {}
        self._lock = threading.Lock()

    def resolver(self) -> Callable[[str, int], list[str]]:
        return self.resolve or system_resolve

    def get(self, issuer: str, client_id: str, revision: int, allowed: tuple = ()) -> Provider:
        key = (issuer, client_id, revision)
        with self._lock:
            if key not in self._cache:
                self._cache = {key: Provider(issuer, client_id, transport=self.transport, clock=self.clock,
                                             resolve=self.resolve)}
            provider = self._cache[key]
            provider.allowed = tuple(allowed)  # the owner's current list, read with each sign-in
            return provider

    def probe(self, issuer: str, client_id: str, allowed: tuple = ()) -> Provider:
        """A fresh provider (no cache) for "Test connection"."""
        return Provider(issuer, client_id, transport=self.transport, clock=self.clock, resolve=self.resolve,
                        allowed=tuple(allowed))
