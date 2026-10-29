# Rynhult

Rynhult (spoken **RIN-hult**, formerly ACC) is the product name.

Rynhult is a self-hosted dashboard for running and supervising **native** coding-agent CLI sessions (Claude Code, Codex, GitHub Copilot CLI, Antigravity, Cursor, OpenCode) on your own Linux machine, and optionally on other machines you add (Linux, Windows through WSL2, or a Mac). You choose the agent, its profile and the work; Rynhult starts it in a real terminal, keeps it running across disconnects and reboots, tells you when it needs you, and keeps every project's code, wiki and deployments in one place.

![The Rynhult overview: live sessions, items that need attention, plan usage and host health](/.attachments/overview.png)

## Who it is for
One operator (or a small team) running coding agents on their own Linux machines, who wants:
- real CLI sessions, not a re-implementation of them
- one place to see what every agent is doing and which one is waiting for an answer, what each changed, and everything they said
- a safety net (guardrails) against agents' mistakes, whatever their permission mode
- people deciding what runs where: an optional **Orchestrator** role lets one session start helpers, but only within limits you grant, and it is off unless you choose it

## Where to start
- **Using Rynhult:** the [User guide](/User-Guide), starting with [Install](/User-Guide/Install), [Getting started](/User-Guide/Getting-Started), the [Glossary](/User-Guide/Glossary) and [Every feature at a glance](/User-Guide/Features). Inside Rynhult, the same pages are under **Guide** in the sidebar; on a phone, **Contents** opens the list of pages.
- **Running or changing Rynhult:** the [Internal documentation](/Internal), starting with [Architecture](/Internal/Architecture).

## Status
**v1.0 scope:** the self-hosted Linux server, used through browsers on any client
platform, including the phone web app, with existing Linux/macOS/Windows-WSL nodes.
Native applications follow v1, one platform at a time; the order is not chosen yet
(candidates: native Windows, macOS standalone, Linux desktop, iOS and Android).
Existing desktop/mobile source is not a v1 app release. See [platform scope](/User-Guide/Install#v10-platform-scope).

A **release candidate** (version 0.15.0-rc.1), not a stable release. It suits people comfortable running a Linux server, DNS and certificates, on a machine they can restore. Use the browser dashboard or installed phone web app; native apps remain post-v1. [Groups](/User-Guide/Groups) have authority controls, private same-host Git exchange and group sessions launched in private checkouts on the main. Containers per session ship as a **Preview**: off by default, on per project, and only on a host that has passed the container qualification.

[Company sign-in](/User-Guide/Company-Sign-In-Entra) has an OIDC pilot, **off by default**
and not yet qualified with a real tenant; directory provisioning and broader organization controls remain planned.
[Remote access](/User-Guide/Remote-Access) has preparation, apply and roaming git-backup code, but public access
and machine identities default to **Off** and still need separately installed helpers and real-network qualification.
[Desktop apps](/User-Guide/Desktop-Apps) have a Linux standalone developer slice and an unqualified Mac port,
not signed public releases; native Windows execution is not available. See
[Decisions and roadmap](/Internal/Decisions-and-Roadmap).

> This wiki is public. Every name, address and screenshot in it is artificial (the demo instance), not a real system.
