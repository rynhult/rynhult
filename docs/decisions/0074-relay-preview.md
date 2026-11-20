# Session relay owner preview (Claude only)

Date: 2026-11-13. Status: accepted by the owner.

## Context

The Claude relay path (shared Chat and Terminal over one exclusive writer) has
passed its automated and native qualification runs. The owner wants to test it
by hand before any wider release. Codex relay take-over has not finished its
native retirement and Terminal transport qualification.

## Decision

1. Enable the session relay preview for the owner's manual test of the proven
   Claude path: `CLAUDE_LAUNCH_QUALIFIED`, `PUBLIC_CLAUDE_QUALIFIED` and
   `PUBLIC_WRITER_QUALIFIED` are set to true.
2. Owners only during the preview. Relay launch refuses anyone else with
   "The session relay preview is limited to install owners", and the capability
   grant re-checks the owner role.
3. Claude only. Codex writer take-over, Codex Terminal input and Codex native
   decisions stay behind `CODEX_PUBLIC_WRITER_QUALIFIED = False`.

The grant (`session_relay/release_qualification.py`) writes Chat text, Terminal
input and the two switches for one pinned Claude CLI digest and Agent SDK
0.1.50. It writes them only for the current unsealed run, when the admin
feature is on, the broker-probed CLI digest matches the pin, the release scope
allows the session, the actor is an install owner and, for subscription auth,
the person's subscription consent is current. Any failed condition writes
nothing. Launch enrolment and a successful switch call it; recovery and fence
loss never re-grant.

## What stays off

- The **Session relay** admin feature stays default off. The owner switches it
  on in Settings → Admin features.
- Claude decisions, Claude interrupts, public L1 for other runs, recovery,
  delivery and Claude service delivery, reconciliation, stock approval keys and
  decoder, and Claude acknowledgement and durable-evidence flags all stay
  unqualified.
- Codex relay stays unqualified for writer take-over and Terminal input.
- Consent is still re-checked on every mutation; revoking subscription Chat
  refuses new work immediately.

## Consequences

Members cannot start relay sessions until a later decision widens the preview.
Changing the CLI pin needs a new qualification record and a new declaration.

## Codex round-two addendum (2026-11-16)

The owner requested the follow-up Codex preview. This supersedes the round-one
Codex writer/input exclusion above; Claude's declaration, consent policy and
qualification flags are unchanged. Implementation remains a review candidate
until the coordinator runs the full gate and focused security review before merge.

Codex uses a separate frozen declaration for CLI0.167.1, pinned by SHA256
`dd5bfdf19d794dcc6fb482d2d5a2d5a2d9fb31d11f6caa44b303bb72890d461e8a02`.
Its only qualified operations are `text` and `terminal.input`. The production
broker adapter uses that declaration; a matching pin alone does not grant a
session capabilities. The actual broker probe must report availability, Codex
provider and that digest before public launch records it in the new run.

The server grants only when Session relay is on, the run is current and unsealed,
its recorded broker-probed digest matches the declaration, `release_scope.local`
allows it, and the person is both a current install owner and the session owner.
Codex has no Claude subscription-consent condition. Any failed grant condition
writes no capabilities. Every person-driven Codex mutation rechecks current person ownership;
demotion refuses input even on an existing attachment/grant and withdraws its
capabilities. Read-only history remains governed by its existing access checks.

`CODEX_PUBLIC_WRITER_QUALIFIED` is true for that owner-granted path. Chat →
Terminal → Chat changes the writer on the same native session/execution owner;
it does not qualify Claude's L1 switch operations for Codex. The retired input
stays read-only with its unsent draft, the new writer starts with fresh input,
and take-over never types, queries the model or replays a prior prompt/draft.

The admin feature stays default off and nothing promotes automatically from a
successful fixture or stored capability. Stock approval input/keys/decoder,
Codex and Claude enrolled decisions, delivery/service delivery, recovery and
reconciliation remain off/unqualified. Codex enrolled decisions now have a
separate default-off gate so opening writer take-over cannot open approval replies,
even with a fabricated decision capability. Unsupported paths stay refused.

Branch evidence is recorded in
`docs/handoffs/2026-11-16-relay-codex-preview-native-20.json`; the owner manual check
has a separate Codex section with no subscription step. Branch proofs do not
replace the coordinator's merged-source gate or security review, and no live
account is used by the automated proof.


Separately gated service delivery is not a person preview grant. A service actor
must pass the exact trusted DeliveryResolver, its current binding/scope and the
DELIVERY_QUALIFIED gate before mutation; it never receives a writer or preview
capabilities of its own. The person-owner preview guard does not replace or reject
that independently authorized service scope. Delivery and scheduled combinations
remain off/refused by their own release gates in production. A service-shaped
actor or copied scope cannot bypass those checks, even if a test enables delivery.
