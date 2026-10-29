# Auto-resume and goals

Approved owner request, 2026-10-22. Both controls are per session, optional and off by default. They can be changed by the session's person or an owner at launch and while running. Goals remain independent of Orchestrator teams and scheduled sessions.

## State and safety

Store settings and progress in the database, with stable session and continuation identities. Reconcile observes provider limit text and idle/permission state. An exhausted usage window supplies its reset timestamp; CLI reset text is the fallback. Preserve that timestamp across polling and restarts. Display “Waiting for the limit to reset at <time>”. Claim each reset before dispatch, never retry an ambiguous send: avoiding duplicate input takes precedence over automatic delivery. Record dispatch results and expose failures. A person typing cancels pending work and stops goal continuations, including terminal input before submission.

Only providers with identifiable limit and idle signals enable auto-resume. Unsupported controls explain why. Permission prompts always stop goals. The shared message path must send literal text and a distinct submit key, allowing the CLI to process the inserted text before Enter (including Codex).

## Goals

A goal has text, a maximum continuation count, maximum elapsed hours and an optional maximum plan-window usage percentage. Instructions explain standalone `GOAL: done` and `GOAL: blocked: <reason>` lines. Only assistant output can terminate a goal. No second model judges completion; model-based judgment may be an explicit option later. Each idle turn is eligible once, subject to caps, human input, permissions and the person's budget pause. Limit waiting takes precedence over goal continuation. Reset continuations also count toward a goal's cap.

The Goal panel shows status/reason, elapsed time, continuations used, caps, editable settings and Stop. Budget pauses remain visible and prevent automated messages. Changes and continuation lifecycle events are audited as `session.autoresume` and `session.goal.*`.

## Verification

Use provider text fixtures and deterministic clocks for detection/reset choice, deduplication, restart behavior, human cancellation, caps, markers, permission and budget stops. Exercise launch/running edits and ownership through API tests, and controls/progress through Playwright. Real authenticated provider TUIs require separate live verification; fixture tests must not be described as that verification.
