# Disposable DNS qualification kit

## Context

The six existing provider adapters have a disposable-zone harness but require private credential input files,
produce no structured report and leave renewal to a manual follow-up. Provider availability must stay gated by
independent review. Azure's pinned certificate module has a known shared-TXT defect; wave 3 is not merged.

## Decision

Add an owner-run `acc.dns_qualify` command, with exact repeated disposable-zone acknowledgement, hidden interactive
credential prompts only, a fresh private state directory and an independently reviewed Caddy SHA256. Reuse the
server adapters, ownership ledger and harness probes. Constrain Python mutations to trial names in the exact zone;
require provider-side least privilege and dedicated projects/accounts for the separately executed trusted Caddy.
Do not add an infrastructure vault export or environment credential fallback.

Run Publishing lifecycle/refusal and staging certificate issuance followed by forced renewal from the same storage.
Require a changed serial and fingerprint, sentinel preservation after each cycle, verified cleanup and an empty
ledger. Preserve uncertain intent and stop probes at the first observed error; no speculative cleanup or resume.
Retain existing bounded adapter read throttling and operation polling. Private Caddy output is bounded and discarded;
error-level events fail the run. JSON reports contain fixed stages/errors and public run identity, never exception bodies.

Azure gets an explicit pre-write hold report until the reviewed module repair. Unknown provider IDs are refused.
A passing report always says `release_qualified: false`; it cannot update release flags.

## Consequences

An ordinary owner can collect repeatable evidence with one command without saving credential input files. IAM
restrictions, negative privilege tests, parallel issuance, actual lifetime renewal/ARI scheduling, provenance and
root/service testing remain separate release gates. The fake-executable tests exercise orchestration, not live IAM
or ACME compatibility. A failed run can retain DNS records and private intents requiring explicit reconciliation.

## Alternatives

Using the existing file-input harness alone leaves secrets on disk and no renewal/report evidence. Reading vault
values through a new export endpoint would weaken the infrastructure credential boundary. Fresh issuance with a
second hostname would not exercise renewal of existing storage. Automatically cleaning uncertain writes could
remove foreign records. None of those alternatives is used by the kit.
