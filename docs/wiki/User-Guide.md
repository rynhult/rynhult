# User guide

Rynhult is spoken **RIN-hult**.

How to use Rynhult day to day. Each page covers one job; the pages run in the order a new owner needs them: install, first project and session, your phone, backups, more machines, then everyday work and advanced setup. New to the words? See the [Glossary](/User-Guide/Glossary). Invited by an owner? Start at [Getting started](/User-Guide/Getting-Started).

**v1.0:** the self-hosted Linux server, browser access including the phone web
app, and existing Linux/macOS/Windows-WSL nodes. Native apps are later, serial
post-v1 work; the experimental source described in the advanced pages is not
a v1 native release.
See [platform scope and native-app order](/User-Guide/Install#v10-platform-scope).

## Install and first steps

| Page | What it covers |
|---|---|
| [Install](/User-Guide/Install) | Linux main, Linux/macOS/Windows-WSL nodes, v1 scope and certificate trust |
| [Deploying](/User-Guide/Deploying) | Draft small-team deployment: Linux server, private remote access, nodes, recovery and updates |
| [Install on Windows](/User-Guide/Install-Windows) | Rynhult Setup for Windows 11: the main Rynhult or an additional machine |
| [Install on a Mac](/User-Guide/Install-macOS) | Rynhult Setup for macOS: an additional machine |
| [Getting started](/User-Guide/Getting-Started) | First sign-in, the Get started list, the layout, your first session |
| [Glossary](/User-Guide/Glossary) | The words Rynhult uses, in plain language |
| [Host and providers](/User-Guide/Host-and-Providers) | Signing your agents in (Providers), machine health and plan usage |
| [Connect an agent](/User-Guide/Connect-an-Agent) | Installing and signing in an agent CLI where Rynhult can't install it: step by step per system |
| [Projects](/User-Guide/Projects) | New projects (empty, cloned or on GitHub), trust, and registering folders |
| [Project brief](/User-Guide/Project-Brief) | Reusable project scope, private captioned references and versioned team intake |
| [New session](/User-Guide/New-Session) | Starting a session: the task, the agent, and everything under Advanced |
| [Sessions](/User-Guide/Sessions) | Watching, typing into, stopping and resuming sessions |
| [Overview](/User-Guide/Overview) | The home screen: what needs you and what is running |
| [Inbox](/User-Guide/Inbox) | Everything that needs your attention |


## Your phone

| Page | What it covers |
|---|---|
| [Connect your phone](/User-Guide/Connect-Your-Phone) | Reaching Rynhult from a phone, its certificate, the app and notifications |
| [Notifications and app](/User-Guide/Notifications-and-App) | Push notifications and installing Rynhult on your phone |


## Backups and more machines (owners)

| Page | What it covers |
|---|---|
| [Backups](/User-Guide/Backups) | Encrypted Linux/S3/local targets, opt-in hourly captures, retention maintenance and restore drills |
| [Nodes](/User-Guide/Nodes) | Using other machines: adding, updating and removing them |


## Everyday work

| Page | What it covers |
|---|---|
| [Workspaces](/User-Guide/Workspaces) | Research without a project, and promoting it to one |
| [Files](/User-Guide/Files) | Browsing and editing a project's, workspace's or session's files |
| [Wikis](/User-Guide/Wikis) | Reading a project's wiki |
| [Preview](/User-Guide/Preview) | Seeing a project's running web app inside Rynhult |
| [Search](/User-Guide/Search) | Finding anything said in any session's conversation |
| [Keys](/User-Guide/Keys) | API keys for projects, given to sessions as environment variables |
| [Budgets](/User-Guide/Budgets) | Optional limits on your own plan usage |
| [Guardrails](/User-Guide/Guardrails) | What agents are stopped from doing, in every permission mode |
| [Schedules](/User-Guide/Schedules) | Start sessions at times you choose |
| [Orchestrate a project](/User-Guide/Orchestrate-a-Project) | A team of agents plans and builds with you: the whole process, step by step <!-- lane F --> |
| [Orchestrator](/User-Guide/Orchestrator) | Optionally granting a bounded team of helpers and watching or stopping it |
| [Orchestration playbook](/User-Guide/Orchestration-Playbook) | How to run a team well: one working list, written briefs, independent review, gates and owner decisions |
| [Publishing](/User-Guide/Publishing) | Putting a project on a hostname |
| [Catalog](/User-Guide/Catalog) | Profiles, skills, agents and MCP servers |
| [Keyboard shortcuts](/User-Guide/Keyboard-Shortcuts) | Moving fast |
| [Every feature at a glance](/User-Guide/Features) | A screenshot tour of everything Rynhult does |
| [Troubleshooting](/User-Guide/Troubleshooting) | Symptoms, likely causes, checks and safe fixes, drawn from operational problems |
| [FAQ](/User-Guide/FAQ) | Short answers |


## Running Rynhult (owners)

| Page | What it covers |
|---|---|
| [Settings](/User-Guide/Settings) | Your preferences and owner installation controls |
| [Admin features](/User-Guide/Admin-Features) | Owner controls for optional features and Previews |
| [People and sign-in](/User-Guide/People-and-Sign-in) | Your sign-ins and password; inviting people (owners) |
| [Updating Rynhult](/User-Guide/Updating) | Back up and update the main Rynhult; sync other machines |
| [Upgrade from rc.1](/User-Guide/Upgrade-from-rc1) | Signed in-place rc.2 upgrade and rollback, main then nodes |
| [Status board](/User-Guide/Status-Board) | One full-screen view of every machine, project and running session, for a large display |
| [Audit](/User-Guide/Audit) | Who changed what |
| [Usage and bug reports](/User-Guide/Usage-and-Bug-Reports) | Optional sharing, report review, local history and deletion |


## Advanced (owners)

| Page | What it covers |
|---|---|
| [Remote access](/User-Guide/Remote-Access) | Disabled public access and one-use LAN device pairing, renewal and revocation |
| [Company sign-in with Microsoft Entra ID](/User-Guide/Company-Sign-In-Entra) | Member OIDC pilot, off by default and not yet tenant-qualified |
| [Groups](/User-Guide/Groups) | Opt-in authority, private Git exchange, published Files/Wiki and group sessions |
| [High availability](/User-Guide/High-Availability) | Standby status and owner-run manual failover with PostgreSQL |
| [DNS providers](/User-Guide/DNS-Providers) | Scoped credentials, vault Publishing and trial zones |
| [Qualify a DNS provider](/User-Guide/DNS-Qualification) | Owner-run disposable-zone checks, least-privilege credentials and qualification reports |
| [Caddy hardening](/User-Guide/Caddy-Hardening) | Certificate credentials, root helper, sandbox, timeouts and log policy |
| [Mobile devices](/User-Guide/Mobile-Devices) | Post-v1 experimental native enrollment, owner controls and qualification limits |
| [Desktop apps](/User-Guide/Desktop-Apps) | Post-v1 developer source and qualification; not the v1 browser app |
| [Rename readiness](/User-Guide/Rename-Readiness) | Read-only rename planning; how it differs from implemented phase B aliases |

## Command names and compatibility

Use `rynhult` for CLI help, `rynhultd` for the daemon, `rynhult-node` for additional
machines, and `rynhult-guard` for the tool guard. `rynhult daemon`, `rynhult node`
and `rynhult guard` dispatch to the same components. The legacy commands `acc`,
`accd`, `acc-node` and `acc-guard` remain supported aliases.

All other existing `acc*` helper commands remain supported compatibility aliases,
including `acc-install`, `acc-request`, `acc-hook`, `acc-statusline`,
`acc-wiki-check`, `acc-restore` and `acc-db-transfer`. Existing automation can keep
using them. Both `RYNHULT_*` and `ACC_*` settings work; when both are set, the
`RYNHULT_*` value wins, even when empty. Phase B adds command and Linux/WSL
service aliases without moving data or starting duplicate services. The Python
package `acc`, existing `/srv/acc` paths, database names and browser cookies stay
unchanged. Mac launchd labels and Windows tasks are not renamed. Existing
installations need no data migration or reinstall; see
[name compatibility and alias-only deployment](/User-Guide/Updating#rynhult-name-compatibility)
before adding aliases from a reviewed checkout.

The public site is rynhult.dev, documentation is rynhult.dev/docs/ (docs.rynhult.dev redirects there), and contact details are in the repository README and security policy. The telemetry collector at
telemetry.rynhult.dev runs as a pre-release staging service; telemetry remains opt-in.
