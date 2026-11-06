# New layout

> Screenshots use fictional demo accounts and disposable fixture data, not live systems or qualification evidence.

The new layout is the only layout for everyone signed in. Classic is retired, including for people who previously chose it. There is no layout selector in Settings and no owner feature switch. Your saved classic preference is retained for now but no longer changes the layout.

Choose the features you need in Settings, and use Setup level to adjust how much you see.

## Appearance

The new layout uses warm neutral surfaces and lavender accents in both Light and
Dark themes. The main content has a panel surface, with borders separating its
cards; in Dark, the sidebar is darker than the content. Light-mode links and
focus outlines use a stronger accent for readability. The theme still follows
your existing **Theme** choice. Security,
Unrestricted, offline and attention indicators keep their strong warning colours;
keyboard focus stays visible.

Overview, Sessions, Inbox, Settings, Host details and Catalog use compact page
titles and more consistent spacing. Files has flatter tree and editor containers.
The controls, warnings and actions still work as before; project-page and
recovery-screen refinements are separate follow-ups.

## Navigate while keeping your work

Every rail item opens its primary page directly: Overview, Inbox, Sessions, Projects, Workspaces, Hosts, Grid, Tools (Catalog), or Settings. In the desktop app, Tools opens the first available tool (Search) because Catalog is hidden. Useful sub-navigation remains in the context column, such as projects, host names and Inbox categories. Its title and summary share the top row with the collapse control; lists use compact spacing, with larger targets on touch devices. Overview and Grid hide that column. Retained Files drafts and terminal attachments stay in their working tabs when you navigate. Closing a dirty file asks before discarding it.

In **Projects**, selecting a project name opens its Overview and replaces the list with its project menu. **Work** contains Overview, Sessions with a count, Files and Changes. Changes lists links to that project’s session Changes views; opening it does not attach a terminal. **Project** shows any source repositories visible to you, with a link to the full Sources view, followed by Brief & knowledge, Wiki and Issues & PRs where available. **Project settings** opens that project's settings, including Security. Failed source reads show an unavailable message instead of old repository details. The menu stays beside that project’s sessions, including their Files and Changes tabs, and returns when you reload a session. Opening another project’s session changes the menu to match; Back to all projects clears the project context. **Working locations** shows the recorded primary folder, account and machine availability. Use **Back to all projects** to return to the searchable list. Pinning has its own button and does not navigate. Pins, recent selections, sidebar width, collapsed state and sidebar mode stay in this browser for this person and installation. They do not grant access or follow you to another device.

**Available projects** shows up to 20 projects at a time, ordered by project slug. Use **Previous projects** and **Next projects**, or search by name, slug or repository across the whole authorized list. Changing the search returns to the first page. On phones, the project list scrolls within Browse while its mode buttons remain reachable.

![Project menu and Changes index, light theme](/.attachments/new-layout-1440-light.png)

![Project menu and Changes index, dark theme](/.attachments/new-layout-1440-dark.png)

**Overview** summarizes sessions that need you and sessions working. **Tools** holds the remaining destinations, including Catalog, Providers, conversation Search and Guide; owners also have Audit and the full-screen Status board. Hosts opens the existing Host page. Settings keeps its existing sections.

On a phone, use the bottom bar or **More** to open the sidebar sheet. Closing the sheet returns you to the same working view. The terminal's touch key bar appears on touch devices; it stays hidden with a desktop pointer. Desktop **Terminal options** keeps **Paste text…** available when you control the terminal. Opening a page directly, reloading it, or using Back selects its section in the rail, including pages under Tools.

![New layout on a phone, dark theme](/.attachments/new-layout-390-dark.png)

![New layout on a phone, light theme](/.attachments/new-layout-390-light.png)

## Continue an existing session

