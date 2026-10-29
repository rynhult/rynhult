# Advisory permission labels

Owner-approved scope, 2026-10-22 (competitor research gap 10).

## Contract

People decide. **Routine**, **Review** and **Risky** describe a pending action;
they never answer, acknowledge, resolve or suppress a prompt, change permissions,
or replace guardrails. A short reason is always visible beside the badge.
Routine is neutral, Review amber, Risky red, using the existing Badge primitive.
Labels appear in Inbox and a session's Needs you banner.

## Data and rules

One server module classifies normalized hook fields (tool, command, file/search
paths and working directory) relative to the session workspace. It imports the
trusted `acc-guard` installed beside the configured hook, reusing its parser,
wrapper handling, command substitutions, operand handling and guard checks rather
than maintaining a second shell grammar. Guard policy and permission decisions
remain unchanged; synthetic parser words now preserve their character metadata,
fixing a tool-approval crash exposed by `su -c` classification.
No command is executed. Guard checks run with all built-in rules enabled and the
session's frozen custom rules; tool-approval refusals are Review, other refusals
Risky. Explicit credential access, privilege escalation, network-to-shell pipes
and writes/deletes outside the workspace are Risky. Network operations, installs,
and workspace writes are Review. Only recognized read-only operations within the
workspace are Routine. Unknown operations and unresolved details are Review.
Highest risk wins across compound commands; quoted prose is not executable code.

Hook ingestion attaches only the derived label/reason to the deduplicated
attention item, in a nullable JSON column. Raw arguments are not copied into it.
Existing items without metadata get Review. Screen detection retains its existing
provider-specific authority over whether a prompt is present (notably Copilot's
advisory permission hooks); screen text is not a trustworthy structured command,
so those items get **Review: details on screen**. New hook evidence replaces old
metadata; resolution and acknowledgement continue to work exactly as before.

## Preference and UI

Settings offers **Show advisory risk labels**, on by default, saved per signed-in
person through the existing Setting store. Turning it off hides labels in both
surfaces, including historical items, without mutating attention or agent state.
The session banner reads unresolved attention including acknowledged items, so
acknowledging Inbox cannot hide the still-pending prompt.

## Verification and limitations

A literal test table covers each provider's payload normalization and screen
prompts, shell nesting/wrappers/redirects, path boundaries, credentials, installs,
unknowns and guard parity. Integration tests verify persistence, deduplication,
preference isolation and unchanged prompt state. Playwright exercises both badge
surfaces and Settings with artificial data, including narrow layouts.

This is static advice, not a sandbox or a safety guarantee: arbitrary scripts,
runtime expansions, symlink targets and remote paths cannot be proven safe.
Unknowns stay Review. An unavailable parser is logged and shown as Review rather
than losing the underlying prompt.
No model calls or external classifiers are used. A future optional AI second
opinion would need a separate privacy, latency and failure-mode design, and must
remain advisory.
