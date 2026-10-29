"""Native identity validation. No private device keys or native bearer login protocol."""
import base64

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from . import auth, devices


def public_key(csr_pem):
    if len(csr_pem.encode()) > 2048:
        raise ValueError('CSR too large')
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    key = csr.public_key()
    if not csr.is_signature_valid or not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != 'secp256r1':
        raise ValueError('A signed P-256 CSR is required')
    return key


def key_hash(key):
    return auth.digest(key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo).hex())


def proof(row, body, origin):
    key = public_key(body.csr)
    message = '\n'.join(['acc-mobile-claim-v1', row.id, body.token, origin,
                         body.installation_id, body.platform, auth.digest(body.csr)]).encode()
    signature = base64.b64decode(body.proof + '=' * (-len(body.proof) % 4), altchars=b'-_', validate=True)
    key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    return key_hash(key)


def identity(result, device_id, csr_pem, days, ca_pem):
    cert = devices.identity(result, device_id, days)
    ca = x509.load_pem_x509_certificate(ca_pem.encode())
    cert.verify_directly_issued_by(ca)
    if (cert.not_valid_before_utc > auth.now()
            or cert.subject != x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id),
                                         x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, 'acc-native')])
            or key_hash(cert.public_key()) != key_hash(public_key(csr_pem))
            or set(e.oid for e in cert.extensions) != {x509.ExtensionOID.BASIC_CONSTRAINTS,
                x509.ExtensionOID.KEY_USAGE, x509.ExtensionOID.EXTENDED_KEY_USAGE}
            or cert.extensions.get_extension_for_class(x509.KeyUsage).value !=
                x509.KeyUsage(True, False, False, False, False, False, False, False, False)
            or ca.not_valid_before_utc > auth.now() or ca.not_valid_after_utc <= cert.not_valid_after_utc):
        raise ValueError('Invalid native signer output')
    return cert