In a project's **Overview**, **Sessions** lists its non-archived sessions, with those needing you first, followed by working, unknown, idle and recently stopped sessions. Each row keeps its exact state, provider, machine, branch or working folder, and last-seen age where available. Lists with more than 20 sessions have Previous sessions and Next sessions controls. The section tabs sit directly beneath the compact project title. Results has its own tab; Working locations contains sources and working folders. Project brief remains in its section and the sidebar.

**Continue** opens the same session without starting, resuming, stopping or typing into it. Opening a session’s Terminal tab requests control when the terminal is free and has no partial input. If another window controls it, the tab stays read-only. **Control terminal** claims a free terminal in one click; for a held terminal it opens the handover dialog, which requires a reason and confirmation. **Take control…** under Terminal options opens the same dialog. Choose Manual handover, Controller unavailable or Team intervention, then confirm. Partial input may remain in the terminal: inspect it before typing. Taking or releasing control never clears, copies or replays it. Only unacknowledged input and active composition must finish before handover; automated messages stay blocked by partial input. Moving a tab never takes control.

**Resume**, **Stop** and **New session** are separate actions. For stopped sessions in **Sessions**, Resume stays disabled while Rynhult checks whether that session can resume. If it cannot, the server's exact reason appears beside the disabled action; **Check again** reads its current eligibility without resuming it. A failed check says eligibility is unavailable.

An **older settings** badge is information, not an eligibility decision. Checks are briefly cached and refreshed while visible; clicking an enabled Resume still checks permission and current conditions again on the server. If that final request is refused, its explanation appears unchanged.

**Continue** is available even when the session's machine is offline; the detail page shows the existing offline state. In the new layout, an offline machine disables **Resume** with the machine reason. **Stop**, **Interrupt** and **Archive** keep their standard-layout conditions: the machine being offline does not disable them, but Rynhult itself being unreachable does. The server still decides whether a requested action can succeed.

If the project has no sessions, the empty state offers a new session for that project. The header names the project when starting a new project-context draft. An existing retained New session draft takes precedence when reopening it.

Saved working tabs show **Saved view** while access is checked. Their names appear as soon as the check succeeds, including tabs you have not opened yet. Refused references show **View unavailable**. Checking a hidden tab does not open its page or attach its terminal.

## Compact session controls

A session’s title, provider, state, **Interrupt** and **Stop** share one compact header line. On narrow panes they wrap to stay reachable. **Session details** contains the project, folder/worktree, branch, handoffs, artifacts, tmux name, conversation ID and copy button, permission mode, current activity, rename, Export, Open in editor and Remote actions where available. Keyboard users can focus Details and press Enter; Escape closes it and returns focus.

A waiting-delivery chip stays on that line. **Retry** checks delivery again; its **Details** button opens the existing Activity history. The Terminal tab row holds terminal control and **Open as text** icons with tooltips. **Terminal options** contains Copy selected text, Take control, Refresh control status, Detach terminal and Screen reader output when applicable. F6 still returns from the terminal to its working-view tabs. Moving controls does not change who controls a terminal or reconnect it.

The Inbox rail button shows the open count, hidden at zero and displayed as **99+** above 99. Its accessible name includes the full count. The new layout has no notices banner: machine, backup and ordinary notices remain in Inbox. If a count cannot refresh, the button’s accessible name reports that it is unavailable or out of date; open Inbox for the error and refresh controls.

## Recent results

In a project's **Overview**, **Recent results** shows the latest five indexed
handoffs, council reports and research findings from the project's checkout.
Each entry names its kind and **Updated** age. Times describe file updates,
not completed, merged or approved work. The list uses the existing Knowledge
index, which indexes up to 100 filenames in each notes folder; it is not a
complete activity history.

Select a note's filename to read it in the existing note viewer. A **Session**
link appears when the filename references exactly one session visible to you
in this project. **Session not recorded** means that no unique visible session
could be identified. Other people's private sessions are not looked up.
**View all** opens this project's **Knowledge** section, including older indexed
notes and other document categories.

**Refresh results** retries the index. **Nothing saved here yet** means the
index was read successfully and had no result notes. **Results unavailable**
means it could not be read; any retained entries are labelled as the last data
received. Private-worktree notes, artifacts, pull requests, structured reports
and previews are not part of this list.

