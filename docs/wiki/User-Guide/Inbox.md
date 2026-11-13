# Inbox

> Screenshots use fictional demo accounts and fixed example metrics, not live systems or qualification evidence.

Select the notices you want to dismiss, then choose **Dismiss selected**, or use **Dismiss all shown** for the current page and category under **Open** or **History**. Only eligible notices are sent; decisions, approvals, active machine/backup alerts and already handled items stay open. New or changed notices stay untouched, and the result reports items skipped because they changed. **Undo** appears inline for eight seconds and restores notices that have not changed since dismissal. It never answers a prompt or approves a request. There is no bulk-dismiss confirmation modal or bottom banner.

The inbox lists everything that needs you. Items come from agent hooks, an agent's approval prompt on its screen (Cursor and Antigravity have no hook for it, and Copilot's fires even for tools it then approves itself), process state, login checks, CI results, host health, guardrails, budgets and agents' publishing or tool requests. An agent request is not authorization to execute it.

Use the sidebar categories **Decisions**, **Approvals**, **Blocked work**, **Failures** and **Information**, or **All items**. Select a row in the item list to review its details and actions. The current page shows up to 20 items; **Previous items** and **Next items** move through the returned results. **History** includes handled items.

## Compact status, without blocking the page

In the [new layout](/User-Guide/New-Layout), the Inbox rail icon has a small open-count badge: hidden at zero, capped visually at **99+**, with the full count in its accessible name. There is no notices banner. All notices, including machine and backup alerts, remain reachable in Inbox. An unavailable or out-of-date count is announced through the button; open Inbox for its error and refresh controls.

![Desktop Inbox count in the rail](/.attachments/inbox-status-desktop.png)

![Phone Inbox count in the rail](/.attachments/inbox-status-phone.png)

![Desktop Inbox error with retry controls](/.attachments/inbox-unavailable-desktop.png)

![Phone Inbox error with retry controls](/.attachments/inbox-unavailable-phone.png)

Machine offline, backup failed and backup stale alerts remain available in Inbox. **Acknowledge** records that you saw an alert but leaves it open and counted. **Snooze 1 hour** hides that item until the hour ends; **History → End snooze** restores it sooner. Reconnecting the machine or fixing the backup resolves the alert. Machine snoozes also appear as planned maintenance in Host.

The optional usage-statistics prompt stays in Inbox: **Review privacy choices** opens Settings, or **Dismiss notice** clears the invitation without enabling anything.

## Review individual items

1. Select **Inbox** in the rail.

![Individual machine and backup alerts with Review, Acknowledge and Snooze actions](/.attachments/inbox-operational-alerts.png)

2. Select the item in the list, then use its action in the detail pane; other alerts remain open.

