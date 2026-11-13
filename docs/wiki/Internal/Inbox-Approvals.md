# Inbox approval cards

Cards extend the existing needs_input Inbox flow, not delegated approvals.
`approval_cards.py` reads the server-computed `acc_complete_input` snapshot after
vault redaction, before generic event trimming (8,192 UTF-8 bytes maximum).
Claude Code and Codex PermissionRequest events share that existing input allowance;
no schema change or migration is needed. Working directory uses the existing hook
cwd field only when it is known not to have reached the 2,000-character trim limit.
No workspace fallback is presented as a CLI-supplied working directory.

Sources: [Claude hook input](https://code.claude.com/docs/en/hooks#permissionrequest-input)
and [official OpenAI hook documentation](https://developers.openai.com/codex/hooks).
These document cwd and tool_input.command. Tool arguments can contain a literal
diff; absent diffs stay absent. Edit old/new strings and Write content are never
converted into a diff. Codex apply_patch's command remains literal patch text.

The classifier scans bounded command text, tool name and explicit paths. First
match wins: history rewrite, deletion, secret path, cloud spend, service stop,
package install, external access, No specific hint. It deliberately makes no claim to parse
shell semantics. Command classification examines the first and last 4,096 characters (covering
the bounded complete-input snapshot) and uses
segment token sets, avoiding repeated unbounded regex runs. The card is computed
once at ingest and stored as acc_card in the event JSON; read/list/Push paths never
classify or rebuild it. Older pending events without a stored card are not backfilled
in a read transaction. Both event queries accept only the actual session provider’s own hook source.
There is no legacy or foreign-provider fallback for tokens, input or cards.
Reply authorization still requires a Claude session and all existing checks.
The batch query uses the authorized session metadata in a SQL CASE expression,
keeping one session read and one event read without correlated session lookups.
Existing Routine/Review/Risky advice shares the Hints (not checks) heading. Labels
never affect approval availability, hook replies, policy, actor recording or audit.
React text nodes escape every field; no HTML, Markdown or syntax interpreter runs.

Push uses the stored facts, the first non-empty command line (up to 180 characters),
or the item detail when no command exists. Shortened lines get … and additional
non-empty lines get (+N more lines), within 240 characters total including the label and warnings. Existing notification recipients remain; session input notifications open the selected Inbox item.
Only the existing Claude hook token enables Inbox actions. Codex cards are
informational. Fifty-second expiry, container exclusions, scope checks and
complete-input checks remain in force. No keystroke answering is introduced.

Focused checks: tests/test_approval_cards.py, tests/test_inbox_permissions.py,
web/e2e/approval-cards.spec.ts, web build, payload budgets, E2E isolation and
truthful documentation checks. These do not qualify live vendor interoperability.

Hidden C0/C1 characters (except newline/tab), Unicode format characters including
bidi and zero-width controls, and U+2028/U+2029 are replaced for display with
⟨U+XXXX⟩. The shared browser helper and server Push helper implement the same
mapping. Cards and Push warn “Hidden characters shown”. Raw bounded input stays
unchanged for the CLI decision and audit: permission.answer records input_sha256,
the SHA-256 of the exact redacted acc_complete_input UTF-8 string (null if absent).
No migration is needed. The hash covers the complete underlying displayed input,
not HTML, visible-character substitutions or just the command excerpt.

Reserved acc_card/acc_complete_input keys are removed from every agent hook before
any event-specific handling. Only Claude/Codex PermissionRequest ingest regenerates
them. Read paths expose these fields only for claude_hook/codex_hook sources,
validate nullable string fields and the known label set, and discard invalid cards.
The UI independently validates card shape and displays an unavailable-details
message if malformed data reaches it. Unsupported provider events, including old
stored events with forged fields, cannot supply cards or complete input.
