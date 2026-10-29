# Groups

Groups now have authority controls and **private same-host Git exchange**. Owners can create groups and
appoint maintainers; maintainers can manage existing Rynhult users and register group
[project](/User-Guide/Glossary#project) metadata. Existing personal projects, workspaces and [sessions](/User-Guide/Glossary#session) stay private.
Members publish committed work explicitly and launch sessions in private same-host checkouts. The work board shares explicit tasks and advisory claims. Own-machine collaboration has an optional first slice, disabled by default; see **Use your own enrolled machine** below.

## Create a group (instance owners)

1. Open **Groups** under **Configure**. You must already have a Rynhult user to appoint.
2. Enter **Group name**, for example **Demo team**, choose an **Initial maintainer**,
   and select **Create group**. Creating the first group opts the installation in;
   Rynhult never creates a default group or shares existing projects automatically.

![Groups with an artificial Demo team and its management controls](/.attachments/groups.png)

The new group shows **Active**, your role and its authorization revision.
A Rynhult instance owner remains a trusted administrator, independently of group roles.

## Manage people (owners and maintainers)

1. Select **Manage people** on your group. This lists existing Rynhult usernames and
   their membership, without [agent accounts](/User-Guide/Glossary#agent-account), sign-in information or keys.
2. Choose a role and select **Add person** or **Save role**. Only instance owners
   can appoint another maintainer. Maintainers cannot invite new Rynhult users or
   create agent accounts.
3. Making someone a viewer or removing them asks for confirmation. Their group
   access changes immediately. Their unrelated personal projects remain theirs.
   Removing someone also revokes every consent they gave to use their keys in
   this group. Sessions using those keys stop, even if another member launched
   them. Making someone a viewer stops their own group sessions but does not
   delete their key consents.

| Group role | Current authority |
|---|---|
| Maintainer | Manage existing users, register projects, read safe group audit, approve merges when named as a project owner |
| Member | Launch own group sessions, claim paths, publish/fetch own committed work, browse published Files/Wiki, download group recovery; no administration |
| Viewer | Read published code and safe backup status; no push, launches, backup download or placement paths |
| Nonmember or removed member | No group access |

![People dialog showing an artificial maintainer and role controls](/.attachments/groups-people.png)

Rynhult keeps at least one maintainer and one named maintainer per project. Before
removing a Rynhult user who holds either responsibility, appoint a replacement.
There is no auditor role. Group roles never grant host administration, global
audit, publishing approval or access to another person's terminal.

## Register a project

Select **Projects**, enter **Project name** and an unused **Project slug**, and
choose a maintainer as **Project owner**. Select **Create group project**.
The row says **Private checkouts**. Registering a project never shares a personal folder; private placements and publication are explicit.
![Projects dialog showing Demo API and private checkouts](/.attachments/groups-projects.png)

Choose **Development workspace** under **Project kind** for a group workspace. It keeps the no-external-push policy. Additional named owners can be managed through the authority API.
An existing personal project cannot be moved into a group in this phase.

Every integration merge needs a named project maintainer's approval of the exact
branch and old/new commit IDs. The approval API records consent; a maintainer applies that exact approval through
the integration API. The proposed result must already be published for review.
Changes to group authority or the target branch invalidate earlier approvals.
Tags are maintainer-controlled and immutable; no one can force or delete refs.

## Read published Files and Wiki

1. Open **Groups**, then **Projects** on a group you belong to.
2. Select **Published Files & Wiki** beside a project.
3. Choose **Published branch or tag**, then **Files** or **Wiki** under
   **View published content**. Select a **Published file**.

![Published Wiki text at a selected committed revision, using an artificial example](/.attachments/groups-published.png)

The full commit ID identifies the content you are reading. Choose **Compare with published base** to view a bounded diff between two published commits; private or unknown commits are refused. Only explicitly
published, regular UTF-8 files appear; dirty files, private branches, symlink
contents and submodules do not. Wiki Markdown appears as text, without loading
remote images or executing repository HTML. Changing a publication does not change
the commit you already selected; reopen the view to choose the current tip.

An empty view means nobody has published committed work yet. A failed request
shows an error instead of pretending the project is empty. Losing membership
removes shared access, including already-issued exchange grants. Previously copied
code cannot be recalled. An instance owner needs group membership to use this
collaboration view, independently of their trusted personal administration rights.

## Publish, fetch and integrate committed work

Commit exchange uses the authenticated authority API and scoped standard Git HTTP. The work board launches reviewed source in a private checkout. An owner first
reserves and provisions your same-host placement. The checkout API copies a chosen
published tip into a fresh private session [worktree](/User-Guide/Glossary#worktree). It never overwrites an existing
worktree or merges into dirty files. The publish API sends only your selected
committed history; it does not include dirty files or other private branches.

Your published branch is derived from your stable Rynhult identity and checkout ID.
The first publication expects an empty branch; subsequent publications must
fast-forward its current tip. Viewers can fetch published commits, but cannot
publish. Review source before running it: independent clones do not sandbox
malicious build scripts or agent instructions.

Integration requires a named project's maintainer owner to approve the exact
branch and old/new commit IDs through the approval API. A maintainer then
applies that receipt. A changed membership, changed branch or consumed receipt
refuses the operation. If a response is uncertain, inspect published refs before
retrying. Another review is required when the expected branch tip changes.
Standard Git clients can push/fetch/clone through a short-lived scoped exchange
grant; viewers can clone published code without gaining write access. The broker
helpers keep credentials out of repository config.
See [Group Exchange](/Internal/Group-Exchange) for endpoint payloads and limits.

## Group and private recovery

Maintainers can create group recovery snapshots of published refs and safe group
metadata, then restore a selected backed-up tip to a new recovery branch. Integration
still needs named-owner approval. Members and maintainers can download group recovery;
viewers can see safe snapshot status and read published code, but cannot download
snapshots or request a restore. Group backup downloads appear in the maintainers'
group audit.

Private recovery is a separate API and store. It saves an explicitly selected
private committed history from your own worktree. Only you and trusted instance
owners can download it, even after you leave the group. Keep the returned snapshot
ID for retrieval. Creating or downloading private recovery leaves a receipt in
the instance owners' audit, never in the group audit. Joining or maintaining a group
never opens another person's private recovery. Neither kind captures dirty files, credentials or provider sign-in.

Both stores join the existing encrypted host backup capture and off-site retention.
A successful local group snapshot does not prove off-site protection; inspect
**Host → Backups** for the host backup result. The same recovery-kit and held-restore
rules apply. Local snapshot/storage limits refuse further captures without pruning
history; ask an instance owner to review storage and retention.

## Archive a group or override a pause

An instance owner selects **Archive group** and confirms. Membership and history
remain, but further changes and exchange access are refused. Archival is explicit; removing a member
does not silently archive a group. All group key consents are revoked and running
group sessions are stopped, with secret cleanup retried if a machine is unavailable.
A paused group shows **Override pause** to its
maintainers and instance owners; an override is audited and lasts 15 minutes. Launch quotas and reported-usage warnings are group-scoped. Running sessions continue when a quota pauses new launches. There is no pooled subscription percentage or hard monetary cap.

![Groups on a phone with the same management controls](/.attachments/groups-phone.png)

## Keys and placements

The authority API records same-host placement reservations. Only instance owners
approve a binding, and Rynhult derives the person's account and private destination.
Reservations start with **pending** trust and **unprovisioned** sync. An instance
owner provisions the reservation through its account broker. Unsafe permissions,
extended ACLs, symlinks and foreign ownership are refused. Successful provisioning
shows **approved** trust and **ready** sync. Each session worktree has its own Git
metadata. New source is untrusted; review it before running it. A member can inspect only their own reservation;
viewers cannot read placement paths.

Group sessions run only under an agent account that belongs to one person. If another Rynhult person's
sessions use the same agent account (for example, two people without their own account both use the
default one), Rynhult refuses the group launch and stops running group sessions on that account: a shared
account could read the other person's private checkout and keys. An instance owner gives each member
their own agent account (see [People and sign-in](/User-Guide/People-and-Sign-in)).
The launch alert says **Your agent account is shared with another Rynhult person** and asks for a
dedicated agent account. This also applies to instance owners: an approved checkout does not make a
shared account private. Removing a person from Rynhult ends their account assignment; merely removing
them from this group does not, because their other sessions can still use that account.

Only a key's owner can explicitly grant or revoke group consent, optionally for a
project or machine. Rynhult also revokes your consents automatically when you leave
or are removed from the group, and revokes everyone's consents when the group is
archived. Each consent gets its own audit entry, committed with the membership
change or archival. Sessions using or potentially receiving those keys are
stopped and their delivered secrets are cleaned up, just as with explicit
revocation. Other groups and unrelated personal sessions are unaffected.
If you rejoin, you must explicitly grant consent again; old consents never return.
Already copied keys cannot be recalled; rotate a leaked key at its provider.

The authority API's `GET /api/groups/key-grants/owned` lists only your current
consent records and the revision needed to revoke them. Departed or archived
group consents no longer appear. It never shows anyone else's keys.
Use that current revision when posting to
`/api/groups/GROUP_ID/key-grants/GRANT_ID/revoke`; stale revocations remain refused.
A local member can leave through the authority API by posting
`{"role": "remove", "revision": CURRENT_REVISION}` to
`/api/groups/GROUP_ID/members/YOUR_USER_ID`. There is no separate Leave button.
The last maintainer or sole named project owner must appoint a replacement first;
identity-provider-managed membership must be changed there.
A maintainer or instance owner attaches an eligible consented key to a group project in the launch view. The key ID is shown without its private name or value. Delivery checks the attachment, key-owner consent, current launching identity and local-machine restriction at every start. Existing project key grants never become group grants automatically. No one gets another person's
provider sign-in, transcripts, artifacts, dirty files or unpublished changes by
joining a group. Instance owners retain Rynhult's existing trusted administrative access.
Tasks are visible to every current group role. Path summaries are visible to maintainers and members, never viewers.

## Launch a group session

1. Select **Work board** on your group, then choose **New group session** under **Group view**.
2. Choose a **Group project**. An instance owner first uses **Approve private checkout for** and
   **Approve & provision checkout** for each developer. Unsafe account roots fail rather than being repaired automatically.
3. Choose **Your private checkout** and a **Reviewed published base**. Read the source through
   **Projects → Published Files & Wiki** before trusting it. Rynhult runs only on this host in this phase.
4. Enter a **Group-safe task**. Everyone in the group sees it, so omit credentials and private paths.
   Enter personal instructions separately under **Private opening prompt**; that prompt never becomes the shared task.
5. Choose **Your agent**, review eligible group key IDs and confirm that you reviewed and trust the source.
   Select **Launch group session**. Retrying an uncertain response for that checkout returns the same session.

![A reviewed group launch with explicit shared task and private prompt on desktop](/.attachments/groups-launch-1440.png)

![The same launch form stacked on a phone](/.attachments/groups-launch-390.png)

The checkout ID becomes the session ID. The session keeps its own person, machine, account and path binding.
Changing project defaults cannot redirect it. Open your session through **Open session**; another member
cannot open its terminal, transcript, artifacts or unfinished diff. Your own ordinary session controls still work.
Removing a developer or making them a viewer persists stop intent, releases claims, removes launch authority
and requests key cleanup. If the broker is unavailable, Rynhult retries cleanup when it returns.

## Coordinate work and refresh context

1. In **Work board & Sessions**, read the person, machine, branch, task, freshness and existing claims.
2. In your own row, enter a source file or directory prefix in **Claim relative path**, then select **Claim path**.
   A conflict requires **Confirm shared work** before another overlapping claim is recorded. Coordinate with the other person.
3. Use **Shared task → Update task** to change the explicit task, or **Release claim** when you finish with a path.
4. Select **Refresh context** to inspect the current bounded board snapshot. Agents also read
   `$ACC_RUN_DIR/group-context.json`; Claude turn hooks attach its latest local snapshot. Other agents must refresh explicitly.

![A desktop board with explicit task, path claim and refreshed context](/.attachments/groups-board-1440.png)

![A phone board with stacked person, machine and claim controls](/.attachments/groups-board-390.png)

Claims are advisory 15-minute leases, not file locks. A live working session renews active claims;
idle and finished sessions release them. Missing heartbeats expire claims. Periodic broker Git observations
catch paths changed without hooks; protected paths are filtered before sharing. Observations can miss intent,
renames and semantic conflicts. A stale or unavailable board is never proof that editing is safe.

Within a session, these commands queue a session-bound request without a dashboard token:

```text
rynhult-request work list
rynhult-request work task --text "Review validation"
rynhult-request work claim --path src/api/
rynhult-request work claim --path src/api/ --shared
rynhult-request work release --path src/api/
```

Queued does not mean accepted. Read the request ID and its accepted/conflict/refused reply in the refreshed
context attachment. Duplicate request IDs reuse their receipt; revoked sessions cannot change the board.
Group sessions currently launch one agent at a time; group [Orchestrator](/User-Guide/Glossary#orchestrator-lead-and-helpers) helpers, schedules and multi-run are refused.

## Group operations and launch quotas

Choose **Group operations** for safe conflict and quota notices, or **Assigned to me** for operations assigned to your username. A maintainer or instance owner can
**Resolve operation**, or choose **Assign operation → Save assignee** to assign a current maintainer. Updates compare the operation revision; stale updates must refresh. Affected people also receive their own conflict notice; acknowledging your personal
notice does not resolve another person's notice. Use **Acknowledge my conflict** for a conflict affecting you; another person’s acknowledgment does not acknowledge yours. Private permission and sign-in prompts stay in personal [Inbox](/User-Guide/Glossary#inbox).

![Safe group operations with maintainer assignment and resolution](/.attachments/groups-operations.png)

Choose **Usage & Settings** to inspect concurrent reservations, today's launches and resumes (UTC day),
reported USD readings and reporting coverage. Unknown readings remain unknown. At most the latest 100 sessions'
reported costs appear; older or unsupported readings make coverage partial. Readings are provider-reported,
not an invoice or a spending guarantee.

![Group launch quotas and reported-usage warning settings](/.attachments/groups-usage.png) Personal subscription percentages are never added together.

Maintainers set **Concurrent sessions**, **Daily launches** and an optional **Reported USD warning**.
Quota saves use the revision of the values you opened. If another tab or person changes the group,
Rynhult refuses the stale save. Refresh the page, reopen **Usage & Settings**, and review the current limits before retrying.
Only instance owners raise quota ceilings (initially 4 concurrent sessions and 50 daily launches).
A quota pause blocks new reservations until reviewed; existing sessions continue. **Override pause** allows
launches for 15 minutes and leaves an audit receipt. An override lifts the maintainers' limits, never the
instance owner's ceilings; beyond a ceiling, launches are refused until an instance owner raises it. USD thresholds warn; they never enforce a provider spending cap.

## Troubleshooting

- **Group changed:** another change used the displayed revision. Refresh the page,
  review the new membership and retry. A stale request never overwrites it.
- **Keep at least one maintainer / appoint another named project owner:** choose
  a replacement before downgrading or removing that person.
- **No such group:** you may have been removed. Ask an instance owner or maintainer.
- **Group launches paused:** review **Usage & Settings**. A maintainer or instance owner can select **Override pause** on the group. Personal budget confirmation does not override a group pause.
- **Group launch ceilings reached:** an override cannot pass the instance owner's ceilings. Ask an instance owner to review them.
- **No authorized private group checkout** while your checkout is ready: your agent account may be shared with another person. Ask an instance owner for your own agent account.
- **No approved checkout:** ask an instance owner to reserve and provision your own same-host checkout. A checkout belonging to another person cannot be selected.
- **Stale heartbeat:** refresh the board; do not assume missing or expired claims mean nobody is editing.
- **Source trust required:** read the published files and confirm the selected source before running it with your account and keys.
- **Published ref changed / integration branch changed:** fetch current refs, review
  the change and obtain a fresh exact approval; never force an earlier result.
- **Exchange or recovery limit reached:** ask an instance owner to review storage
  and retention. A refusal does not delete an earlier recovery snapshot. Each person
  can publish at most 32 branches per project: keep publishing to an existing session
  branch. Part of the project's storage is reserved for maintainers' integration,
  tags and restores.
- **Exchange busy / too many requests:** a host backup or another Git operation is
  running, or a grant is already in use. Wait a moment, then request a new grant.
- **Pack would expand beyond its limit:** a single file over 128 MiB, or a publication
  expanding beyond 512 MiB, cannot be exchanged. Keep large generated files out of
  published commits.

See [People and sign-in](/User-Guide/People-and-Sign-in) for Rynhult accounts and
[Workspaces](/User-Guide/Workspaces) for current personal workspaces.

Group sessions launch reviewed published source in private same-host checkouts. See **Launch a group session** above for the required placement and source review. Owners can create a group above an empty list and choose its first maintainer; others ask an instance owner for membership. People marked **Added in Rynhult** can be managed here; roles **Managed by your identity provider** must be changed there. Revision identifiers are under Details.

If the work board is slow to load, **Usage & Settings** waits for the saved limits before opening its quota form. Automatic board refreshes keep your unfinished quota edits.


## Use your own enrolled machine

An instance owner must first enable own-machine groups in the deployment settings
and enroll your machine. This is an early opt-in path; native platform qualification
is still required before rollout. Your machine's administrator can read its files
and credentials. A native Mac uses the signed-in OS account.

1. Open **Groups → Work board → New group session** and select your project.
2. Under **Your own machine**, enter the enrolled machine ID supplied by the
   instance owner and your broker account, then select **Request machine account**.
3. On that machine, open a terminal as the requested broker account. Run the
   `rynhult-node group-proof` command shown on your pending request. Replace
   `BROKER_RUN_DIR` with that account's broker run directory: normally
   `/srv/acc/run/alex-agent` on Linux/WSL, or the signed-in account's
   `"$HOME/Library/Application Support/ACC/run/alex"` on a Mac (quote paths containing
   spaces). Your machine administrator can confirm a custom location from the
   broker configuration. Never run this command as root or as another person.
4. Enter the ten-character **Confirmation code** shown in that terminal and select
   **Verify on the machine** within five minutes. Codes work once, for that exact
   request, machine and account. You can generate another after a minute; five
   incorrect checks exhaust a code. Too many checks in Rynhult require a minute's
   wait. A restart does not reset these limits.
5. An instance owner reviews **Verified on the machine** and selects
   **Approve machine account**. **Unverified** requests cannot be approved.
   A matching account name alone does not identify you. A machine account approved
   before confirmation codes were introduced also needs this verification before
   group work can run there.
![Verified account awaiting owner approval on a demo machine](/.attachments/groups-account-proof-1440.png)

![Machine account verification on a phone](/.attachments/groups-account-proof-390.png)

6. Choose **Your approved machine account** and select **Request project placement**.
   A group maintainer or instance owner reviews **Project machine approvals** and
   selects **Approve project placement** while the machine is connected.
7. Choose the new machine under **Your private checkout**, select reviewed published
   source, enter a group-safe task, confirm source trust and launch. The session
   runs in your private checkout on that machine. Your provider sign-in stays there.

![Example group session on an approved own machine](/.attachments/groups-own-machine-1440.png)

![Own-machine group launch at phone width](/.attachments/groups-own-machine-390.png)

Other people's private files, prompts, terminals and unpublished commits stay
private, and a group maintainer who approves your placement gets no access to your
sessions. Instance owners can still open any session, including yours on your own
machine, as on every machine enrolled with your Rynhult. Only your own keys with **Other machines** enabled can be delivered in
this slice. A teammate's group key consent does not authorize remote delivery.
Publishing remains explicit; reconnecting never publishes or resets your edits.
This slice does not add remote private-checkout recovery backups.

While disconnected, existing agents may keep editing, but new launches and sync
need the main's authorization. The work board is stale. Revoking a machine account,
project placement or membership refuses further group execution. **Cleanup pending**
means stopping or key-file removal has not been confirmed; reconnecting retries
them before any restart. Copied code or keys cannot be recalled. A removed [node](/User-Guide/Glossary#machine-node)'s
enrollment key cannot reconnect; its operator must stop sessions and remove secrets
locally. Revoked bindings are permanent in this first slice; ask the instance owner
before arranging a freshly enrolled machine/account. A removed machine that had
machine accounts is enrolled again under a new name. Cancelling an unapproved
request releases it without granting authority. Personal sessions are unaffected.
