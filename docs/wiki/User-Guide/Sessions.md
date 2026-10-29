# Sessions

> Screenshots use fictional demo accounts and fixed example metrics, not live systems or qualification evidence.

A session is one native agent conversation (Claude Code, Codex, Copilot, Antigravity, Cursor or OpenCode) running in tmux on its selected machine, in a [project](/User-Guide/Glossary#project)'s folder or a [workspace](/User-Guide/Workspaces).

Rynhult retains startup hooks that arrive during launch and uses them to verify the new run. If the agent does not report starting within its verification window, Rynhult asks you to inspect the terminal.

When a process ends, Rynhult reads its pending hook events before closing that run so its reported exit reason
stays with the right run. A large backlog is read in bounded batches and can delay the final state. For a remote
session, closing the run waits until its machine's last hooks can be checked. Existing history-size limits still
apply; a truncated final hook cannot hold a dead run indefinitely.

## Finding a session
- **Sidebar** lists running sessions under **Needs you**, **Working**, **Unknown** and **Idle**: up to five each, then **+N more**. When many are running, that list scrolls on its own, so **Operate** and **Configure** stay in view. Select a group's name (or **+N more**) to see all of its sessions on the Sessions page. The number beside **Needs you** is how many sessions are blocked on you, the same number as on the Overview.
- **What it's doing now.** While a session works, its row and its page header show its current step, from the agent's own tool events: "Thinking", "Running npm test", "Editing src/api.py", "Asking to run rm -rf build". It clears when the turn ends. Antigravity and Codex report steps after they finish ("Ran …").
- **Claude background work.** After a turn ends, running shells, monitors or helper agents shown at the bottom of Claude's terminal keep the session under **Working**, with "Working in background". It becomes **Idle** when those indicators disappear. Permission requests still show **Needs you**.
- **Sessions** lists all of them, what needs you first: **Needs you**, then **Working**, **Unknown**, **Idle** and (with **Stopped** or **All**) **Stopped**, each newest first, under the same headings as the Overview. **Recent** lists them by last activity instead. Filters: **Live / Stopped / All**, a text filter (name, project or id), agent and project. Arriving from a group link shows only that group, with a button to show all.

![The sessions list groups sessions by priority and offers filters](/.attachments/sessions-list.png)

Every row names its **machine**, including the [main Rynhult](/User-Guide/Glossary#main), for example **acc-host** or **lab-box**. Wide session tables have a dedicated **Host** column. In Overview's **Live sessions**, select **Host** to sort ascending, then descending within the priority groups. On phones the compact **· on acc-host** metadata stays visible even beside long names. The status board names every machine too.

![Session rows identify both the main machine and another enrolled machine](/.attachments/sessions-machines.png)

![Compact inline machine names stay visible beside session metadata on a phone](/.attachments/sessions-machines-phone.png)

### When a working status is too old

After ten minutes without fresh hook state or activity, a hook-based **Working**
status becomes **Unknown since** its last evidence time. The old **Thinking** or
tool activity is no longer presented as current, and the session moves to an
**Unknown** group instead of being counted as Working. This also covers older
records without state/activity timestamps, using their creation time.

Open the terminal to inspect it. Unknown does not stop, resume or change the
agent's lifecycle. A fresh hook restores the current state. **Needs you** is not
expired merely because a permission question has been waiting for an answer.

![An old hook is labelled Unknown rather than Working or Thinking](/.attachments/sessions-unknown.png)

![Unknown status and its evidence age on a phone](/.attachments/sessions-unknown-phone.png)

## Grid: several terminals at once
**Grid** (in the sidebar) shows up to four live sessions side by side, each as its real terminal: watch several agents work and answer whichever needs you without switching pages.

- The first time, it picks the sessions most likely to need watching (**Needs you**, then **Working**). **Choose sessions** picks others, in the order you want them; the choice is kept in the address (so you can bookmark it) and remembered in this browser.
- Each tile gives its session name room before secondary activity text, then shows its status (and, on a wide screen, its agent and what it's doing now). The arrow opens its page; **×** takes it off the grid (it keeps running).
- The one-window-types-at-a-time rule still applies: a tile whose session is open with control somewhere else shows **Read-only** with **Take control**. A tile you can type into says nothing.
- **On a phone** the grid shows one terminal at a time: the chosen sessions sit in a row of tabs above it, each with its status dot, so you can switch between them.
- A tile is smaller than the session page, and the agent's screen follows the size of the last window that typed into it, so text can wrap differently when you go back to the full page.

![Grid session names keep space ahead of lengthy activity descriptions](/.attachments/grid-labels.png)

![The phone Grid keeps its session name and one-terminal switcher](/.attachments/grid-labels-phone.png)

## Worktrees
Several agents in one project's folder see and change the same files. Tick **Own worktree** in New session to give a session its own working copy instead:

- Rynhult creates it with git (`git worktree`) next to your projects, in `.worktrees/<project>-<id>`, on a new branch `acc/<session name>-<id>` from the project's current commit. The session page shows the branch.
- The agent works, commits and pushes on that branch as usual; the project's own folder and branch are untouched. Its **Changes** tab compares that checkout with [its current branch's base](#how-changes-chooses-its-comparison), not just commits authored by this session.
- **Archiving** an ordinary session removes the worktree folder if everything in it is committed. The branch and its commits always stay, ready for a pull request or a merge. If there are uncommitted changes, the folder is kept and the [Inbox](/User-Guide/Glossary#inbox) says so; commit or discard them there, then remove the kept worktree with the command below. Replace `<folder>` with that worktree's path. Multi-run worktrees are always kept for comparison, even when clean.
  ```sh
  git worktree remove <folder>
  ```
- The project needs to be a git repository with at least one commit.

## The session page

The terminal keeps its input editor in place while background status refreshes. Browser spelling corrections and inline writing suggestions are disabled in this field so they cannot rewrite text already sent to the agent. Safari still supports ordinary typing, paste and composed text; only committed input is sent, once. Panels in other tabs do not keep polling while you type. Pending message deliveries refresh quickly; empty or settled delivery history checks less often, and pauses its timer while the browser tab is hidden.

When the server build changes, **A new version is available — reload** appears above the page. Save or copy unsaved edits and unsent terminal text first, then choose **Reload** and confirm. The page stays open until you confirm. An older tab may need one manual reload before it can show update notices.

This notice detects a changed build; it does not install an update. A failed
download can also show **Rynhult was updated, reload to continue**: check your
connection and save or copy unsaved input before reloading. The notification
worker does not keep an offline copy of the dashboard.

![A session with its live terminal, screen reader toggle and text output controls](/.attachments/session-page.png)

When the session needs an answer, the header shows **Needs you** with the same
advisory risk badge and reason as [Inbox](/User-Guide/Inbox#advisory-risk-labels).
On the **Terminal** tab that is all, so the terminal stays near the top: answer
there. On any other tab a **Needs you** banner says so too, with **Go to terminal**.
Dismissing the Inbox notice does not answer it or hide the banner. Settings can hide the advisory labels
without changing the waiting state.

Codex can queue a question while it continues working. When its live footer shows
**? 1 question** (or several questions) and **shift+← to answer**, Rynhult shows
**Needs you**. Open **Terminal** and press **Shift+Left** to open the question.
Rynhult then shows the visible question text in the activity line and Inbox.
Type your answer and press **Enter** to submit, or **Ctrl+]** to skip.
When the question controls disappear, Rynhult clears the notice and returns the
session to **Working**. Old question text in the conversation does not keep it waiting.

If a Codex turn ends with an error directly above its input prompt, such as
**Selected model is at capacity. Please try a different model.**, and there is no
**esc to interrupt** working indicator, Rynhult shows **Idle** with that error as its
activity. Open **Terminal**, address the error (for example, choose another
model), and send a new prompt. This is not a permission request or an automatic
retry. Old error lines higher in the conversation do not change the session's status.

![A pending prompt with an advisory Risky badge in the session's Needs you banner](/.attachments/risk-labels-session.png)

On a phone, the label and its reason sit under the session's name, so the terminal stays near the top of the screen:

![The Needs you label on a phone, with the complete advisory reason visible](/.attachments/risk-labels-phone.png)

- **Header.** Name (select the pencil to rename it), agent, status and what it's doing now, project, branch, handoff and artifact links, the tmux name, the conversation id (select it to copy) and the permission mode in words ("Accept edits"). Actions on the right: **Remote** (when Remote Control is on), **Export**, **Goal and auto-resume** (see [Goals](/User-Guide/Sessions#goals-and-automatic-continuation)), **Open in editor**, **Interrupt** and **Stop**. A local workspace session also has **Promote to project**.
- **On a phone** the header is one line: name, status and project. **Details** holds the agent, branch, handoff and artifact links, ids, permission mode, **Export**, **Goal and auto-resume**, **Open in editor** and rename. On a touch screen, **Interrupt** and **Stop** are behind **⋯**, away from the terminal, so a stray tap can't stop an agent mid-edit. The tabs show **Terminal** and **Changes**; the others are under **More**, and a tab opened from a link is always shown.

  ![A session on a phone: one-line header, Terminal and Changes tabs, More, and the key bar with Enter first](/.attachments/phone-terminal.png)

  The [Inbox status line](/User-Guide/Inbox#compact-status-without-blocking-the-page) sits below the content and above the bottom navigation on phones. Alerts never push the terminal down. Always-on machine offline and failed/stale backup counts stay on screen until resolved or snoozed; select **Inbox** to review them. Desktop sessions keep the line below the top bar.
- **Terminal.** The real terminal. When you have control, your keystrokes go to the agent without an extra Read-write label. Only one window can type at a time; another window shows **Read-only** with **Take control**.
- **Files.** Browse and edit the files in the session's folder or worktree (see [Files](/User-Guide/Files)).
- **[Preview](/User-Guide/Glossary#preview).** The project's running web app beside the terminal, with a port picker, **Reload** and **Open in new tab**. On phones they stack vertically (see [Preview](/User-Guide/Preview)).
- **Changes.** The current branch's changes, edits and new files, as a diff per file. The tab's count and the row's "+40 −2 · 3 files" use the same comparison and refresh while it works. Switching branches changes the comparison too; the old session-launch commit is not kept as the base. Sessions sharing a folder see each other's changes; give each its own [worktree](/User-Guide/Sessions#worktrees) to keep them apart.
- **Pull request.** For a project with a GitHub repository: create, edit and merge the branch's pull request, and send failed checks or review comments to the agent (see [Pull requests](/User-Guide/Sessions#pull-requests)).
- **Artifacts.** Screenshots, short videos, test logs and reports the agent left for you (see [Artifacts](/User-Guide/Sessions#artifacts)). The tab appears once there are any, with their count.
- **Activity.** Every event Rynhult recorded, in words, newest first: *Session started*, *Using a tool: Bash*, *Turn finished*, *Now Needs you (was Working)*. Hover an event for the agent's own name for it; **guessed** marks one Rynhult inferred rather than heard from the agent's hooks. While loading it shows placeholders, not an empty feed. An initial failure shows an error; if a later refresh fails, a warning appears above the last received events. **No events yet** means a successful request returned no events.
- **Handoffs** (or **Findings** in a workspace). The handoff notes the session wrote, read from the folder it works in: its own [worktree](/User-Guide/Sessions#worktrees), the project's folder, or a team helper's worktree. A note shows here as soon as the agent saves it, before it is committed or merged. Select one to read it. After a worktree is cleaned up, the notes merged into the project's folder are shown. While Rynhult is reading, placeholders show; if the read fails, an error with **Retry** shows instead of "No handoff note yet", so a failure never looks like an agent that left nothing.
- **Setup.** How the session was set up, in words first: agent and model, permissions, [profile](/User-Guide/Glossary#profile), skills, MCP servers, folder, machine, the account it runs as, and when it started or resumed. Open **Exact inputs** for the original inputs, or **Generated files** for the file count and a searchable file chooser. One selected file is shown at a time, with Copy and keyboard scrolling.

A link to a tab the session doesn't have (**Team** on a session without helpers, **Pull request** without a GitHub repository) opens **Terminal**.

The output links count this session's handoff notes and available artifacts. **No handoff yet** and **No artifacts yet** mean a successful empty read; **Checking…** and **Unavailable** never claim zero. Select a link to open that output tab. On phones, selecting it also closes **Details**, keeping the output in view.

![Compact handoff and artifact links in the session header](/.attachments/sessions-deliverables.png)

![Phone Details shows the same output links without pushing down the terminal](/.attachments/sessions-deliverables-phone.png)

![The Handoffs tab listing a note the agent wrote in its worktree, not committed yet](/.attachments/session-handoffs.png)

![The activity tab](/.attachments/session-activity.png)

On phones, each event's time and source stack above its content. Long errors and paths wrap rather than being clipped.

![An Activity event on a phone with the complete error wrapped below its metadata](/.attachments/session-activity-phone.png)

### How Changes chooses its comparison

Rynhult uses the current branch's shared ancestor (**merge-base**) with origin/main or local main. If neither is available, it tries the locally recorded origin default branch, configured upstream, origin/master and master. Main takes priority over the feature branch's own upstream, so pushing the feature does not hide its changes. It never fetches or changes branches for this. Commits already on that comparison branch are not counted as new session work.

The comparison is named above the diff. If no shared upstream/default ancestor is available, **uncommitted changes** means only changes against the current HEAD. Before the first commit, new files count against an empty repository. A plain folder, including an untracked scratch folder inside another checkout, has no badge. A tracked subfolder shows only its own files, not unrelated parent changes.

![Changes names the current branch's merge-base comparison](/.attachments/session-changes-baseline.png)

![The same comparison and file diff on a phone](/.attachments/session-changes-baseline-phone.png)

Older [node](/User-Guide/Glossary#machine-node) brokers still return launch-based changes and are labelled that way. [Update the machine](/User-Guide/Nodes#updating-a-node) to use current-branch comparisons there.

### Open in editor
**Open in editor** (in the header of sessions on this machine) opens the session's folder, or its worktree, in VS Code over Remote-SSH: select **VS Code (Remote-SSH)**, or copy the `code --remote ssh-remote+<host> <folder>` command and run it on your computer. You need SSH access to the host with an account that can open the folder. The host is the dashboard's own name unless an administrator sets `ACC_SSH_HOST` ([Configuration](/Internal/Configuration)); without either, the menu is hidden.

### Branch and pull request
The header shows the branch the session's folder is on (refreshed with **Changes**). For a project on GitHub, when that branch has an open pull request, the header links to it (**#42**) and shows the latest CI run on the branch: **CI success**, **CI failure** or **CI running**. It comes from the project's GitHub view, which refreshes every few minutes (or with **Refresh** on the project page). To create, update or merge it, use the [Pull request](/User-Guide/Sessions#pull-requests) tab.

### Pull requests
Use the **Pull request** tab to take a session's branch from "pushed" to "merged" without leaving Rynhult. It appears on sessions of a project with a GitHub repository; give the session its own [worktree](/User-Guide/Sessions#worktrees) so it works on its own branch.

**Before you start:** the agent has pushed its branch to GitHub. You need to be an owner, or the member whose session and project it is. Rynhult acts on GitHub only when you click, as you: owners use Rynhult's GitHub token (the same one as the project's GitHub views); members use their [agent account](/User-Guide/Glossary#agent-account)'s own GitHub sign-in (**Providers → GitHub**).

1. Open the session and select **Pull request**.

The **Pull request readiness** summary appears before the description, including on phones. It shows the server's merge blockers and links to **Checks**, **Review comments**, and the **Merge decision**. The summary never enables a merge itself; the server's decision and exact-commit confirmation still apply.

![Pull-request readiness before a long description, with failing and running checks and unresolved review comments](/.attachments/sessions-pull-readiness.png)

![The same blockers-first pull-request summary on a phone](/.attachments/sessions-pull-readiness-phone.png)

2. No pull request yet: check the **Title** and **Description**. Rynhult fills them in from the session's handoff note, or from the agent's last summary if there is no note. Edit them as you like, tick **Open as draft** if it isn't ready, and select **Create pull request**.
3. To change it later, select **Edit title and description**, then **Save**. **Ready for review** and **Back to draft** switch its draft state. To ask for reviews, type GitHub logins under **Reviewers** and select **Request**.
4. **Checks** lists every check on the latest commit; required ones are marked **required**. When some failed, select **Send failed checks to the agent**.
5. **Review comments** counts the unresolved review threads. Select **Send review comments to the agent** to pass them on.
6. **Merge** shows what still blocks a merge. Once nothing does, choose a method if there is more than one, select **Squash and merge** (or **Create a merge commit**, **Rebase and merge**) and confirm.

![A session's Pull request tab: the pull request, its checks with the failing required check, review comments and the Merge card listing what blocks it](/.attachments/sessions-pull-request.png)

**What is sent to the agent.** Both send buttons first show the message, and **Send to agent** types exactly that text into the agent's terminal as one message (the session must be running). For failed checks it contains each failed check's name, the step that failed and the end of that job's log; for review comments, each unresolved thread's file, line, author and text. Before you see it, Rynhult:
- redacts the session's [keys](/User-Guide/Keys) and anything that looks like a token, password or private key;
- strips timestamps and colour codes, shortens long lines and keeps only the end of each log, within the 4,000-character limit, and says how many items were left out;
- tells the agent that the content comes from CI logs or reviewers, is untrusted, and is to be evaluated rather than obeyed.

![The preview of failed checks for the agent, framed as untrusted CI output](/.attachments/sessions-pull-request-send.png)

**Merging stays with you.** Nothing merges automatically, and agents have no way to use this tab. **Merge** is enabled only when the pull request is ready for review, every required check has passed, it has no conflicts and the branch rules are satisfied. It offers only the methods the repository and its branch rules allow, and merges the exact commit you saw: if the agent pushed since, Rynhult asks you to look again. GitHub has the final say; if it refuses, its reason is shown and nothing changes. Rynhult never bypasses branch protection.

Every action is in the [audit log](/User-Guide/Audit): `pr.create`, `pr.update`, `pr.ready`, `pr.merge` and `pr.send_to_agent` (plus `session.message` with the text sent).

**Troubleshooting**
- *"… isn't on GitHub yet: ask the agent to push the branch"*: the branch exists only locally. Ask the agent to push, then select **Refresh**.
- *"This session works on main, the default branch"*: pull requests need their own branch. Start the session with **Own worktree**.
- *"GitHub refused (403/422) …"*: the GitHub sign-in used has no write access to the repository, or a reviewer isn't a collaborator. Members: check **Providers → GitHub**.
- *"broker unavailable"* or *"gh api: …"* (members): your agent account's GitHub sign-in is missing or expired; sign in again under **Providers**.
- *GitLab:* [issue sources](/User-Guide/Projects#connect-gitlab-or-linear) can browse issues and merge requests and attach their context at launch. Creating, updating or merging GitLab merge requests from this tab isn't supported yet.

### Review feedback to the agent
In **Changes**, select any line of the diff to comment on it, or its **+** button in the margin. With the keyboard, Tab to a file's diff, pick a line with ↑ and ↓, and press Enter. Comments collect above the diff; while the session is running, **Send to agent** types them into its prompt as one message (`Review feedback on your changes:` then one `- path:line` `` `code` `` `— comment` per line) and submits it, as if you had typed it in the terminal. The CLIs submit on Enter, so the message arrives on a single line. A message is at most 4,000 characters; you can send to your own sessions (owners: any), and every confirmed message is recorded in the [audit log](/User-Guide/Audit) as `session.message`.

Rynhult checks the input after Enter and retries Enter at most twice if the message or a paste placeholder is still there. This includes Codex's `[Pasted Content ...]` placeholder for long messages. The message API reports success only after a new provider hook confirms submission, even if the agent finishes its turn immediately.

If you see **message typed but not submitted**, open the session's **Terminal** before sending again. The text may still be in the input box; press Enter there to submit it. If the input is empty, inspect the conversation first: acceptance could not be confirmed, so resending might duplicate the message. Only confirmed submissions produce a `session.message` audit entry.

### Explain these changes

Use a generated walkthrough to find a sensible reading order through a large diff.
It is a reading aid, not a review or an approval.

1. Open your session's **Changes** tab and select **Explain these changes**.
   This opens the panel; it does not run an agent.
2. Choose **Explanation agent** from the CLIs you are signed in to on this
   machine. The session's provider is selected when available. Set
   **Explanation model** to a model available to that CLI account, then select
   **Generate walkthrough**.
3. Read the steps beside the diff. **Next step** and **Previous step** move
   through the reading order. Select a file/line link to open that file and
   highlight the exact old or new lines.

![A generated walkthrough beside the diff, with agent, model, time and exact line links](/.attachments/sessions-explain-changes.png)

On a phone, the walkthrough stacks above the diff:

![The walkthrough on a phone, with step navigation above the changed file](/.attachments/sessions-explain-changes-phone.png)

The panel says **generated; check the diff**, names the selected agent and model,
and shows the generation time. **Helper session** opens its entry in Sessions;
its **Artifacts** tab contains `walkthrough.json`. Helpers run as **your** agent
account, including when an owner explains another person's session. They use
your native CLI plan and existing launch budgets. Members can explain only
their own sessions; owners can explain any.

The helper sees only Rynhult's already-redacted, size-limited diff. It runs outside
the source folder, with read-only permissions, no tools, no project keys,
no MCP servers and no inherited project instructions. It stops after one
response, failure or a three-minute limit, and never resumes automatically.
Very large diffs are reduced to per-file summaries and excerpts first;
the panel warns when input was reduced or the source diff was truncated.

Reopening an unchanged revision uses the saved result without spending usage.
**Regenerate** opens the agent/model form for another explicit run. A changed
revision needs a new walkthrough; Rynhult never generates one automatically.

**Troubleshooting:** if no agent is available, sign in under
[Providers](/User-Guide/Host-and-Providers) and reopen the panel. An unknown model or an
unsupported CLI flag produces a helper error, not wider permissions. If a
budget pauses the launch, adjust your budget or wait for its window to reset.
Invalid or oversized JSON produces an error; file/line references outside the
displayed diff are discarded. Refresh **Changes** if the panel says the diff
changed, then generate again. Rynhult currently links to pull requests rather than
showing a separate PR diff; a session reviewing a PR uses this same Changes tab.

### Artifacts
The **Artifacts** tab shows screenshots, short recordings, test logs and reports the agent left for you, refreshed while it works. **No artifacts yet** is an empty result, not a failure. **Where do artifacts come from?** expands the folder convention: agents save them in `.acc/artifacts/` at the top of their working folder. The [acc-workflow](/User-Guide/Catalog) skill asks them to.

![The Artifacts tab with a readable fictional test log](/.attachments/session-artifacts.png)

- **Images** (PNG, JPEG, GIF, WebP, up to 10 MB) show as thumbnails; select one, or press Enter on it, to see it larger.
- **Videos** (MP4, WebM, up to 20 MB) play in place.
- **Text** (`.txt`, `.log`, `.md`, `.json`, `.csv`, `.xml`, and `.html` shown as plain text; up to 2 MB) shows in a scrolling box. **Download** saves any file.
- Files directly in the folder and in one level of subfolders are listed, up to 200. Other kinds (SVG included), links and anything larger are left out or marked too large to show; Rynhult never follows a link out of the folder and never shows a page as a web page.
- The same people who can see the session's **Changes** can see its artifacts: you for your sessions, owners for all. Sessions on another machine work the same way.
- The folder is part of the agent's working copy, but Rynhult lists `.acc/` in the repository's own `.git/info/exclude` (never committed) when a session starts, so artifacts don't show under **Changes** and aren't committed.

### Export
**Export** in the header downloads the session as one Markdown file, `<session name>-<date>.md`: its details (project, agent, model, created, status, branch, worktree), the conversation as Rynhult indexed it for [search](/User-Guide/Features#search) (what was asked and answered, without tool output), its handoffs (or a workspace's findings), and the exact launch command. A conversation shows up shortly after the session has done something; until then the file says it isn't indexed yet. Conversation indexing waits for current database work to finish, so using search or exporting a session can briefly show the previous indexed conversation while an update is pending.

## The terminal
The **Terminal** tab embeds the session's tmux session in the page. It is the real thing, not a transcript: Rynhult's broker runs `tmux attach` for that session on the server and streams it to your browser, so you see exactly what you'd see over SSH, colours, spinners and permission dialogs included, and your keystrokes go straight to the agent.

- **Typing.** Click into the terminal and type. Answer a permission prompt with its number or the arrow keys and Enter; press Esc to cancel, as in a local terminal.
- **Focus while loading.** On desktop, if you focus a tab or another control while the terminal loads, it keeps focus when the terminal appears.
- **One writer.** Only one window types at a time. Opening the session somewhere else shows **Read-only · another window has control** with **Take control**, which moves typing to that window.
- **Phone.** Opening a terminal does not focus its text input or open the phone keyboard. Tap the grid to type. A key bar under the terminal adds the keys a touch keyboard lacks. The ones that finish or stop an answer come first and always fit the screen: **⏎** (Enter), **Esc**, **^C** (interrupt), **↑**, **↓** and **Tab**. **More keys** adds **Ctrl** (tap it, then a letter; it stays highlighted until used), **←**, **→**, PgUp/PgDn and **Paste**. To answer a permission prompt: tap **↑**/**↓** to pick an option, then **⏎**.
- **Keyboard.** Inside the terminal, **Tab** and **Shift+Tab** go to the agent for completion and other CLI actions. Press **F6** to leave for **Screen reader output**, then use **Tab** normally to reach **Open as text** and **Copy recent output**. **Shift+F6** leaves backwards, including from a screen reader row. On keyboards with media keys, you may need **Fn+F6**. A visible hint sits in the top bar. The **Skip terminal** link appears when reached by keyboard, before the grid, and goes straight to the output controls.
- **Reconnecting.** After waking the browser or losing its connection, use **Reconnect** if the terminal says **Disconnected**. Check the current prompt before continuing: reconnecting does not replay previously typed or pasted text, and input entered while disconnected is not queued. Accented characters and IME compositions are committed once, without reinserting earlier text.
- **Screen readers.** **Screen reader output** is on by default and exposes the terminal's visible lines. Toggle it below the grid; the choice is saved for this browser and applies when you next open a session. Grid tiles do not expose these rows; open the session for accessible output. Rynhult announces state changes such as **Needs you** or a terminal turning read-only once, without reading the agent's output stream aloud.
- **Text and copying.** Choose **Open as text** for selectable, read-only text with ordinary keyboard navigation. It holds a snapshot while you read; **Refresh output** fetches a new one. **Copy recent output** copies the snapshot in that dialog, or fetches recent output when used below the grid. If clipboard access is denied, select and copy from the text view instead. These controls are available on phones too.
- **Size.** The terminal follows the size of the window; tmux redraws the agent's screen to fit.
- **Scrolling and links.** Scroll the output available in the grid with the mouse or trackpad. Use **Open as text** for retained terminal history beyond the grid. Links open in a new tab.
- **Leaving.** Closing the tab, signing out or losing the network only detaches the view. The session keeps running in tmux on the server (**Closing this view never stops the session**).
- **Security.** Signing out, or ending a sign-in from **Settings**, closes its terminals at once. Members can only open their own sessions' terminals, and every attach is recorded in the [audit log](/User-Guide/Audit).
- **Access changes.** Demoting an owner immediately closes their views into other people's sessions. Rynhult also rechecks the current role and session ownership every 30 seconds.
- **Connection failures.** An unavailable machine does not reserve terminal control: reconnect normally when it returns. A slow browser pauses terminal output reads rather than growing an unbounded queue; detaching never stops the agent session.

Focused regressions cover terminal input, composition and polling isolation in
Chromium, Firefox and Linux WebKit. Physical macOS/Safari predictions,
VoiceOver and sleep/wake have not had a full qualification pass. These checks do
not prove every source of typing latency is fixed. If earlier text appears again,
check it before submitting and report the browser, input method and steps.

### Messages sent to the agent

**Message delivery** on the session page lists messages that are queued, blocked
or awaiting submission confirmation. A queued receipt is not proof that the
agent received the message.

An open writable terminal holds typing control even if nobody is typing. Switch
to **Activity** and close other terminal windows to release it. Queued messages
retry automatically when the agent can accept input; **Retry delivery** checks
the same message again without adding a copy. Permission questions still need
your answer in the terminal.

If a message may already have been submitted, Rynhult does not paste it again
automatically. Check the terminal first, then enter the full text and select
**Send again after checking** if needed. Review comments in **Changes** stay
until their delivery is confirmed. See
[team delivery details](/User-Guide/Orchestrator#queued-messages-and-delivery-confirmation).

### Reading recent output as text

1. Leave the grid with **F6**, or use **Skip terminal**, then choose **Open as text**.

   ![Recent terminal output in a selectable text view with Refresh output and Copy recent output](/.attachments/terminal-output-text.png)

2. Read or select the text. New output does not move it. Choose **Refresh output** when you want an updated snapshot, or **Copy recent output** to copy exactly this snapshot.
3. Close with **Esc** or **Close**; focus returns to **Open as text**.

The snapshot includes the current screen and up to 10,000 rows of retained terminal history, limited to the most recent 200,000 characters. Colours and terminal control sequences are removed, and wrapped lines are joined. It can include output from before you opened the page, but is not a complete conversation recording: erased screens and output outside the retained history are unavailable. The dialog says when the character limit cuts off older text.

**Keys and the audit log.** Values of [keys](/User-Guide/Keys) given to the session are replaced with «key: NAME» in the snapshot, as in transcripts and exports. The live terminal and the browser-held fallback below show the raw output, so other secrets the agent prints can still appear. Each snapshot is recorded in the [audit log](/User-Guide/Audit) as `terminal.read` (never the text itself). You can take up to 20 snapshots a minute; beyond that the view says **Too many snapshots in the last minute** and shows only the output held in this browser.

If the session's machine or terminal cannot be reached, the view says **Scrollback unavailable** and offers only the output still held in this browser. Older lines may be missing; try **Refresh output** when the connection returns. Reading or copying does not take typing control or send anything to the agent.

## Statuses
| Status | Meaning |
|---|---|
| Working | The agent is busy (reported by its hooks) |
| Needs you | The agent asked a question or wants permission; answer it in the terminal |
| Idle | Finished its turn; waiting for your next message |
| Recovering | The process ended unexpectedly; Rynhult is resuming the same conversation |
| Recovery needed | Resuming failed three times; check **Activity**, then select **Resume** |
| Stopped / Exited | Not running. The conversation is kept |

Rynhult never starts a fresh conversation in place of one it couldn't resume.
If the CLI closes its terminal while shutting down, Rynhult waits for its exit result before retrying, so **Activity** retains each failed attempt's status or signal.

## Actions
- **Interrupt** (the hand icon) sends Esc to the agent, the same as pressing Esc in the terminal. Use it to cancel a turn or a pending permission request. On a touch screen it is in the **⋯** menu, with **Stop**.
- **Stop** ends the process and keeps the conversation. Rynhult asks first.
- **Resume** restarts it in the same conversation.
- **Archive** (stopped sessions) hides it from the lists. The transcript stays on disk.
- **Remote** opens the conversation in Claude Remote Control (claude.ai or the Claude app), when it was started with Remote Control on.

## Observe-only sessions
Sessions started outside Rynhult (for example an administrator's own session on the host) can be registered as **observe-only**. Rynhult shows their status and activity but never types into them; their page offers **Open in Remote Control** instead of a terminal. In the sidebar they open Remote Control directly (marked ↗).

## Teams

An optional [Orchestrator](/User-Guide/Orchestrator) lead groups helpers beneath their parents in Sessions and the sidebar. Every member keeps its own status. The lead’s **Team** tab shows shared limits, branches and commits, and its decision log. **Stop team** stops every member; the ordinary **Stop** action stops just that session. Permission prompts from helpers remain yours to answer in Inbox.

## Goals and automatic continuation

Select **Goal and auto-resume** in a session's header (on a phone, under **Details**). The same two settings are in New session under **Advanced**, with the same name. These settings start off and belong to the session's person; owners can edit them too.

![The Goal and auto-resume dialog](/.attachments/sessions-automation-settings.png)

Once something is set, a line above the tabs shows it: the goal's status, continuations and hours, **Stop goal** and **Edit**. Nothing shows while both are off.

![The goal status line with Stop goal and Edit](/.attachments/sessions-automation.png)

1. Enable **Resume when the limit resets** and select **Save**. After a detected plan limit, Rynhult shows **Waiting for the limit to reset at** and the local time. Usage-window reset data takes precedence over the CLI's printed time. Rynhult sends one continuation at that reset, including after a Rynhult restart. If you type while waiting, that reset's continuation is cancelled.
2. Optionally write **Goal text**, **Maximum continuations**, and **Maximum hours**. **Maximum plan usage (%)** stops at a plan-window percentage, rather than measuring tokens spent on this goal. When usage cannot be read, that cap waits for data before allowing continuation. Select **Save**. Editing caps preserves the counter and original start time.
3. Watch the goal status, elapsed hours and continuations. The agent is instructed to report a standalone `GOAL: done` or `GOAL: blocked: <why>` line. Rynhult reads assistant messages; no second model judges completion. Select **Stop goal** to end the loop. If that request fails, the error appears below the status line; check the goal status and retry when the controller is available.

Typing into the terminal, sending a message, or selecting **Interrupt** stops the goal. Permission prompts stop it too; budget pauses prevent automated continuations and show the reason. A stopped goal prevents pending goal-related reset continuations. Caps bound further prompts; they do not interrupt a turn already in progress. Saving a goal on an idle session can start its first continuation immediately.

Claude, Codex and Copilot have plan-limit signals. Cursor, Antigravity and OpenCode currently disable auto-resume with an explanation; goals use their existing idle detection. These providers' idle fallback may take 90 seconds (OpenCode: 120). Provider preview status still applies. Model-endpoint and externally managed sessions do not support automation. Usage readings currently come from the default agent account; use CLI reset times on other accounts.

If a continuation says **claimed** or **delivery uncertain**, inspect the terminal before sending your own message. Rynhult deliberately does not retry an uncertain submission, to avoid duplicate instructions. Changes and continuations appear in Audit as `session.autoresume` and `session.goal.*`.

## Multi-run

Multi-run compares independent solutions to one task. It is not an Orchestrator
team: each run works on the whole task, and you choose which one to keep.
Start one with [New session → Run on several agents](/User-Guide/New-Session#run-on-several-agents).

1. In **Sessions**, expand the **Multi-run** row to see its runs, or select the
   group's name to open the comparison page. Each run also keeps its ordinary
   individual session row. **All** includes groups whose runs were archived,
   so you can return to their comparisons.

![An expanded Multi-run row with its independently usable sessions](/.attachments/multirun-sessions.png)

2. Review the run cards: state, branch, changed-file count, additions/deletions,
   cost and context usage where the CLI reports them. Unknown usage is not
   treated as zero. Snapshot/source times say when the numbers were observed.
   CI shows the project's latest cached result matching that run's branch,
   if it has been pushed and Rynhult has a permitted GitHub view. A missing result
   is not a successful check.
3. Under **Compare Changes**, choose any two different runs using **Left run**
   and **Right run**. Their diffs line up file by file, each against its own
   branch's [comparison base](#how-changes-chooses-its-comparison); this is not a merge or a branch-to-branch patch. A file
   unchanged on one side is labelled accordingly. Binary files and truncated
   output are identified rather than silently omitted.

![Two independent solutions with per-file Changes beside each other](/.attachments/multirun-compare.png)

On a phone, the two sides stack:

![Multi-run file comparison stacked on a phone](/.attachments/multirun-compare-phone.png)

4. Select **Open Changes** on a run to inspect its full session, comment to its
   agent, or explicitly use **Explain these changes**. Opening the group or
   comparison never starts a paid helper.
5. Select **Keep this one** on the preferred run. Its card is marked **Winner**;
   this alone stops nothing. To stop the rest, select **Stop and archive others**
   and confirm the dialog. You may select a different winner later.

Archiving never merges, pushes or deletes branches. Multi-run worktrees also
remain for later comparisons. After saving any work you need, you can remove a
worktree with the removal command under [Worktrees](#worktrees); branch deletion is a separate,
explicit git action.

**Troubleshooting:** launch refusals appear on their own cards and do not cancel
other runs. Fewer than two accepted runs cannot be compared. A failed compare
request shows an error (or a stale-data warning), not an empty diff. If stopping
a machine is not confirmed, the group reports it and Rynhult retains the stop intent
for reconciliation; check the run before treating it as stopped. Members can
read and keep only their own runs; owners can manage any.

If a broker action reports **delivery uncertain**, the connection failed after the
request may have arrived. Rynhult does not send that action again automatically. Check
the terminal and session state before deciding whether to repeat it.

You can keep editing in **Goal and auto-resume** while a save is in progress.
When that save finishes, newer edits stay in the fields; select **Save** again to
apply them.


## Sessions in containers

**Preview.** Containers are a Preview in this release: off by default, switched on per project by an owner, and
only on a host that has passed the container qualification. Every launch, including resuming an old or restored session, checks the host record again. Existing running sessions keep running. Helpers cannot fall back to running outside the container; unsupported helper launches are refused. Refusals appear in Audit. Full support follows once a second host has qualified.

**What it's for:** an owner can run a project's sessions each in its own container, with limits on CPU, memory,
processes, disk and how many run at once. It is off unless an owner turns containers on for this Rynhult and for the
project. Other projects' sessions start exactly as before. Installing or updating the container launcher alone
does not enable containers: the Rynhult wide setting and the project's switch must both be on.

**Before you start:** containers are still being qualified on each machine. Keep them off outside an owner
qualification window. The sign-in and HTTPS adapters are built but **off and not qualified**.
No dashboard setting enables them; a passed host qualification alone does not qualify them.

A reviewed candidate image can receive a narrowed, read-only copy of the selected provider
sign-in from your enrolled agent account. It never mounts your home or the owner's home. Claude receives only its
inference access token; Codex currently requires an API key; Copilot supports only the selected GitHub.com account.
Unsupported or expired sign-ins refuse launch. Renew the sign-in outside the container, then recreate the session;
refresh tokens and credential changes are never copied back. The copy still acts as you: the agent in the
container can read it, and with the HTTPS proxy it can use it from anywhere until it expires. A Copilot sign-in
is a GitHub token for your account, so with the proxy the agent can reach GitHub as you, outside the guard's git
rules. These adapters still need real-provider qualification.

Direct container networking stays disabled. In an explicitly approved candidate policy, HTTPS can use a filtered
session proxy that denies LAN, metadata and control-plane destinations. Local models, project services, HTTP and SSH
are not supported by this first slice. If the broker restarts, stop and recreate the container session to restore
its proxy. Container isolation, provider hooks and network behavior still need the owner's machine qualification.

The container is the selected sandbox: Codex and Claude retain permission prompts and mandatory guard hooks.

1. An owner opens the project's **Runtime** tab and, in the **Containers** card (marked **Preview**), ticks **On**. The
   card shows only while containers are on for this Rynhult (or, to switch it back, while the project still has them on).

   **On** is unavailable until this host has passed the container qualification. The card then says why, for
   example that no passed qualification is recorded, and how to record one: run the qualification kit, then
   `sudo /srv/acc/app/.venv/bin/python -I -m acc.container_host_record record report.json` as root (`docs/CONTAINERS.md`, "Release status").
   Switching a project off always works. If the host's qualification record later stops passing, new sessions in
   the project are refused with the same reason, and the audit log records it. Sessions already running keep
   running.

   ![The Containers card on a host that hasn't passed the qualification](/.attachments/projects-containers-unqualified.png)

   ![The Containers card in a project's Runtime tab, switched on](/.attachments/projects-containers.png)

2. Start a session in the project as usual with **New session**. It gets a checkout of its own (a separate copy of
   the repository at the project's latest commit, on its own `acc/…` branch) instead of a worktree, and runs in a
   container through Rynhult's container launcher.
3. In the session's **Setup** tab, **Folder** says **its own private checkout** and **Runs in** says **A container**.

**What you should see:** the session starts and behaves like any other: the terminal, Inbox, Changes and Files work
the same. When you archive it, its branch comes back into the project (a new branch, or a fast-forward), and the
checkout is removed once it has nothing uncommitted, like a worktree. For container checkouts, make commits and
push from the session terminal; dashboard Git writes are refused. Very large Git metadata or changed Git
administration may prevent Changes or archiving until reviewed. Running sessions keep how they started when
the switch changes.

Rapid edits that keep a file's length unchanged still count as uncommitted:
**Changes** shows them, and archiving keeps the checkout until they are committed
or discarded.

**Troubleshooting:**
- **launch failed: container launch refused: unpinned_image** — no image is pinned on this machine yet. An owner
  pins one (see the owner's qualification steps in `docs/CONTAINERS.md`).
- **account_leftover** — the launcher's own Podman check left a process of the agent account that it
  couldn't end. The launch is refused before the container starts (a cleanup is retried). Ask an owner to look
  at the audit entry `container.account_leftover`, which names the process by PID.
- **stale_podman_state** — Podman's `pause.pid` in the agent account's runtime folder couldn't be checked, so the
  launcher didn't run Podman. This happens when a link is on the way, or when the file isn't a regular file, isn't
  the account's own, or couldn't be removed. Ask an owner to look at the audit entry `container.stale_podman_state`.
  When the file only names a process that has gone, the launcher removes it by itself and records
  `container.pause_state_cleared`; the launch goes ahead.
- **untrusted_image_store** — the pinned image resolved outside the owner's protected image store,
  its files are writable, or its protected execution tree is missing or has a different image pin.
  Ask an owner to install both the reviewed image and its execution tree using the owner runbook,
  then rerun qualification. Launches use the owner's read-only tree, not another image lookup in
  your account. Loading an image into your agent account does not fix this refusal. When the detail reads
  **protected session image is unavailable: podman exited …**, the rest is Podman's own error, for the owner.
- **unsafe_runtime_config** — the owner's launcher storage profile is missing, writable or outdated.
  Ask an owner to install the reviewed storage profile and execution namespace guard. The guard
  and its parent directories must be protected, root-owned paths without directory symlinks.
- **start_failed** mentioning the protected user namespace — this runtime cannot establish the required
  isolation. Systemd 255 is supported by the owner-installed namespace bootstrap and guard;
  fresh owner qualification is required. Ask an owner to check the installed guard, user
  namespace support and delegated cgroups. Failed setup refuses launch. The launch failure in the
  audit log names the guard's reason, for example that the namespace isn't owned by root.
- **account_capacity** — the account already runs as many container sessions as its limit allows. Stop one, or an
  owner raises **Sessions at once** in [Settings](/User-Guide/Settings#container-limits).
- **containers are turned off on this Rynhult** — the project still has containers on, but the machine doesn't. An owner
  switches the project's **Containers** card off to run sessions as before. Rynhult never quietly runs it outside a
  container instead.
- **the checkout's git settings include …** — something changed the checkout's `.git/config` to run a program.
  Rynhult won't run git there (Changes, archiving) until that setting is removed; look at the session's work first.
- **cleanup_failed** — Rynhult could not verify cleanup, or found a leftover container. It keeps the account's slot
  reserved. Ask an owner to inspect the unit and container; Rynhult does not remove leftovers based on their labels.
- **disk_scan_failed** — the checkout changed during inspection or exceeded the scan budget. Rynhult stops the
  session; its work stays in the checkout for review.
- **container teams require local helpers only** — choose only **local** under **Machines**. Helpers and
  descendants each need a separate container. See [Orchestrator](/User-Guide/Orchestrator) for merge preparation.
- **unverified container sandbox** — the pinned image did not verify its launcher boundary. Ask the owner to
  qualify the installed launcher, image and protected root filesystem; there is no native fallback.

In **Setup**, the launch summary stays visible while **Exact inputs** and **Generated files (count)** start closed. Open **Exact inputs** to read or copy the original inputs.

![Setup shows launch choices with exact inputs and generated files collapsed](/.attachments/session-setup.png)

![The same Setup summary and disclosures on a phone](/.attachments/session-setup-phone.png)

Open **Generated files**, optionally type part of a filename in **Filter generated files**, then choose **Generated file**. The selected file appears in one named, keyboard-focusable pane: focus it and use the arrow keys or Page Down to scroll, or select its **Copy** button. Closing and reopening the disclosure keeps your filter and selection while you stay on Setup. **No matching files** means the filter excluded every file; clear it to see them again. **No generated files** means this session has none.

![Generated files offers a filename filter, chooser and one file pane with Copy](/.attachments/session-setup-files.png)

![The generated-file controls and pane stack on a phone](/.attachments/session-setup-files-phone.png)

Long text stays inside its own keyboard-focusable region. Copy actions confirm success only after the clipboard write completes. If copying fails, select and copy the displayed text manually using the contextual explanation.

Codex question and error detection also works when terminal lines wrap on a phone.
Opening a question keeps **Needs you** until you answer or skip it; a completed turn's error appears as **Idle** activity.

If you choose **Local workspace** while the catalog is loading, Rynhult selects **Researcher** when the profiles arrive. A compatible profile you deliberately selected is kept. Wait for the launch preview before starting.

## Instructions at every launch

Every provider receives Rynhult's mandatory core, including sessions without a profile or a team. It covers
scope, permissions, untrusted input, secrets, stops, evidence and reporting. Profiles and skills cannot widen
your grant. Project conventions remain in the project's instructions.

At launch and resume, the session also gets its actual role and current capabilities: gate availability,
number reservations and restacking, report/plan/brief tools, run mode, granted machines, image support,
and named profile items its provider cannot load. Unknown image support stays unknown; an agent must admit
when it cannot read an image. An unavailable gate is a verification limit, never a passing result.

Team leaf workers commit without merging, pushing or opening PRs. Coordinating helpers can merge only
stopped descendants through the team tool. The root lead prepares one consolidated PR; a person merges
to main. Read-only reviewers return findings through reports and do not edit code or handoff files.

Native subagents share their parent's slot and permissions. They are not shown in the team tree, and their
count cannot be capped. If a council cannot use managed helpers within the grant, its sequential views
are labelled **single-session perspectives**, rather than independent reviews.
