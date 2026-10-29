# Device CA helper (not installed)

This first slice contains an uninstalled signer. Nothing invokes an installer,
changes trust, writes Caddy, forwards a port, or enables public access.

The future owner installation must copy this script into a root-owned, non-agent-
writable location. Both tools run with `/usr/bin/python3 -I`; the reviewed system
Python environment must contain cryptography without relying on `PYTHONPATH` or
user site-packages. Recovery checks that the fixed sibling signer and its parent
directories are root-owned and not group/other-writable before loading its source
(not a bytecode cache). Provision
`/var/lib/acc-device-ca` as root (0700) using `initialize(Path(...))` once; retain
`ca.key` as root-only 0600, and copy only `ca.pem` to ACC's data directory as
`device-ca.pem`. Never reuse the server CA or distribute the signing key.

Runtime contract: exactly one JSON object on stdin, at most 4096 bytes, followed
by EOF. No arguments. Exactly these fields:

```json
{"operation":"issue","device_id":"0123456789abcdef0123456789abcdef","days":90}
```

`days` is an integer from 1 to 90. Device IDs are 32 lowercase hex characters.

Machine identities use the only other operation, for a key the node holds:

```json
{"operation":"issue_machine","node_id":"0123456789abcdef","csr":"-----BEGIN CERTIFICATE REQUEST-----...","days":30}
```

`node_id` is 16–32 lowercase hex characters, `days` 1 to 30 and the CSR at most
2048 characters, a P-256 key with a valid self-signature. Only its public key is
used: subject `OU=acc-machine,CN=<node_id>`, URN SAN `urn:acc:machine:<node_id>`,
clientAuth only, non-CA. The response is `{"certificate": "PEM"}`; the ledger
records `machine_id`. No extension, key, name, file path, shell command or
provisioning operation is accepted. Output is one bounded JSON response containing the public leaf PEM,
an encrypted base64 PKCS#12 and a separate random import password. Neither the
private leaf key nor bundle is written to disk. Each issuance is appended to the
root-only ledger `issued.log` (issued_at, device_id, serial, not_after) under an
exclusive lock; more than 20 issuances in the trailing hour are refused. The
helper refuses to run if the state directory, `ca.key`, `ca.pem` or the ledger is
group/other accessible, owned by another user or a symlink, and `initialize()`
refuses an existing directory with loose permissions. ClientAuth is the only EKU on CA
and leaves; CA path length is zero. This initial authority is a single dedicated
online root, not an offline-root/intermediate hierarchy.

A later reviewed owner install must expose this as a socket-activated, one-request
service at `/run/acc-device-ca.sock`, with socket access limited to accd and root,
root-owned script and parent directories, finite request deadline and service
resource bounds. accd's client uses that fixed socket and no command execution.
The helper reads fixed root-owned state; callers cannot select a directory.
No service or socket unit is shipped or enabled in this slice.

`{"operation":"authority"}` returns `{"certificate": "PEM", "fingerprint": "hex", "previous": "hex" | null,
"previous_key": bool}`: the current authority's public certificate, plus the previous authority's fingerprint and
whether its key still exists. accd uses it to tell which identities predate a rollover and to audit the steps.
It reads no key.

Planned rollover (root-only, the same recovery tool; decision 0062): `rollover stage` creates `next/`,
`rollover promote` makes it current after `/etc/acc-remote-access` trusts both authorities and deletes the old
key, and `rollover finish` deletes `previous/` (the old certificate) once the edge no longer trusts it. Each step
is appended to the root-only `rollover.log`, and refuses before any change if that record isn't private. Each step waits for a signing in progress to finish (both lock the
authority directory), so no identity is signed half-way through a promote. A trust file in `/etc/acc-remote-access`
that is a link, or anything but a plain file, makes promote and finish refuse, as the edge helper refuses it.
See the runbook in the
internal wiki's Device pairing page.

Back up the authority with `acc-device-ca-recovery.py` (root only, installed in
the same root-owned directory, for example `/usr/local/lib/acc-device-ca/`):
`backup <new absolute file>` and `restore <file> <recorded CA fingerprint>`.
Its passphrase is the first line of `/etc/acc-device-ca/backup-passphrase`
(root, 0600, at least 16 characters, never in a backup set; keep an offline
copy). The root `acc-backup` writes `device-ca.enc` into each day's folder when
the tool and passphrase exist, and otherwise says the authority was not backed
up. Record the CA fingerprint offline when provisioning: restore requires it.
Before any deployment, qualify restore/rotation and the installation's permissions.
On suspected compromise keep remote access off, replace the authority, discard
all old leaf policy and re-pair via LAN. Do not restore a broad CA-only gate.

Tests import the provisioning/issuance primitives using temporary directories;
the executable boundary tests never provision the fixed production directory.

## Experimental native profile

The off-by-default mobile server slice uses the same fixed socket and owner CA,
with a closed `issue_native` operation: `operation`, `device_id` (32 lowercase
hex), `csr` (signed P-256, at most 2048 bytes) and `days` (integer 1–90). It issues
`OU=acc-native,CN=<device_id>`, clientAuth only, ignoring requested subject/SANs/
extensions. The result is only `certificate`; no device key, password or PKCS#12
is created. Native, browser and machine issuance share the persistent 20/hour
ledger. Installation and real-device qualification remain separate owner work.
See `docs/MOBILE-APPS.md` and `docs/wiki/Internal/Mobile-Devices.md`.
