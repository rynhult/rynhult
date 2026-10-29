# Google Cloud DNS certificate credential files follow root generations

Date: 2026-10-24. Status: implemented candidate, not live-qualified.

## Context

Decision 0010's portable DNS boundary uses fixed schemas and root credential generations. The pinned Google
Caddy module accepts a service-account JSON **file path**, not inline JSON, and lists managed zones across the
project. Treating JSON as a path or enabling ambient Application Default Credentials would break that boundary.

## Decision

Keep explicit service-account JSON in the purpose-bound infrastructure vault and root-only environment generations.
The standalone renderer derives a SHA-256 filename solely from the validated JSON under the fixed
`/etc/caddy/acc-dns-google` directory. Root provisions this directory as root:caddy 2750 and writes root:caddy 0640
files before validating Caddy. Directory setgid supplies the group without expanding helper capabilities. Caddy
can read files but cannot replace them. Descriptor checks refuse symlinks, hardlinks, wrong modes and owners.
Retain only files referenced by current, previous and candidate environment generations, following existing
validation failure, rollback, recovery and expiry purge. Never accept a credential path over IPC.

Publishing uses DNS Administrator on one explicit managed zone; the certificate candidate needs DNS Administrator
in a dedicated project because its discovery call lists managed zones. Both consumers remain NOT qualified.
The owner-run harness supplies a temporary owner-only file instead of production root paths.

## Consequences

Service-account plaintext exists in Caddy-readable root-managed files as well as root-only environment generations.
Root/accd/Caddy are already trusted consumers. Rotation and rollback retain the right file; retired files are
removed with their generations. Live permissions, restart, dependency compatibility and custody require qualification
and independent security review. No changes to a live host or DNS are authorized by this decision.

## Alternatives considered

Ambient machine credentials hide credential selection and broaden authority. Arbitrary owner-selected paths expand
privileged IPC. An inline JSON configuration is unsupported by the pinned module. A patched module might remove
the file requirement later but introduces a separate supply-chain review and is outside this candidate.