![Recent results with source, update age and visible session attribution](/.attachments/new-layout-recent-results.png)

## Hosts and working locations

Select **Hosts** in the rail (on a phone, use **More → Hosts**) to open the Hosts overview directly. The context column lists machine names with a connection dot. Selecting a name opens that machine’s existing health, accounts and services page directly. The overview’s **Open host** link reaches the same details.

Owners see the main machine and enrolled nodes. Members see the same filtered
main-machine data as the existing Host page, plus their authorized project and
group checkout locations. This does not grant access to other machines or
accounts. Project links open the existing project; group checkout links open
that group's Projects view.

Each entry in the **Hosts overview** shows connection, observation or last-seen age, health readings and
active work visible to you. On the main machine, **Live psutil** CPU and memory
have their own measured age. **Collector snapshot** disk readings have the
collector's separate observation age; a stale collector warning can appear while
live CPU and memory are fresh. Older servers without a measurement time show
when the live response was received. Node readings name their periodic summary
and its observation age.

An offline machine shows **Unavailable** rather than Idle or zero usage.
**Refresh hosts** retries both main health and owner machine summaries. If a
session refresh fails independently, **Active work: unavailable** keeps dated
**Saved counts** with their own **Last received** age; the host's fresh readings
do not date those counts. Saved sessions are not evidence that an offline process
is still running.

**Working locations** lists project host/path records and authorized group
placements. These are recorded locations, not a live filesystem check. Files and
sessions are not moved, resumed or restarted by browsing here. The standard
layout keeps its existing **Host** page unchanged.
## Settings scopes

**Settings** uses the contextual sidebar for **Account**, **Project** and (for owners) **Installation** navigation. The selected editor starts below the page title. Account holds personal sign-in,
devices, notifications, keys and layout; **Provider sign-ins** opens Providers.
Choose Project, then **Settings project**, to open that project's existing Keys,
Issues & PRs, brief, backup, ports or Runtime sections where available. Installation
holds owner controls such as people, features, updates, integrations and policy.

These are links to the existing editors, not copies. **Security** opens the
same Security editor with the selected project or installation scope. Owners'
**Agent account Security** opens their main machine/agent-account scope.
Members can read their own project protections; an owner manages changes.
**Allowed folders** opens the existing account root-list editor. From a project,
it selects that project's machine and agent account; it is shared account access,
not a project-only folder policy.

The new layout omits the duplicate scope buttons, section chooser and in-page project view tabs. Use the sidebar for Settings sections and project Overview, Files, Changes and Preview. **Settings needing attention** is a compact disclosure: expand it for warning details and links. Bookmarks and old links, including `#all`, still work.
Changing scope within Settings keeps mounted form drafts, dialogs, validation and
attention notices. Links to a different page keep that page's existing unsaved
work protections.

If your role or agent account changes while you are signed in, project and
session lists recheck under the new authority. Earlier owner-only choices,
selected context and private session rows are not kept visible while that read
is loading or unavailable. Pins and recent-project IDs only resolve against the
current authorized list; they do not preserve access.

![Project scope links in Settings on desktop](/.attachments/new-layout-project-settings-1440.png)

![Project scope links on a phone](/.attachments/new-layout-project-settings-390.png)

## Header and keyboard

The header keeps connection status, update availability, and the owner's Security shield. The desktop header fits on one compact row. Owners see CPU and memory readings and compact plan summaries such as **Claude 23%**. Each provider shows its highest used window, without combining providers. Focus the **Plan usage details** button and press Enter to inspect every window with account, source and age; Escape closes the popover. Hover over the summary for window details. Machine reading age is available on its link. Missing readings stay unavailable; different providers' usage is never combined. Members do not receive owner-only readings.

