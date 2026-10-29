"""A local OpenID provider for tests only (never shipped): an httpx transport serving discovery, JWKS and a token
endpoint, signing ID tokens with throwaway RSA keys. Knobs let a test rotate keys or send hostile tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64json(value) -> str:
    return b64(json.dumps(value, separators=(",", ":")).encode())


class TestIdP:
    __test__ = False

    def __init__(self, issuer: str = "https://idp.example.com/tenant-a", client_id: str = "acc-client",
                 client_secret: str = "test-client-secret", clock=time.time):
        self.issuer, self.client_id, self.client_secret, self.clock = issuer, client_id, client_secret, clock
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.signing_kid = self.rotate()
        self.codes: dict[str, dict] = {}  # code -> {sub, nonce, redirect_uri, challenge, client_id}
        self.down = False
        self.requests: list[httpx.Request] = []
        self.token_overrides: dict = {}  # claims replaced in the next ID tokens
        self.header_overrides: dict = {}
        self.forge = None  # callable(claims) -> id_token, replacing the signed one
        self.discovery_overrides: dict = {}
        self.transport = httpx.MockTransport(self.handle)

    # ---- keys

    def rotate(self, drop_old: bool = False) -> str:
        kid = f"k{len(self.keys) + 1}"
        if drop_old:
            self.keys.clear()
        self.keys[kid] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.signing_kid = kid
        return kid

    def public_pem(self, kid: str | None = None) -> bytes:
        return self.keys[kid or self.signing_kid].public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

    def jwks(self) -> dict:
        out = []
        for kid, key in self.keys.items():
            numbers = key.public_key().public_numbers()
            out.append({"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
                        "n": b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                        "e": b64(numbers.e.to_bytes(3, "big"))})
        return {"keys": out}

    # ---- tokens

    def claims(self, sub: str, nonce: str, access_token: str | None = None, **extra) -> dict:
        now = int(self.clock())
        claims = {"iss": self.issuer, "sub": sub, "aud": self.client_id, "exp": now + 300, "iat": now, "nonce": nonce}
        if access_token:
            claims["at_hash"] = b64(hashlib.sha256(access_token.encode()).digest()[:16])
        return claims | extra

    def sign(self, claims: dict, kid: str | None = None, header: dict | None = None) -> str:
        kid = kid or self.signing_kid
        head = {"alg": "RS256", "typ": "JWT", "kid": kid} | (header or {})
        signing_input = f"{b64json(head)}.{b64json(claims)}".encode()
        signature = self.keys[kid].sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{signing_input.decode()}.{b64(signature)}"

    def unsigned(self, claims: dict) -> str:
        return f"{b64json({'alg': 'none', 'typ': 'JWT'})}.{b64json(claims)}."

    def hs256_with_public_key(self, claims: dict) -> str:
        """The classic confusion attack: HMAC keyed with the provider's public key, which anyone can fetch."""
        head = {"alg": "HS256", "typ": "JWT", "kid": self.signing_kid}
        signing_input = f"{b64json(head)}.{b64json(claims)}".encode()
        return f"{signing_input.decode()}.{b64(hmac.new(self.public_pem(), signing_input, hashlib.sha256).digest())}"

    def issue_code(self, sub: str, authorize_query: dict, nonce: str | None = None) -> str:
        code = f"code-{len(self.codes)}-{sub}"
        self.codes[code] = {"sub": sub, "nonce": nonce or authorize_query["nonce"][0],
                            "redirect_uri": authorize_query["redirect_uri"][0],
                            "challenge": authorize_query["code_challenge"][0], "client_id": authorize_query["client_id"][0]}
        return code

    # ---- HTTP

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("identity provider is down", request=request)
        path = request.url.path
        base = httpx.URL(self.issuer).path.rstrip("/")
        if request.method == "GET" and path == base + "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.issuer, "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token", "jwks_uri": f"{self.issuer}/keys",
                "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
                "id_token_signing_alg_values_supported": ["RS256"]} | self.discovery_overrides)
        if request.method == "GET" and path == base + "/keys":
            return httpx.Response(200, json=self.jwks())
        if request.method == "POST" and path == base + "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            grant = self.codes.pop(form.get("code", ""), None)  # one use
            if grant is None or form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if form.get("redirect_uri") != grant["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if b64(hashlib.sha256(form.get("code_verifier", "").encode()).digest()) != grant["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            access = f"at-{grant['sub']}"
            claims = self.claims(grant["sub"], grant["nonce"], access) | self.token_overrides
            token = self.forge(claims) if self.forge else self.sign(claims, header=self.header_overrides)
            return httpx.Response(200, json={"token_type": "Bearer", "access_token": access, "id_token": token})
        return httpx.Response(404)
