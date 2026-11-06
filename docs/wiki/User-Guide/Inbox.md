# Inbox

> Screenshots use fictional demo accounts and fixed example metrics, not live systems or qualification evidence.

**Dismiss all** appears only when the current view has at least one dismissible notice. It takes a snapshot of the items currently shown under Open or History. Confirm **Dismiss N items?** on the page, or Cancel. Decisions, active machine/backup alerts and already handled notices are skipped; the page shows how many. New or changed notices stay untouched. **Undo** is available for eight seconds and restores notices that have not changed since dismissal. It never answers a prompt or approves a request.

The inbox lists everything that needs you. Items come from agent hooks, an agent's approval prompt on its screen (Cursor and Antigravity have no hook for it, and Copilot's fires even for tools it then approves itself), process state, login checks, CI results, host health, guardrails, budgets and agents' publishing or tool requests. An agent request is not authorization to execute it.

Open items are grouped by type, in this order: **Waiting [sessions](/User-Guide/Glossary#session)** (an agent waiting for your answer, or a session needing recovery), **Approvals** (publishing and tool requests), **Alerts** (failures and warnings) and **Notes** (for your information, such as a guardrail refusal). Always-on offline and failed/stale backup alerts come first within Alerts; otherwise, newest first within each group. **History** lists past items by date.

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

2. Use the action for the item you want to review; other alerts remain open.

![The inbox, grouped into waiting sessions, approvals and notes](/.attachments/inbox.png)

| Item | What to do |
|---|---|
| *session* needs your input | Claude permission hooks show the tool and a shortened, key-redacted command or path. **Approve** or **Deny** answers that one live request; **Answer in terminal** opens the session for other prompts and providers, including Codex. |
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
the full selection controls will arrive with the new layout. A batch does not
authorize future changes, and a changed or expired preview must be reviewed again.

### Claude permission replies

In session **Setup**, **Answer permission prompts from the Inbox** defaults on.
Inbox replies are available for up to 50 seconds while nobody is watching the
terminal. Attaching a tmux client or a browser terminal writer or observer returns
the prompt to Claude immediately (checked on each half-second wait tick). If
attachment state cannot be checked, use **Answer in terminal**. Container sessions
currently use terminal-only permission replies.

Read the complete redacted tool input in the scrollable panel before choosing
**Approve** or **Deny**. All tool fields are included, including file contents,
edits, prompts and MCP arguments. If the complete input exceeds 8 kB or is missing,
only **Deny** and **Answer in terminal** are available, with a reason. Expired
requests cannot be answered from the Inbox. Success shows **Approved** or **Denied**.
