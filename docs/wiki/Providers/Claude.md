# Claude Code: subscriptions and automation

Claude sessions signed in with a Claude subscription only take your own typing.
Add an API key to automate them.

With a Pro, Max or Ultra subscription (claude.ai / OAuth sign-in), you can launch
Claude Code, type in its browser terminal, read its output and approve requests
through the documented hooks. The prompt you type in **New session** is allowed.
Launching the CLI to reopen a conversation does not send a continuation prompt.

Rynhult refuses schedules, goals, automatic continuations, team and orchestrator
messages, review feedback, preview feedback and the session message API for these
sessions. Unknown authentication is refused too. A skipped scheduled run appears
in Inbox with the reason. Existing queued messages are checked again before any
terminal input.

Team and orchestrator lead launches are also refused on subscription auth, even
when a person supplies the lead prompt: kickoff, helper messages and automatic
nudges require automation.

To automate Claude, supply `ANTHROPIC_API_KEY` through the Rynhult vault, or supply a supported
cloud provider environment (Bedrock, Vertex or Foundry) through Rynhult at launch.
Agent-writable `apiKeyHelper` and cloud settings alone cannot authorize automation. See
[Providers settings](/User-Guide/Host-and-Providers#providers) for selecting the
machine and account. Claude's [authentication guide](https://code.claude.com/docs/en/authentication)
and [CLI reference](https://code.claude.com/docs/en/cli-reference) describe setup
and `claude auth status`. Check the effective authentication, then relaunch the
session. Rynhult does not treat an OAuth token as an API key.

Rynhult probes the documented auth status in the session's launch environment,
working folder and settings, at launch and on refresh. It stores only the mode
(API key, cloud, subscription or unknown) on the session's auth observation.
Provider settings show the account default; session keys can change the actual
launch mode, which is checked at execution. Automation requires the CLI report
to agree with the environment Rynhult supplied. Refreshes can only revoke this
permission; changing settings or PATH cannot enable automation without relaunch.
Probes use the absolute CLI path resolved at launch.

For subscription or unknown-auth Claude sessions, an authenticated person’s API
`task` is treated as their launch prompt, like **New session**, with the same
20000-character limit (personal Claude tasks are capped before launch). API/cloud Claude sessions and other providers keep task
message delivery; their task is not stored as a launch prompt. An automation bearer token’s `task` uses guarded message delivery;
subscription or unknown auth returns HTTP 409 with the session ID.

After a broker restart, existing Claude terminals have unknown authentication until
relaunched: Rynhult cannot reconstruct their effective environment safely. Older
machine software and unobservable container authentication also refuse automation.
You can keep typing in those terminals. Image qualification prompts still check their
own isolated environment.

## Walkthrough helpers and containers

When a Claude walkthrough has no API key or cloud credentials supplied by Rynhult,
the helper uses Codex if it is installed and signed in on the helper's account.
Automated Claude container launches (including scheduled and team sessions) make
the same choice before compilation. Codex uses its own default model; Claude model
names and command-line options are not forwarded. The walkthrough shows the actual
provider and reason; container session activity records the choice. If Codex is
unavailable, the existing Claude automation refusal remains. The chosen provider's
budgets still apply.

To use Claude, give the source project an `ANTHROPIC_API_KEY` vault key that the
launching person may use, or configure supported cloud credentials through Rynhult.
The walkthrough borrows only Claude credential variables from that project's grants,
never unrelated project keys or MCP configuration. Its isolated home does not borrow
subscription sign-in when API/cloud credentials are supplied. Credentials enter the
launch environment through the existing private run-folder `session.env` delivery;
they are never put in the helper workspace, launch specification, activity or UI.
Only credential IDs are recorded for redaction.

Container credential delivery uses the session/generation-bound adapter and sealed
memory file. An owner must qualify and pin the updated image, entrypoint, launcher
and execution relay, with `credentials: "readonly-v1"` and `claude_auth: "launch-v1"`
in the protected image policy. Older images cannot start automated Claude turns.
The immutable entrypoint checks actual CLI auth before an automated initial prompt;
the broker probes inside that same container through its cgroup-checked relay at
launch and on refresh. Only API/cloud reports matching supplied credentials allow
automation. A later downgrade or change of allowed mode cannot regain authority.
No host-account probe substitutes for container auth. Existing image, egress,
credential-adapter and qualification restrictions remain in force, including the
Codex adapter's supported sign-in formats. Cloud credentials must be usable inside
the isolated runtime; host-only credential-file paths do not become container mounts.

Other providers retain message delivery and automated terminal access after a
broker restart.

Claude Code 2.1.308: Rynhult does not pre-answer the custom API key confirmation. `customApiKeyResponses` is not documented in the official [settings reference](https://code.claude.com/docs/en/settings-reference), so confirm the key yourself in the terminal when asked. No key or key suffix is written to settings for this purpose.
