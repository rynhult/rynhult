# Record Codex trust only for ACC's own hook definitions

Renumbered on 2026-10-24 from 0012 to 0037.

Date: 2026-10-23

## Context

ACC installs its own account-level Codex hooks. New machines can stop at Codex's hook-review dialog before a session reports starting. Trusting all hooks would also approve commands ACC did not install. The persisted trust key and normalized hash are internal Codex formats that may change across releases.

## Decision

Bootstrap records the upstream hash only for entire groups exactly equal to ACC's generated minimal command groups, using their original indices and the actual hooks.json path. Probe the configured CLI and accept only exact reviewed releases (initially 0.163.0 and 0.164.0). Preserve all unrelated config text, validate the parsed candidate against precisely the intended state updates, and retain explicit disablement. Unknown versions or formats receive no trust writes. Never use a global hook-trust bypass or answer the dialog automatically.

Detect remaining review dialogs through screen inspection and show Needs you with a plain explanation. The implementation and fixture provenance are documented in [the source research](../research/codex-hook-trust.md).

## Consequences

Fresh machines using reviewed releases can run ACC hooks immediately; other hooks still require the person's review. Each future Codex release requires source review before the allowlist advances. Unsupported config layouts may retain manual review. This trusts ACC's already-installed command definitions, not the agent account or arbitrary hook scripts as an OS boundary.

## Alternatives considered

- Trust all hooks or pass a global bypass flag: would approve unrelated hooks.
- Send keystrokes to the review dialog: depends on TUI layout and can approve unrelated hooks.
- Require manual review everywhere: preserves current unattended-start failure.
- Assume the format remains compatible across a version range: not sufficiently strict for a safety-prompt bypass.
