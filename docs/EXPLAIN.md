# Explain these changes

Approved scope: competitor gap 8, 2026-10-22. This is a reading aid, not
a review, an approval, or an automatic follow-up to an agent's work.

## Interaction

Changes offers **Explain these changes**, an agent picker defaulting to the
session's provider, and an explicit launch button. Only installed, signed-in
CLIs belonging to the person clicking are eligible. A cached result opens
without launching anything. Regenerate is another explicit launch.

A responsive side panel shows a few ordered steps, Previous/Next controls,
and file/line links that open and focus the corresponding displayed diff.
Show agent, selected model, generation time, helper-session link, and
**generated; check the diff**. Loading, failure, stale revision and unsupported
agent states must be explicit. ACC currently links to PRs but does not render
a separate PR diff; sessions reviewing PRs use this same Changes surface.

## Execution and trust boundaries

Use the existing broker diff operation and vault redactor, never reread source
files through an agent. Hash the complete bounded diff response for its
revision. Above the prompt limit, first reduce each file to a bounded
statistical/hunk summary and excerpts; label omissions. No hidden second model
call or automatic regeneration.

Launch one visible, short-lived helper as the requesting person's agent account,
on the local broker, in an isolated run workspace, not the source checkout.
Keep the normal session/run/audit records and usage hooks. No project keys,
MCP servers, catalog skills, remote control, endpoints or source instructions.
Researcher-style read-only native permissions plus a fail-closed all-tools
guard deny edits, shell execution, subagents and network tools. Only the CLI's
model connection is needed. This is CLI permission enforcement, not a new OS
sandbox.

A bounded runner invokes the selected native CLI in noninteractive mode and
leaves its structured response as `.acc/artifacts/walkthrough.json`. Maximum
wall time: 180 seconds; maximum result: 64 KiB. The helper stops after success,
failure or timeout, and is never automatically resumed or relaunched.
Unsupported CLI capabilities fail closed rather than weakening restrictions.
Check the requesting person's existing launch budget before recording a helper;
a paused budget refuses the launch without silently overriding it.

## Data and API

Session-scoped explanation GET returns the result for the current diff revision
or its running/error state; POST explicitly requests generation or regeneration.
Members can use only their own sessions; owners can use any. Helper ownership
and billing always belong to the requester, including when an owner explains
someone else's session.

Persist cache entries by source session and revision, including helper id,
provider, model, timestamp, status and validated steps. Serialize launch
reservation and check persisted running entries to prevent duplicate helpers.
GET never launches work. An old result never masquerades as the current diff.
After a server restart, reconcile the existing helper rather than repeat its
model request.

The JSON contract is `{"steps":[{"title":"...", "summary":"...",
"references":[{"path":"app.py","side":"new","start":12,"end":18}]}]}`.
Validate JSON, total bytes, step/text/reference counts and types. Drop references
to files or old/new line ranges not present in the captured diff; never construct
arbitrary URLs from model output. Reject malformed or oversized results with a
visible error. Render text, not HTML or model-supplied Markdown links.

## Verification

Focused Python tests cover strict validation, prompt reduction, redaction,
revision caching/invalidation, concurrent clicks, requester account, read-only
launch configuration, completion/failure, ownership and paused budgets.
Playwright uses the existing isolated test stack and a fake native CLI that
writes a walkthrough: explicit click, one helper, side panel, exact line links,
step navigation and regeneration. Update the Guide, internal docs, plan,
changelog and session handoff with verified outcomes and limitations.