The required **Security** shield opens the existing **Settings → Security** editor for the displayed account or project. Hover over it or focus it with Tab to read the full current state, scope and older-session count; Escape closes the tooltip without moving focus. Press Enter or click the shield to open the editor. Members do not see the shield or request the owner's policy inventory.

- A neutral checked shield means **Current protections On**.
- A solid amber warning shield means **Unrestricted**. Custom, pending or person-varying protections use an outlined warning shield; the tooltip explains which state applies.
- A small amber number marks sessions on older settings. Counts above 99 show **99+** on the shield; the tooltip and screen-reader name include the full count.
- A dashed question-mark shield means the security state is loading or unavailable. A crossed shield means Rynhult itself is offline. If only the project's or session's machine is offline, the shield keeps showing the current policy and the tooltip adds **machine offline**. Cached policy and counts are explicitly marked **last known**, so unavailable data does not look like confirmed protections.

Choose **Customize header** to show, hide or reorder Machine load, Plan usage, Go to…, Search conversations and Find project files. Use the checkboxes and each row's **Move up** / **Move down** buttons, then **Save header**. The buttons work with Tab and Enter; dragging is not required. **Reset to default** restores the initial order and visibility in the editor; save to apply it. Cancel leaves your saved header unchanged. A failed save keeps your choices open and says **Header not saved**.

The preference belongs to your sign-in and follows you to other browsers and devices. It changes the header immediately without reloading your working view. Machine load and Plan usage remain owner-only; their controls are marked **Owners only** for members. Hiding a module changes its presentation, not permissions or keyboard shortcuts.

On narrower windows and phones, **Header tools** opens an overflow menu containing your visible optional modules in the saved order, plus **Customize header**. Identity, account, New session, Security, Live/connection state and update notices retain their existing places and conditions. Open Inbox counts appear on the Inbox rail button. You cannot hide or reorder them through this preference. If header preferences cannot load, the default tools stay available with an error and **Retry header preferences**.

On short phone windows, the New session button shares the available header space with required controls, including while Security is loading. Long project names shorten visually inside the button; the full launch label remains available to assistive technology.

![Keyboard-accessible header customization, light theme](/.attachments/shell-header-owner-1440-light-editor.png)

![Header tools overflow on a phone, dark theme](/.attachments/shell-header-owner-390-dark.png)

- **⌘K / Ctrl-K** opens the existing navigation palette; **C** opens New session.
- **⌘⇧F / Ctrl-Shift-F** opens **Find project files**. Choose the project, then enter a filename; arrows move through results and **Enter** opens the file. **Escape** closes search and returns focus.
- Unmodified arrow keys move between rail buttons; **Enter** opens their primary page. Modified shortcuts and input composition keep their normal behavior.
- **F6** cycles header, rail, sidebar and content; **Shift+F6** moves backwards. On phones it cycles header, bottom bar and content. In a working terminal, F6 returns to its view tab.
- Drag the sidebar’s outer edge to resize it between 208 and 400 pixels. Focus **Sidebar width** and use Left/Right in 8-pixel steps, or Home/End for the limits. Double-click the edge to reset to 256 pixels. **Collapse sidebar** and **Expand sidebar** keep their state across reloads on this browser; F6 skips the collapsed sidebar. Selecting a desktop rail item expands it again.
- If a narrower window hides the control you were using in the rail or sidebar, focus moves to the matching bottom-bar control or **More**. Widening the window closes Browse and returns navigation focus to the desktop rail.
- Project search and paging announce changed result counts through the shared status announcer. Unchanged counts are not repeatedly announced.

## Find a project file

**Find project files** in the header searches filenames and relative paths, never file contents. **Go to…** still opens the navigation palette; **Search conversations** searches conversation content.

The search names its project, machine, agent account and **primary project folder**. It does not search a session's separate worktree. On first opening, it uses the project in your working view, if there is one; otherwise choose a project. Browsing a different project in the sidebar does not change the target. Your choice stays as you open other views within the layout or close and reopen search. Change it explicitly with **Search project**. Reloading or leaving the layout starts fresh.

