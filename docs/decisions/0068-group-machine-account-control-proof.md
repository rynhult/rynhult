# 0068: Prove local account control before group-machine approval

Status: accepted by the owner, 2026-10-25 (groups phase 4 review M2).

## Context

An enrolled machine and a Unix account name do not establish which person controls
that account. Owner approval based only on a name could reserve another person's
account and run group work there. Same-host groups remain independent of this path.

## Decision

A member creates a pending request, then generates a short confirmation code
locally on that machine, as the requested ordinary account. Only the local command
returns the code. The account's broker verifies the member's submission over the
existing authenticated node link; no remote generation or code-read RPC exists.
Codes bind the immutable node ID, account and request ID; they expire after five
minutes and work once. Persistent local attempt and issuance limits, plus a
persistent per-person API limit, constrain guessing and generation. Local receipts
and server audit events omit the code and digest. The owner reviews the explicit
verification state before approval. A proof establishes control, not identity,
machine integrity, provider consent or project authority.

## Consequences

The member needs terminal access as that account and the configured broker run
directory. No new sudo, socket access, node key access or daemon is required.
Restarting a broker does not reset proof state. Failed or interrupted verification
can consume a code without granting authority; generate a new code after a minute.
Cancellation/revocation cannot be undone by a delayed successful broker response.
The own-machine feature remains off by default; native qualification still applies.

## Alternatives

Relying on owner knowledge or historical account usage leaves the original identity
ambiguity. Returning a code to the requesting browser or generating it remotely
would prove access to Rynhult, not local access to the machine account.
