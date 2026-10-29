# Vault variable suggestions

Renumbered on 2026-10-24 from 0010 to 0033.

## Context
People adding vault keys need the variable name their tool expects. Free text
alone makes common names hard to discover, but a fixed allowlist would exclude
application-specific settings.

## Decision
Keep provider-grouped names and descriptions in `web/src/lib/envVariables.ts`.
Add and Edit use the existing native Select primitive, ending with Custom and
the original normalized text input. New session reuses descriptions for known
variables. Mark visible stored names without disabling them: separate keys may
legitimately share a variable.

## Consequences
Keyboard and screen-reader interaction remain native, including on phones.
Suggestions do not configure providers, create credential files or replace the
server's reserved-name validation. The existing `.env` import is unchanged.

## Alternatives considered
A fixed allowlist would break custom settings. A separate searchable combobox
would add interaction and accessibility code without an existing primitive to
reuse. Duplicated lists in each picker would drift.
