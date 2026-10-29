# Accessible terminal output

Renumbered on 2026-10-24 from 0015 to 0045.

Date: 2026-10-24

## Context

UX findings SE-9 / SESSION-03 require readable output, a discoverable keyboard exit and a text alternative. Tab is also a necessary CLI completion key. A tmux attachment redraws the current screen in xterm, so the browser buffer does not reliably contain tmux's retained history.

## Decision

Keep F6 / Shift+F6 as the terminal exit, capture it around both xterm input and accessibility rows, and provide a keyboard-visible skip link. Enable xterm screen-reader output by default for session views, with a browser-local toggle; Grid tiles remain compact. State announcements use the shared polite announcer rather than reading the output stream continuously.

Read text snapshots on demand through an authenticated session endpoint and the session's own broker account and machine. Capture at most 10,000 history rows plus the current screen, join wrapped lines, omit terminal formatting, and return only the last 200,000 characters. The endpoint uses the same ownership boundary as session access and sends `Cache-Control: no-store`. It neither takes terminal control nor types into the session.

The text dialog holds a snapshot until Refresh. A failed capture falls back to the parsed browser buffer with an explicit warning that older lines may be missing. Clipboard denial leaves the selectable text alternative available.

## Consequences

The text view can include retained output from before this browser attached. It is not a conversation transcript or a recording: erased screens and output beyond tmux's retention are unavailable. Like the live terminal, it can contain secrets, and is never persisted by this feature. Unlike the live terminal it is a copyable record, so (security review 2026-10-24) the server redacts the session's vault keys as for transcripts and exports, re-applies the 200,000-character cap rather than trusting the broker or node reply, records each read as `terminal.read` in the audit log, and allows 20 reads per person per minute. The browser-buffer fallback cannot be redacted; it shows only what the live terminal already showed. Real NVDA and VoiceOver validation remains necessary before claiming assistive-technology conformance.

## Alternatives considered

- Reading only xterm's buffer: misses history when tmux redraws the screen.
- A continuously updated transcript/live region: disrupts reading position and repeats rapidly changing CLI output.
- Reassigning Tab: breaks completion and terminal applications.

## Owner confirmation, 2026-10-24

Decision 0020 (private decision record) retains
20 snapshots per minute per person. When the audit write is busy, keep matching
the live terminal: allow the read with a warning. Revisit the rate limit and
this availability/audit tradeoff together.
