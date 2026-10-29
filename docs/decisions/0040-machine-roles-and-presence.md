# Machine roles and presence

Renumbered on 2026-10-24 from 0013 to 0040.

ACC must distinguish expected workstation shutdowns from outages on machines intended to stay online.

Store the owner's role (`always_on` or `workstation`) on each enrolled node. The OS supplies a default only at enrollment and during the one-time migration. Reconnects, version checks and OS changes never rewrite the role. A pending enrollment may leave the role unset until its OS is known.

Use the authenticated outward node link as the availability signal. Background broker work, spool reads, transcript indexing, key cleanup, backup rounds and team deliveries skip disconnected links. Presence transitions persist the outage start, emit one log entry and audit record, and resolve alerts with downtime on reconnect. The supervisor evaluates elapsed time against the stored threshold; it does not probe offline machines. An already-observed outage survives an ACC restart. When there is no recorded outage, the new primary starts a fresh observation window rather than treating its own downtime as proof the node was offline.

Always-on outages use ordinary owner Inbox attention items and push delivery. Maintenance snoozes suppress open-list visibility and notification delivery without discarding outage history. Reconnect ends the snooze. Roles, thresholds and snoozes are owner-only audited edits.

An owner can explicitly check all machines, once per 30 seconds per server process. Each connected machine gets a fresh bounded broker round-trip, returning version and live host readings. Disconnected nodes return recorded presence; the main ACC cannot dial them. Cached results are owner-only and shared between Host and Overview. Older brokers can answer ping but cannot return the new snapshot extension, which the UI reports.

Alternatives rejected: OS-derived roles on every reconnect would overwrite owner intent; periodic connection attempts cannot reach outward-only nodes and produce noise. A separate alert store would duplicate Inbox lifecycle and push behavior.

## Opt-in workstation sleep

Owners may set a per-node idle sleep duration (off by default; Host initially chooses 30 minutes). Only connected, active Workstation nodes qualify. Pause resumable ACC-managed idle conversations through ordinary stop/resume, with a durable idle clock, pause marker and per-pause audit; never archive them or change OS power settings. Active and answer-waiting sessions are excluded. Refresh the screen, process output and hooks before stopping and defer on missing or failed evidence. Always-on machines ignore the setting. Host reports ACC's running-session count and idle names, not measured OS sleep assertions.

An unconfirmed stop stays visible as pending and counts as possibly running until reconciliation confirms it. Resume is one click after confirmation. Reusing the existing lifecycle avoids a second pause mechanism and lets restart recovery and key cleanup follow ordinary stop semantics.
