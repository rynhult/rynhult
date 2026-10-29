"""Check the recorded Android fixtures without a JVM: complete, public-only, and their leaves bind the test keys.

    python -I mobile/android/tools/check_fixtures.py      (needs the `cryptography` package, as the server does)
"""
import base64
import hashlib
import json
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

FIXTURE = Path(__file__).resolve().parents[1] / "enrollment-core/src/test/resources/fixtures/lifecycle.json"
EXPECTED = {
    "start": 200, "claim": 200, "credential_waiting": 409, "confirm": 200, "credential": 200, "claim_used": 410,
    "credential_awaiting": 200, "before_bind": 401, "auth_state": 200, "bind": 200, "devices_public": 200, "devices_private": 200,
    "rotate": 200, "rotate_same_key": 409, "rotate_claim": 200, "rotate_confirm": 200, "rotate_credential": 200,
    "old_receipt_after_rotation": 409, "revoke": 200, "credential_after_revoke": 410, "session_after_revoke": 401,
    "devices_after_revoke": 200, "edge_after_revoke": 403, "throttled": 429,
}


def main() -> int:
    fixture = json.loads(FIXTURE.read_text())
    text = FIXTURE.read_text()
    assert "PRIVATE KEY" not in text, "fixtures hold public material only"
    assert {name: e["status"] for name, e in fixture["exchanges"].items()} == EXPECTED, "exchange set or status drift"
    exchanges = fixture["exchanges"]
    for index, name in enumerate(("credential", "rotate_credential")):
        label = fixture["test_keys"][index]["label"]
        key = ec.derive_private_key(int.from_bytes(hashlib.sha256(label.encode()).digest(), "big"), ec.SECP256R1())
        spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        assert base64.b64encode(spki).decode() == fixture["test_keys"][index]["public_key_spki_b64"], label
        body = exchanges[name]["body"]
        leaf = x509.load_pem_x509_certificate(body["certificate"].encode())
        leaf.verify_directly_issued_by(x509.load_pem_x509_certificate(body["chain"].encode()))
        assert leaf.public_key().public_numbers() == key.public_key().public_numbers(), name
    public = exchanges["devices_public"]["body"]["devices"][0]
    assert "installation_id" not in public and "installation_id" in exchanges["devices_private"]["body"]["devices"][0]
    assert exchanges["devices_after_revoke"]["body"]["devices"][0]["status"] == "revoked"
    print(f"{len(EXPECTED)} recorded exchanges from {fixture['recorded_from'][:12]} are complete and consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