Results open in the existing [Files](/User-Guide/Files) view. An already open file is focused again; other file drafts stay open. Members can search their own projects; owners can search across people, using the same access rules as Files.

Search waits briefly after typing and shows up to 20 results. **Searching filenames…** means the request is still running. **Partial results** means the search reached a limit or could not read part of the folder; even an empty partial result does not prove the file is absent. Refine the filename to narrow the search. Ignored folders, secret paths and symbolic links are excluded by the file search service.

If the project's machine is offline or unavailable, search says so and does not search another machine instead. If your browser is offline, **Search paused** waits for reconnection. Correct an invalid query or use **Try search again** after a connection or service error. If search is busy for the account or another search is already running, wait briefly and use **Try search again**. **No filenames match** applies only to a completed search without a partial-results warning.

![Project filename search, light theme](/.attachments/shell-file-search-owner-1440-light.png)

![Project filename search on a phone, dark theme](/.attachments/shell-file-search-member-390-dark.png)

## Review your Inbox

Choose **Inbox** in the rail to open its main page. The sidebar
shows category filters and counts for **Decisions**, **Approvals**, **Blocked work**, **Failures** and
**Information**. These counts describe the open notices returned for you, up to
200 per read, rather than an installation-wide total. Owners do not see other
people's private Inbox items.

Open a category to review its list and selected notice. The context names the
project, session, source and execution host when available. **Open related work**
uses that exact session or the existing relevant settings or Host view. Decisions
and approvals use the original operation and its current permissions, just as
elsewhere in Rynhult. Similar titles remain separate notices.

**Dismiss notice**, **Acknowledge** and **Snooze 1 hour** do not approve anything
or answer a session. A pending stop remains pending until its machine confirms
it. If a decision has changed or needs another sign-in verification, its existing
review controls show the server's explanation; dismissing it cannot bypass that
review. **History** includes handled notices; **Refresh Inbox** reads their
current state. Failed reads show an error, or mark previously received data as
stale, rather than showing an empty Inbox.

When you open the Inbox from another working view's sidebar, **Return to work**
returns to its address. Working columns retain open terminals and file drafts;
closing dirty work still asks before discarding it.
The phone uses the same categories in the browsing sheet and stacks the list
above the selected notice. Long lists have Previous items and Next items controls.

## Simpler navigation

[Setup levels](/User-Guide/Setup-Levels) let you choose the rail and Tools entries
you normally see. **Tools → More tools** reveals other authorized areas. Saved
links, Security and recovery remain reachable; simplifying never stops work.

## Current limits

Shared workspace layouts remain a follow-up. See [Files](/User-Guide/Files), [Sessions](/User-Guide/Sessions)
and [Keyboard shortcuts](/User-Guide/Keyboard-Shortcuts) for the existing controls.

## Recovery views

An offline machine is marked **Offline** in a project's Continue working panel and in session views. **Last seen** comes from the owner's machine inventory when available. If this view cannot read a check-in time, it says **not reported for this view**; session activity is not used as a substitute. Only Resume is disabled by the machine-offline status. Stop, Interrupt and Archive keep their usual conditions and server checks. Navigation remains available. The card uses the inventory label (or the known host name), and does not repeat the Terminal offline placeholder.

Chat is off by default in Admin features. When enabled, a linked Chat view shows the adapter's actual state: unsupported, unavailable, offline or busy never becomes an empty successful history. An unsupported view stays in Chat until you choose **Open Terminal**. It opens no terminal connection automatically. Explicitly opening Terminal requests free control, including from Chat. It never takes control from another window automatically. Terminals shown only by restoring an arrangement remain read-only.

A missing or unauthorized linked session or file view shows **This saved view can’t be opened**, without its private details. A valid session with an unavailable tab uses neutral wording and an **Open Terminal** link instead of suggesting that your access changed. **Recheck availability** only reads availability again. **Back to sessions** leaves a missing-resource placeholder. Working columns use the same recovery panel. A denied Files refresh also hides the filename in its working tab; a read-only recheck can restore access without losing the existing draft.