| Item | What to do |
|---|---|
| *session* needs your input | Claude and Codex permission hooks show CLI details with a wrapped, key-redacted command when supplied. **Approve** or **Deny** answers that one live request; **Answer in terminal** opens the session for other prompts and providers, including Codex. |
| Recovery needed | **Open session**, read its **Activity**, then **Resume** it |
| Login expiring or logged out | Re-login on [Host and providers](/User-Guide/Host-and-Providers) |
| CI failed | Open the run on GitHub from the [project](/User-Guide/Glossary#project) page |
| *session* asks to publish *host* | Read who asked, for which project, whose port it is and who can reach it, then **Approve** or **Reject**. An amber note marks anything unusual, such as a port no project reserved; then the button reads **Review and approve** and asks first (see [Publishing](/User-Guide/Publishing#requests-from-agents)) |
| Install or remove tool *package* | Check the manager, package, exact version (or latest), machine, [agent account](/User-Guide/Glossary#agent-account) or system-wide destination, and reason. **Approve** executes the change; **Reject** executes nothing. Members approve only their own account's tools; owners approve system-wide tools. |
| Retarget live site *host* | The name is live already: **Review and approve** shows what it serves now and where it would point; approve only if you meant to move it (see [Publishing](/User-Guide/Publishing#a-request-for-a-name-that-is-already-live)) |
| Disk, memory or service problems | Check [Host](/User-Guide/Host-and-Providers) |
| Always-on machine offline | **Review machine** opens Host. Reconnect it, or use **Snooze 1 hour** for planned downtime |
| Rynhult backup failed or stale | **Review backups** opens Host's Backups section. Review the failed job or capture a verified backup; snooze only when you intend to defer the warning |
| *session*: guardrail blocked … | Nothing to answer: the agent was refused and told why. Check its terminal if it gets stuck (see [Guardrails](/User-Guide/Guardrails)) |
| *window*: N% used, past your budget | Slow down, or **+5% until it resets** in Settings → Budgets (see [Budgets](/User-Guide/Budgets)) |
| *session*: [worktree](/User-Guide/Glossary#worktree) kept | Commit or discard the changes in that folder, then `git worktree remove` it (see [Sessions → Worktrees](/User-Guide/Sessions#worktrees)) |

New items can also reach your phone as push notifications (see [Notifications and app](/User-Guide/Notifications-and-App)).

Each item ends with where it came from, in words: **Claude Code** (or another agent's name) when its hook reported it, **Agent request**, **Guardrail**, **Host monitor**, **Sign-in check** and so on. Hover it for the exact source. A grey **guessed** chip marks the exception: a prompt Rynhult read from the screen because that agent has no hook for it.

**Dismiss notice** clears an ordinary item; it never answers anything for you. On a waiting session it sits behind **⋯**, because the session keeps waiting until you answer in its terminal. Operational alerts use **Acknowledge** and **Snooze 1 hour** instead. Items also clear themselves when the condition goes away. **History** shows past items, with dismissed ones marked **dismissed** and acknowledged operational alerts marked **acknowledged**.

### Claude permission decisions

Only a signed-in person allowed to type into the session can answer. Each click is
recorded in Audit as `permission.answer` and `permission.result`. A second click,
an expired hook, or an item already handled in the terminal returns **Already
answered**. No permission rule is saved and no prompt is answered automatically.
The hook waits up to 50 seconds for your decision, then returns control to Claude's
terminal prompt without a decision. Existing sessions need to use the updated hook
on their machine. Codex has no reply channel implemented here: use **Answer in terminal**.
See Claude's [PermissionRequest hook response](https://code.claude.com/docs/en/hooks#permissionrequest-decision-control).

## Team prompts

[Orchestrator](/User-Guide/Orchestrator) helper prompts are labelled with their team. Open the helper to answer. The lead can inspect status and send ordinary messages, but cannot answer permission or input prompts on your behalf. A team time-limit notice also reaches your Inbox.

## Advisory risk labels

1. Open **Inbox** and read the badge and reason below a session prompt.

![An Inbox prompt labelled Risky because it pipes a network download to a shell](/.attachments/risk-labels-inbox.png)

| Label | Meaning |
|---|---|
| **Routine** | A recognized read-only tool or command reads within the session's project or worktree |
| **Review** | A project write, install, network request, outside read, or action Rynhult cannot classify |
| **Risky** | A guardrail would refuse the action, or it involves outside writes/deletes, credentials, elevated privileges, or a network download piped to a shell |

2. Read the tool details. For a live Claude permission hook, choose **Approve** or **Deny**. Otherwise select **Answer in terminal**, inspect the actual request in **Terminal**, and decide there.
   **Routine is not a safety guarantee.** Labels never answer, approve or
   dismiss a prompt, change the agent's permissions, or replace guardrails.
   No AI model is called.
3. To hide labels, open **Settings** and untick **Show advisory risk labels**.
   The choice is saved for your sign-in on every device, not for other people.
   Prompts and guardrails are unchanged.

**No label** means Rynhult saw an approval prompt but the agent sent nothing it
could judge (Claude Code's permission notice, or a prompt found on screen). Read
the terminal; Rynhult does not guess a command from screen text. An action it can't
classify shows **Review: unknown action; check the details**.

**Troubleshooting:** a label can miss runtime script behavior or symlink targets.
If the reason seems incomplete, inspect the full request rather than treating the
badge as permission. Dismissing the Inbox notice does not remove the session's
Needs you banner while its prompt remains pending.

## Approving a tool

1. Open **Inbox**. Find **Install tool** or **Remove tool** and read its destination before deciding.

![A registry tool request waiting in the Inbox](/.attachments/tools-inbox.png)

2. Choose **Approve** or **Reject**. Approval changes the named machine/account, not necessarily this machine. Nothing else in the Inbox approves it.
3. Open **Providers → Tools** for the resolved version and request history. Failure raises another Inbox item with the reason. If Rynhult restarts or a machine disconnects during an install, inspect that account before requesting it again: Rynhult does not replay uncertain work automatically. Removing a machine or replacing
its connection also interrupts its pending tool work on the main. An interrupted
request is marked failed when possible; that does not undo an install already
started on the machine. Inspect the reported destination before trying again.

Every request, approval, rejection and result is recorded in [Audit](/User-Guide/Audit) as `tool.request`, `tool.approve`, `tool.reject` and `tool.result`.

### Exact decisions and duplicates

Identical pending tool changes from workers in the same project, account and
explicit team share one approval card. Solo sessions do not share cards. If that
account/tool already has a pending change, resolve it before requesting it from
another solo session. Changing the package version or operation needs its own
review. Approving a request records the real person and reason; the resolved card
and **Providers → Tools → Request history** show **Decided by**. Approval means
permission to try the change; check the individual result before assuming success.

The server also supports reviewed batches of up to 50 exact pending tool changes;
these tool-approval batches are separate from dismissing notices and do not share its selection controls. A batch does not
authorize future changes, and a changed or expired preview must be reviewed again.

### Claude permission replies

In session **Setup**, **Answer permission prompts from the Inbox** defaults on.
Inbox replies are available for up to 50 seconds while nobody is watching the
terminal. Attaching a tmux client or a browser terminal writer or observer returns
the prompt to Claude immediately (checked on each half-second wait tick). If
attachment state cannot be checked, use **Answer in terminal**. Container sessions
currently use terminal-only permission replies.

Read the redacted **Complete tool input** before choosing (expand it for command requests)
**Approve** or **Deny**. All tool fields are included, including file contents,
edits, prompts and MCP arguments. If the complete input exceeds 8 kB or is missing,
only **Deny** and **Answer in terminal** are available, with a reason. Expired
requests cannot be answered from the Inbox. Success shows **Approved** or **Denied**.


## Delegated team activity

**Team activity** shows the latest 50 delegated operations with the deciding lead, permission issuer and observed outcome. Expand the digest to inspect originals. These entries do not need an answer. Failed or uncertain image checks remain failed or unknown; the activity digest cannot approve publication, root changes or a provider permission prompt.

### Ready for review

**Ready for review** has its own group, separate from approvals. It contains
informational notices for sessions that finished with branch changes and no
pending question. Each session produces at most one notice per process run, even
if you dismiss it and the agent continues in that run. Dismiss it individually or
with the existing bulk dismiss control. These notices do not send push alerts.
Members see their own notices; owners see notices within their existing access.

## CLI approval details

Claude Code and Codex permission cards show the supplied command and working
directory. A literal unified diff is shown only if the tool payload supplies one;
Claude Edit supplies old/new text, so review its expanded tool input instead. Commands wrap without shortening; diffs over
40 lines have **Show all**. Text is displayed literally. Missing or oversized
fields are identified plainly; Rynhult never reconstructs a diff from file contents.
Vault redaction still applies before storage. Complete tool input remains available
for review. For tools with neither command nor diff, the tool name, file path and
complete input appear expanded. Oversized input has an explanation and no empty panel.
Hidden format/control characters appear as visible ⟨U+XXXX⟩ tokens with a
**Hidden characters shown** warning in the card and Push. Newlines and tabs remain readable.

Each card has one deterministic advisory label: Deletes files, Rewrites history,
Network / external message, Installs packages, Restarts or stops services,
Spends money, Touches secrets paths, or No specific hint. **Label is a hint, not a check**:
it does not authorize anything or change policy. Both advisory classifiers appear
under **Hints (not checks)**. Push includes a bounded command
first non-empty line and label, with … for shortening and (+N more lines) for additional non-empty lines; open the app for the complete card. Phone Inbox uses the same card.

Approve and Deny retain the existing Claude Code permission-hook transport and
person audit. Codex cards do not add an answering transport: use **Answer in terminal**.
No action simulates approval keystrokes. Oversized input cannot be approved in Inbox.
