# Moving from Conductor or Jules

Keep your code and branches. Start new agent sessions in Rynhult.
There is no Conductor or Jules session importer.

Conductor is joining Meta. Its Cloud service goes read-only on **November 13,
2026** and closes on **November 20, 2026**. Local Conductor continues receiving
new models and critical fixes until early 2027. See the
[Conductor changelog](https://www.conductor.build/changelog)
(accessed November 10, 2026).

Google Jules is **reported** to be closing. See
[TechRadar's report](https://www.techradar.com/pro/google-kills-off-jules-its-ai-coding-agent-which-promised-to-do-all-the-jobs-you-didnt-want-to)
(published November 2; accessed November 10, 2026). Save your work and exports
before leaving it.

## Save your Conductor work

In Conductor, open **Settings → Account → Export data**. Keep the export as an
archive. For a cloud workspace, right-click it and choose **Transfer to local**
to bring the workspace and chats to your Mac. These are Conductor's
[documented export steps](https://www.conductor.build/changelog)
(accessed November 10, 2026). Transferring chats to local Conductor does not
import them into Rynhult.

Local Conductor workspaces are Git worktrees. They usually live at
`~/conductor/workspaces/<repo name>/<workspace name>`. Each has its own checked-out
branch, but shares Git data with the original repository. See
[Conductor's Git worktrees guide](https://www.conductor.build/docs/concepts/git-worktrees)
(accessed November 10, 2026).

In a terminal in the original repository, run `git worktree list` to find the
actual folders and branches. In each folder, run `git status` to check for
uncommitted work. Keep the original repository and worktree folders together;
copying just a linked worktree folder does not make an independent repository.
Stop Conductor agents before letting a new agent edit the same folder.

For Jules, first get your code into a local Git checkout on the branch you want
to continue. Keep any downloaded session export separately. The folder steps
below apply to that checkout too.

## Register each existing folder

Rynhult needs to reach the machine holding the folder. For a checkout on your Mac,
connect that Mac as a [machine](/User-Guide/Nodes) to your self-hosted Linux main.
The selected agent account must already be able to read and write the checkout
and its shared Git data. See [existing repository access on macOS](/User-Guide/Install-macOS#existing-repository-access).

1. Open **Projects → Register folder**, or **New project → Use existing folder**.
2. Enter a **Name** and unique **Slug**. Select the **Machine** holding the
   checkout and, when offered, its **Agent account**.
3. Set **Workspace path** to the full existing worktree path. Use a separate
   registration for each folder/branch you want to continue.
4. Choose **Let agents work here**, then **Register**. **Review only** is for
   inspection rather than editing.

Registration keeps the folder where it is. It does not clone, move, initialize
Git or change the branch. If already registered, choose **Open the existing
project**. See [Projects → Adding a project](/User-Guide/Projects#adding-a-project)
and [Existing folders and account access](/User-Guide/Projects#existing-folders-and-account-access).

If Rynhult asks you to allow the folder, follow **Open Allowed folders in
Security**. An owner selects the correct machine/account, enters the exact
repository folder, then uses **Check folder → Add to review → Preview folders →
Apply folder policy**. Wait for target acknowledgement before retrying registration.
This allows account-service access; it does not register a project, trust its code
or grant OS permissions. Members ask an owner. See
[Allowed folders](/User-Guide/Settings#allowed-folders).

## Continue on the same branch

1. Sign in to your providers again on **Providers**, for the agent account and
   machine you will use. See [Host and providers](/User-Guide/Host-and-Providers).
2. Open the registered project and choose **New session**. Check the folder under
   **Project** and keep **Machine** set to the machine holding that checkout.
3. Under **Model and working location**, leave **Own worktree** unchecked to work
   in that existing folder on its current branch. Enabling it creates a separate
   checkout and a new `acc/…` branch from the project's current commit.
4. Enter your task and choose **Launch**, or **Trust and launch** after reviewing
   the named folder. Check the branch in the session header before editing.

See [New session](/User-Guide/New-Session) and
[Sessions → Worktrees](/User-Guide/Sessions#worktrees). Give the new session a
short handoff with the goal, unfinished work and checks already run. Avoid running
two agents in the same checkout at once.

## What carries over

Your existing checkout keeps its files, commits, branch and uncommitted changes.
Chat history, cloud sessions and credentials do not carry over into Rynhult.
Keep exported conversations for reference. Start fresh sessions and sign in to
providers again. Review any old setup scripts and environment settings before
using them in the new account.

Rynhult is self-hosted. Its server and agent sessions run on your machines;
there is no Rynhult vendor cloud workspace service to shut down. You still manage
backups and machine access, and external model providers have their own service
availability. Start with [Getting started](/User-Guide/Getting-Started) and
[Backups](/User-Guide/Backups).
