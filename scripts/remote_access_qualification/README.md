# Remote-access qualification lab

Run from this checkout, with the existing project Python environment:

```sh
PYTHONPATH=src /srv/acc/app/.venv/bin/python -m pytest -q tests/test_remote_access_qualification.py
```

This is a qualification fixture, not a deployment tool. It uses the real
`/usr/bin/caddy`, Python's OpenSSL TLS client, and a minimal HTTP/2 wire client.
It requires the project's existing `cryptography` and `pytest`; it installs nothing.
Missing Caddy is an error, never a skipped security qualification.

Each test owns one Caddy subprocess and a counting HTTP upstream. The kernel
chooses high ports on `127.0.0.1`. A private `/tmp/acc-ra-*` directory holds a
separate Unix admin socket, JSON configuration, server CA, device CA, wrong CA,
certificates, keys, Caddy storage, home/config/data directories and logs. Keys
are mode 0600 inside the mode-0700 temporary directory. Teardown terminates and
waits for Caddy, closes the stub, and deletes the directory even on failure.
No real DNS lookup, system trust installation, live Caddy admin request,
`/etc/caddy` access, ACC upstream, port 80/443 listener or product route is used.
A race claiming a free TCP port fails startup rather than attaching to another
service. Only the tests start these listeners.

`Lab.config()` is the exact tested JSON policy. The server certificate covers
both `remote.example.com` and `acc.example.lan`, deliberately allowing the
conditions for HTTP/2 connection coalescing. One TLS connection sends an allowed
request, a cross-authority request, then another allowed request; only the two
matching requests may increment the upstream count. Host/SNI confusion is also
tested over HTTP/1.1 in both directions. There is no HTTP fingerprint gate.

The expired and wrong-CA leaves are intentionally members of the TLS leaf list:
rejection must therefore come from certificate verification. The revoked leaf
has a valid chain but is not in the list. Empty membership keeps the leaf
verifier present. TLS 1.2 and TLS 1.3 both have invalid-certificate and revocation
probes. Reload timing is bounded at two seconds on this localhost lab, with
five immediate fresh-handshake rejection attempts after successful apply.

Session tests verify that the candidate configuration issues no tickets and
rejects a reconnect offering the prior SSLSession. A second test deliberately
enables tickets in this disposable lab, obtains an actual ticket, proves it
resumes, then revokes and reloads with tickets disabled and tries the real old
ticket. These controls prevent counting an unexercised resumption path as proof.

See `docs/REMOTE-ACCESS.md`, “Phase 1 results”, for the installed version,
version-specific JSON fields, boundaries of these results and owner checklist.
A passing lab is not permission to expose ACC or claim phone/PWA compatibility.

Phase 2's `tests/test_devices.py::test_rendered_registry_revocation_rejected_by_caddy`
uses the uninstalled helper to issue a real encrypted identity, registers its public
leaf, and passes the complete product gate fragment through `Lab.reload_fragment`.
The lab keeps its temporary listener, certificate, private recovery route and admin
socket; the public gate comes directly from ACC's renderer. A listed helper-issued
identity reaches the counting stub, then registry revocation renders an empty leaf
verifier and the same identity is refused with no added upstream requests.
