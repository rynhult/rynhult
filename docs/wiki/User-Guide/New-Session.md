# New session

Select **New session** (top bar, or press <kbd>C</kbd>). Most launches need only a [project](/User-Guide/Glossary#project) and a task: choose them and select **Launch**. The form opens in the working area. **Agent or team** holds Agent, Profile and Run; the remaining choices live under **Model and working location**, **Permissions and limits**, and **More options**.

### Task and working location
1. **Where it runs** (under **Model and working location**). **Project** (a registered project or Development workspace) or **Local workspace** (see [Workspaces](/User-Guide/Workspaces)). With no project yet, select **New project** right there (see [Projects](/User-Guide/Projects#adding-a-project)); what you entered stays.
2. **Project.** The line under it is the project's registered folder, prefixed by its machine when applicable. **Machine** under **Model and working location** defaults to that machine; you can choose another enrolled machine for this session without changing the project's registration. A project that isn't trusted yet says so, and **Launch** becomes **Trust and launch**: it asks once, names the folder, then launches.
   - **Start from.** For registered projects, choose **Your own task (type it below)**, or a connected GitLab issue, GitLab merge request, Gitea / Forgejo issue or pull request, or Linear issue.
     Filter the list and select **Use issue**. The preview includes its title, description and link as untrusted
     reference data fetched by Rynhult; source tokens never reach the agent. Configure connections on
     [Projects](/User-Guide/Projects#connect-gitlab-or-linear). Changing projects or choosing Local workspace clears
     the issue selection. **Choose another** clears it without changing your assignment.
3. **Task.** What the agent should work on. Sent once when the session starts, never again on resume. Without one, the agent starts and waits for you in its terminal. (Required when you run several agents.)
4. If another live session already works in the project's own checkout, a note says so. Open **Model and working location** and select **Own worktree** to give this session its own [worktree](/User-Guide/Glossary#worktree).

### Agent or team
5. **Machine.** Choose an enrolled machine, or **This Rynhult (main)**. The description shows online/offline state, operating system and agents installed for the project's account. An offline machine or one without that account cannot be selected. This choice is available for ordinary project sessions as well as several agents, teams and schedules.
6. **Agent.** Claude Code, Codex, GitHub Copilot CLI, Antigravity, Cursor or OpenCode: whichever are installed for the selected project's [agent account](/User-Guide/Glossary#agent-account) on the chosen machine (see [Host and providers](/User-Guide/Host-and-Providers)). Changing projects or machines reloads availability and models; unavailable agents are disabled with the reason. Local workspaces use your agent account on the named main machine, not the device running your browser.
7. **[Profile](/User-Guide/Glossary#profile).** A role, such as Full-stack, Frontend or Researcher. It sets instructions, the skills and tools the session gets, and permission defaults. The chooser includes **None (house rules only)** and supplies each profile description in its option title.
8. **Keys this session gets** (when the project has keys). The environment variables the session gets from the [key vault](/User-Guide/Keys), by name, never their values: the keys given to the project that you may use (yours and shared ones). Common variables also show the short descriptions used by the Keys dropdown. On another machine, keys whose owner hasn't allowed **Other machines** are listed as not sent. If two keys use the same variable, the preview says which and **Launch** stays disabled until one is removed from the project.
9. **Name.** Rynhult names the session for you and shows the name it will use: from the task's first words for a project (`Fix the login form` → `fix-the-login-form`), from the topic for a new local workspace, and from the profile in an existing one. Select **Rename** to type your own; **Use automatic name** goes back. A name that's already set (from an issue, a link or your draft) shows in the field straight away.

The **Working location** section names the target machine, agent account and recorded or planned checkout.

### Choose another machine

Select **Machine** under **Model and working location**. If Rynhult already knows an accessible checkout
of this project's repository there, the form says **Using an existing checkout**.
The session gets its own worktree of that checkout.

Otherwise choose **Clone on lab-box** (with your machine's name), then **Launch**.
The button records your choice; the copy is made only when you launch. Rynhult
uses the same unprivileged Git-bundle path as team helpers, without a network
clone or a new administrator action. It copies committed Git history, not
uncommitted edits, repository configuration or account credentials. Transfers
are limited to **8 MiB**. For a larger repository, register a trusted checkout
of the same remote URL on that machine first.

The project keeps its original machine. Changing machines does not move an
existing session; its later resumes use its recorded machine. Private copied
repositories do not inherit an `origin` remote: configure one explicitly if you
want an ordinary agent to push from there. Local workspaces and Development
workspaces keep their existing local-only rules; container sessions still
require the qualified main machine. Ask an owner to enroll a missing machine
or add the project's agent account there.

### Detailed choices
The summary under **Agent or team** describes the current choices. Open the named sections to change them:
- **Run** (under **Agent or team**). **Once** (the default); **Several agents** (see [Run on several agents](#run-on-several-agents)); **With helpers** (see [Optional Orchestrator role](#optional-orchestrator-role)); or **On a schedule**, which adds **Timing** under the task and saves a schedule instead of launching (see [Schedules](/User-Guide/Schedules)).
- **Own worktree** (under **Model and working location**, for project sessions). Runs the agent in a separate copy of the project on its own branch (`acc/<name>-<id>`), so several agents can work on one project at once without touching each other's files. Development workspaces require this option; it stays checked and cannot be turned off. Their git stays local-only. See [Sessions → Worktrees](/User-Guide/Sessions#worktrees).
- **Model** and **Effort** (under **Model and working location**). Leave them on the defaults unless you need a specific model. OpenCode's models are written `provider/model` (choose **Custom…** to type one, for example `openrouter/anthropic/claude-sonnet-4`), and OpenCode has no effort setting. New session asks the selected account's broker for its model list and caches it for an hour. Switching projects clears the model and effort so a choice from the previous account is not carried over.
  - **Models from.** When an owner has added model endpoints (see [Host and providers](/User-Guide/Host-and-Providers)), choose one to run the agent on a local LLM or another compatible API instead of its vendor's models, then pick one of its models. The terminal, hooks and resume work as usual; Remote Control isn't available on an endpoint.
- Under **Permissions and limits**: **Permissions** (Claude), **What it can change** (*Can edit the project*, *Read only* or *Full access*) and **When it asks** (*Asks when it needs to*, *Asks before most commands* or *Never asks*) for Codex, or each other agent's mode options. OpenCode has **Permissions** (*Ask before commands*, the default; *Ask before commands and edits*; *Approve everything not denied*) and **Primary agent** (*Build*, or *Plan*, which doesn't edit files). How much the agent may do without asking. [Guardrails](/User-Guide/Guardrails) apply whatever you choose here. Modes that let it act without asking need an extra confirmation, reviewed in **Permissions and limits**, and that choice is recorded in the [Audit](/User-Guide/Audit) log.
- **Remote Control** (Claude, under **Permissions and limits**). Also lets you continue the conversation from claude.ai or the Claude app.
- **Goal and auto-resume** (under **More options**; see [below](#optional-auto-resume-and-goal)).
- **Skills and tools** (under **More options**). The profile's set is shown; select **Customize** to add or remove skills, subagents and MCP servers.
  Only what the chosen agent can receive is offered. The profile's instructions, skills and MCP servers reach every agent; subagents reach Claude, Cursor, Antigravity and OpenCode (not Codex or Copilot). Claude, Copilot, Cursor, Antigravity and OpenCode get them from the session's own folder, for that session only; nothing is written into your workspace. Whatever the profile holds that the agent won't receive is listed under **Not passed to …**, with the reason.
  - Cursor asks before each MCP tool call ("Run this MCP tool?"), which shows the session as needing you.
  - The agents' own account-wide settings still apply alongside: MCP servers and skills set up in the agent itself (for example `~/.cursor/mcp.json` or `~/.gemini/config`), and for Cursor every catalog skill linked for Codex. Claude limits a session to exactly what you select. OpenCode nearly does: an OpenCode session uses its own config folder instead of the account's `~/.config/opencode`, though skills in `~/.agents/skills` and the project's own `opencode.json` or `.opencode/` still apply.
- **Exactly what will run** (under **More options**). The command and every generated file. That is what launches, nothing more.

**Launch** stays disabled until the form is complete and the available agents have been checked; the line beside it says what is missing or still being checked.

Launch errors and **Not passed to …** warnings stay visible outside the closed disclosures. With several agents, each affected run is named there, alongside its keys and any keys withheld from another machine; opening the command preview is not needed to see these warnings.

### Your draft is kept
**Back to workspace** or Esc keeps what you entered: the next **New session** reopens it. **Discard draft** clears it after asking. Issue and pull-request launch targets keep their matching typed drafts when reopened. A different target has a separate draft, and a successful launch leaves the next New session clean. Opening the launcher from an unsaved project brief keeps that brief in place; it does not count as leaving the project.

## Optional Orchestrator role

**With helpers** (under **Agent or team → Run**) is off by default. Choose it only when you want this session to start and coordinate helpers. Set its allowed agents/profiles with permission modes, machines, running and lifetime counts, depth and time. This requires a project and gives the lead its own worktree. See [Orchestrator](/User-Guide/Orchestrator) for the full flow and screenshots. Ordinary sessions keep the same launch defaults.

**Starting from an external issue:** the project-page **Start session** shortcut ticks **Own worktree** for you.
Starting from the picker keeps your existing worktree choice; tick it if you want an isolated branch or the optional
Linear PR-link comment. The source's configured team/project/label restrictions are checked again for the selected
issue. An expired or revoked key blocks preview and launch with an explanation, rather than launching without context.

## Optional auto-resume and goal

Under **More options → Goal and auto-resume**, before **Launch**, enable **Resume when the limit resets** if you want Rynhult to continue after a detected plan limit. Unsupported providers show a disabled checkbox and the reason. Leave **Goal text** empty for an ordinary session, or write a goal and set **Maximum continuations**, **Maximum hours**, and optionally **Maximum plan usage (%)**. These controls are off by default and can be edited on the running session. If you set a goal and leave **Task** empty, Rynhult uses the goal as the first prompt. A usage cap refers to the plan window percentage, not per-session token spending.

See [Goals and automatic continuation](/User-Guide/Sessions#goals-and-automatic-continuation) for progress, stopping, limits and the settings screenshot.

## Run on several agents

Use Multi-run when you want independent solutions to the same task, rather than
a team splitting the work. You start every run; no agent chooses or launches
another run.

1. Choose a trusted git **Project** with at least one commit. **Name** names
   the group (it defaults to the task's first words).
2. Under **Agent or team → Run**, choose **Several agents**. **Agent or team** then lists the
   runs: pick 2 to 5 combinations of **Agent**,
   **Profile** and **Model**. **Add run** adds a choice; **Remove run** removes
   one. You can choose the same CLI twice with different models. Leave a model
   empty for that CLI's default.

3. Check each run's permission options. A mode that lets commands run without
   asking needs its own explicit confirmation, including modes inherited from
   a profile. Changing that choice clears its confirmation.
4. Enter the shared **Task**, review **Exactly what will run** for each
   choice (under **More options**), then select **Launch**.

The group page opens. Each accepted run is an ordinary visible session in its
own worktree and branch, named `acc/<name>-<agent>-<id>`. The prompt and project
are the same; profiles supply each run's instructions and tools. Worktrees are
required, and helpers aren't available with several agents.

Each run gets your usual launch budget check. A budget refusal is shown on that
run's card; the other runs still start. Multi-run does not override a paused
budget. Adjust your [budget](/User-Guide/Budgets) or wait for the window to reset
before explicitly starting another attempt. An interrupted launch is not
automatically retried: inspect **Sessions** before starting another one.

Continue with [comparing and keeping a run](/User-Guide/Sessions#multi-run).

Multi-run uses the shared **Task**. You can configure goals and automatic continuation on each resulting session. A saved schedule launches one configured session per occurrence.

While a launch or save is in progress, repeated clicks on its action are ignored.
Wait for the result before trying again. If a network error appears, check Sessions
before retrying a launch: the server may have accepted it before the connection
was lost.

Under **More options**, **Exactly what will run** has **Copy launch command**, including the working-directory step. Each generated file has its own Copy control and keyboard-readable region. Several agents shows a command for every run. A command containing a placeholder such as `<run-dir>` is a preview: replace the placeholder before running it. Copying does not execute a command.

### Team approval scope

Choosing a profile, saved team or broad task does not issue a saved approval.
The server's new scoped consent records remain separate from the launch grant.
This phase adds no automatic setup-level permission and no new consent switch
to this dialog. Prepare stays read-only; the existing person review still applies.