When a file changes on disk while you edit, **Resolve a save conflict** keeps your draft and shows its base revision and the current disk revision. The file service does not supply an observation time, so the view says so. **Review changes** shows the disk snapshot beside your editable draft; **Keep editing** hides the snapshot. Neither discards anything, adopts a revision or saves. Both revision choices are disabled until **Review changes** displays that disk snapshot. Hiding the snapshot with **Keep editing**, or detecting another conflict, requires review again. Saving stays paused until you explicitly choose **Keep draft and use current revision**, or confirm **Use disk version**. Saving after adopting the current revision still checks for further concurrent changes.
### Consistent page surfaces

In the new layout, Settings section panels (including Passkeys and Access devices),
Providers, the Hosts overview and the categorized Inbox use the same compact titles
and flat panel edges. Settings identifies the current Account, Project or Installation
scope; Providers identifies the selected machine and agent account. Other titles omit
a breadcrumb when there is no useful context to add. This is a presentation change:
permissions, warnings, forms, drafts and actions keep their existing behavior.

## Arrange your working views

Session terminals, Chat and Files open as tabs in the working area. The controls at the end of the tab row act on the active tab: **Fill working panel**, **Move view to column**, **Reset columns** and **Close view**. After a reload, tabs for sessions and projects you can list keep their names; a file tab reads **Saved view** until you open it. Select **Move view to column** to place the focused tab in one of up to three columns. A new column is available only when each view fits its minimum width. Drag a tab onto another column or before another tab to move it. Closing the last tab leaves an empty column with browsing links; **Remove empty column** removes that space.

With a view tab focused, **Left/Right**, **Home** and **End** select tabs. **Ctrl-Shift-Left/Right** moves the tab to a neighboring column. **Focus column** selects another column. Drag a divider to resize, or focus it and use **Left/Right** (hold Shift for larger steps), **Home** or **End**. **Reset columns** gathers your tabs into one column without closing their resources.

**Fill working panel** gives the active view the working area. **Restore columns** returns the preceding arrangement. Both keep the same terminal or editor. A move requested while you are composing text waits until composition ends.

A phone or narrow window shows one column with all your tabs. Its **Focus column** control and tab strip keep every view reachable. Widening the window restores the saved desktop placements. Resizing, moving and filling do not add browser history entries. Back/Forward selects the referenced view.

Column arrangements follow your sign-in. They are saved for each project or private workspace, separately for desktop, tablet and phone and for wide, medium and narrow windows, and restore in another window or browser of the same kind. Files are saved by a private reference, never by name, and are checked against your current access each time they restore. A tab reads **Saved view** until you select it and your access is checked. Anything you can no longer open appears as **View unavailable**, never its contents. Restoring a layout never starts, resumes or stops a session, and a restored terminal stays read-only until you explicitly open its session tab or choose **Control terminal**. Columns in a group project, or for a session without a project, last for this visit, and the working area says so.

If two windows change the same arrangement, nothing is overwritten silently. The second window shows **This layout changed in another window** and keeps working. Choose **Use this window's layout** to save yours, or **Load the newer one** to take the other arrangement. Loading it keeps any view holding an unsaved draft or terminal control in this window.

When the server can't be reached, the working area says **Offline** and keeps your change on this device, or only in this window if browser storage is unavailable. It is sent when the server returns, unless the saved layout changed meanwhile, in which case you get the same choice. A file opened while offline joins the saved layout at the next save after you reconnect.

**Reset columns** returns your open views to one column and clears the saved arrangement for this project and window size. It closes nothing. A file's saved reference is removed once no saved layout uses it, and Reset removes all of yours for the project, so files saved for another window size restore as **View unavailable**. Signing out removes any arrangement still waiting on this device.

Saved layouts contain references and sizes, not file drafts, terminal output or control grants. Direct links to archived sessions still open their history; an archived session restored only from a saved layout stays unavailable. Workspace Files links without a selected session explain how to choose a folder. Unavailable views can be closed; they never become another resource with a similar name.

