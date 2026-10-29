#!/usr/bin/python3 -I
"""single-cert.py: exactly one PEM certificate on stdin, written back re-encoded on stdout (standard library only).

deploy/install.sh copies Caddy's root CA into Rynhult's data, where GET /api/nodes/ca serves it to anyone. The file
lives in Caddy's writable home, so it is read as caddy and passed through here: anything but one certificate (another
file a link pointed at, a key, several blocks) is refused and nothing is copied (install and update review, IU-2).
"""
import base64
import re
import ssl
import sys

LIMIT = 65536


def certificate(text: str) -> str:
    blocks = re.findall(r"-----BEGIN ([A-Z0-9 ]+)-----", text)
    if blocks != ["CERTIFICATE"]:
        raise ValueError("expected exactly one PEM certificate")
    der = ssl.PEM_cert_to_DER_cert(text[text.index("-----BEGIN CERTIFICATE-----"):])
    if len(der) < 64 or der[0] != 0x30:
        raise ValueError("not a DER certificate")
    base64.b64decode(base64.b64encode(der), validate=True)
    return ssl.DER_cert_to_PEM_cert(der)


def main() -> int:
    raw = sys.stdin.buffer.read(LIMIT + 1)
    try:
        if len(raw) > LIMIT:
            raise ValueError("too large")
        sys.stdout.write(certificate(raw.decode("ascii")))
    except (ValueError, UnicodeDecodeError):
        print("single-cert: not exactly one PEM certificate; nothing copied", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
