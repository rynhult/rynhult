# Tools on approval (M16)

Owner's rule, 2026-10-22: **Existing tools should be usable, new tools should
require approval. Approval should only be required for installation or changes.**

## Boundary

`acc-guard` decides from its existing shell parser, after wrappers, directory
changes, substitutions and nested shells have been read. It does not use a list
of permitted executables: running an installed program never needs tool approval.
The other guardrails still apply. This is a safety net, not a sandbox; scripts,
symlinks, arbitrary interpreters and a determined account can evade text checks.

These are tool changes:

| Form | Approval boundary |
| --- | --- |
| apt / apt-get, dpkg, snap, flatpak | Install, remove, purge, upgrade, refresh and package database/repository changes. Read-only search, list, policy and version commands are not changes. |
| npm / pnpm / yarn | Global installs, add, update, upgrade, remove, link and explicit prefixes outside the workspace. Local dependencies are allowed only inside the session workspace. |
| pip / python -m pip | Install, uninstall outside a project virtualenv, `--user`, and external target/prefix installs. A virtualenv's interpreter/pip inside the workspace or an explicitly activated workspace venv is project-local. |
| pipx / uv tool | Install, inject, upgrade, uninstall and environment changes. `uv pip` follows pip's boundary; `uv sync`, project venvs and ordinary project builds are local. |
| cargo install / uninstall, go install, gem install / update / uninstall, brew | Account or machine tools; explicit cargo/gem destinations inside the workspace are local. `cargo build`, `go build` and bundle dependencies are local. |
| Installer scripts / binaries | curl or wget piped to a shell (including process substitution); executable permission changes, copies, moves, links, downloads and writes into PATH folders (`~/.local/bin`, `~/bin`, `/usr/local/bin`, `/usr/bin`, etc.). |

Unknown/dynamic destinations are not proven project-local. Options are checked
wherever the manager accepts them, including combined global flags and `=value`.
Quoted text and search patterns are not installations. Corpus rows name each
manager's changes, safe counterparts and representative wrappers.

## Request and authorization

The refusal tells the agent:

```
acc-request tool --manager npm --name prettier --version 3.6.2 --reason "Format this project's output"
```

`--version` is optional. `--action install|remove` defaults to install; installing
an already installed name is also the upgrade/change path. Removal from the
Tools list creates the same approval request, not an immediate deletion.

Supported registry managers: apt, snap, flatpak, npm, pnpm, yarn, pip, pipx, uv,
cargo, go, gem and brew. apt/snap/flatpak are system-wide; others install into
the session person's agent account. dpkg files and arbitrary installers are
detected but cannot be submitted: ask for the corresponding registry package,
or ask an owner to handle it outside ACC. There is no arbitrary-command escape.
Manager-specific names and exact versions are bounded and validated at the CLI,
server, broker/node and helper boundaries. No URL, filesystem path, option,
version range, script, shell text, custom registry or caller-supplied executable
is accepted. Go module import names are registry coordinates, not local paths.

An agent request derives its person, account, machine and project from the
session, never the event's claims. A member requests and approves only their own
account's tools. Only owners may request system-wide changes; only owners may
approve them. Owners may approve any request. Approval rechecks the current
person/account/role, including after transfers or account changes.

The Inbox item shows action, manager, package, requested version (or latest),
machine, account/system destination and reason. Approve and Reject are explicit;
acknowledgment is not approval. System requests go to the owners' Inbox; account
requests go to their person. Audit actions are `tool.request`, `tool.approve`,
`tool.reject`, `tool.result` (including failures).

## Execution and durable state

The server stores a `tool_request` row before creating its attention item.
Decisions atomically claim a pending request; repeated decisions cannot execute
twice. Approved work runs asynchronously with bounded timeouts. Interrupted
work is failed visibly after server restart, never replayed automatically.

System changes use `acc-packages`' existing atomic request/result file pattern:
`action: tool`, with validated manager/name/version/operation fields and no
other inputs. The root helper constructs fixed argv and revalidates every field.
It never runs a user manager or a supplied script as root. Existing fixed
agent-software catalog actions keep their existing behavior.

For another Linux/WSL machine the main sends a bounded `tool` frame on its
authenticated link. acc-node validates it and writes that machine's helper
request; a matching `tool-result` reports the result. IDs correlate replies to
the live link/request; late, unsolicited or mismatched replies cannot apply
another machine's result. Disconnect/timeout fails visibly. Macs reject
system-wide managers rather than falling back to privileged execution.

User changes use a separate broker connection to the session's agent account
and host (the existing node channel transport works unchanged). The broker
refuses root and services accounts, validates again, supplies fixed account
destinations/environment and executes argv without a shell. Package lifecycle
code, when a manager needs it, runs only as that account, never root.

After install, query that manager for the actual installed version; a successful
process without a resolved version is a failure, not a guessed pin. Store the
name and resolved version in a durable `installed_tool` row keyed by machine,
account/system, manager and name. Removal verifies absence before marking it
removed. Providers has a Tools list (machine/account, version, provenance and
Remove); members see their own account only. Failed work stays visible in
request history and the Inbox. No automatic upgrades for these tools.

## Approval setting and instructions

Default on. Owners can disable approval for a person or project using scoped
settings. Either explicit exemption disables only the tool-change rule;
force-push, secrets, deletes and custom rules are unaffected. Like existing
guardrails the effective setting is frozen into a new session's launch policy;
start a new session to change it. Exempt direct commands simply run and are not
inventoried by ACC. Structured requests still validate and authorize their
destination; an exemption does not grant members system privileges.

Every session's instructions include one short paragraph: use installed tools
freely; project-local dependencies are allowed; request account/machine tool
changes with `acc-request tool`, wait for the Inbox decision, and never work
around a refusal.

## Verification

Tests execute guard corpus rows as data, CLI validation, manager argv/version
resolution with fake executables, helper rejection before subprocess work,
member/owner decisions and scoped settings, durable install/removal/result
history, and node correlation/refusal/disconnect behavior. Playwright exercises
Inbox approve/reject and the Tools list with artificial demo data. Tests never
install software on the test machine.