Within the working columns, up to 32 view references, eight attached terminals and eight file editors can remain open in this visit. Opening another terminal detaches the least recently used hidden terminal and reconnects it when you return. When all eight attachments are protected by visibility, composition or unresolved input, opening another terminal allows one extra attachment. The oldest protected terminal detaches once it is hidden and its input is resolved. Further views wait quietly for capacity; attachments never exceed nine. Other view limits ask you to close or detach something first. **Detach terminal** releases an attachment; **Recheck terminal** creates a new read-only connection. Detaching warns about unresolved input: it cannot recall bytes already sent, and reconnecting never replays them. A clean hidden read-only terminal can detach after ten minutes; **Recheck terminal** explicitly reconnects it. Hidden terminals automatically release control when they have no pending or partial input or composition. This includes switching views, hiding a column and leaving the browser tab hidden for 1.5 seconds. Partial input stays with its controller. A compact toolbar chip offers **Show** when another tab preserves unsent text. Other controllers appear in one inline toolbar chip; hidden terminals add no banner. Returning to an explicitly opened Terminal or restarting its run automatically rechecks and can claim free control; it never automatically takes control from another writer.

**Open as text** shows the locally buffered terminal output for selection and copying. **Copy selected text** uses your terminal selection, with a selectable fallback if clipboard access fails. **More terminal keys** includes arrows, Page Up/Down, clear-line and **Paste text…**; review pasted newlines because they can submit input.

**Close view** never stops a session. A dirty file asks before discarding; a pending save or composition must finish first. File drafts remain only in memory until **Save** succeeds. Save or copy them before reloading. The [Chat](/User-Guide/Chat) tab is read-only. In this layout, **Open Chat beside terminal** and **Open terminal beside Chat** use the working columns and focus existing views when available.

Grid keeps its existing terminal panes and separate limits in this slice. Shared workspace layouts remain later work.

## Start a session in the workspace

In the new layout, **New session** opens a working page inside the workspace.
Start with the task, then check the agent/profile and run choice. **With helpers**
uses the existing team grant; choosing it opens **Permissions and limits** for
review. Provider defaults and the project's permissions still apply.

**Name** appears with Task and Agent in the main form. Keep the automatic suggestion or choose **Rename**.

Three disclosures hold the existing launch controls:

- **Model and working location**: project or local workspace, machine, checkout,
  model and effort. The target summary shows the selected host, agent account
  and recorded checkout. A new session's worktree is created only at launch.
- **Permissions and limits**: provider permission modes and helper grants.
  Explicit consent never overrides Security settings.
- **More options**: goals, skills/tools and the launch-command review.

**Back to workspace** and browser Back keep the draft in this tab. Reopen
**New session** to continue it, or use **Discard draft** to clear it. Existing
reload and sign-in-boundary protections still apply; do not assume a draft held
in memory is saved to disk.
Opening New session and returning to the workspace also keeps unsaved file edits;
leaving the file for another page still asks before discarding them.

If the selected machine is offline, the task and target stay in place alongside
the refusal. Owners also see the last-seen age; members see an unknown age.
An unknown age or unchecked checkout is shown as unknown. **Check connection**
refreshes readiness; reconnecting never starts the draft. **Choose another location** opens and focuses the machine selector. Only
an explicit selection changes the target, and any required clone or permission
review still applies.

If launch is not confirmed because the connection failed, the draft stays and
Rynhult asks you to check Sessions before trying again: a session may already
have started. A retained draft is not proof that a launch failed.

Session tabs keep less frequent views (**Setup**, **Activity** and **Handoffs**) under **More** in the new layout. Choosing one keeps its selected tab visible; direct links still open it. In multiple working columns, session headers use the same compact pane spacing as project views.

The terminal fills the remaining height of its working pane. Short windows keep the terminal's minimum height and let you scroll the page to reach its controls.
