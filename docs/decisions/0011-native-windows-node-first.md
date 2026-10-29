# Native Windows node before a native main

Date: 2026-10-23. **Status: proposed for owner approval; not approved or implemented.**

## Context

The owner asks whether ACC can run natively on Windows without WSL. Both current
Windows installers run ACC inside Ubuntu/WSL2. The node's outbound TLS WebSocket
is portable, but brokers rely on tmux, Unix PTYs, peer UIDs, POSIX file handling
and shell hooks. Native provider binaries alone do not port those boundaries.

The approved container direction and native exception policy (private decision record)
remain in force. A Windows account and Job object do not supply the planned
per-session Linux container boundary.

## Proposed decision

Approve a gated native Windows **node** feasibility phase before any native main.
Use dedicated local users and per-account services, a persistent ConPTY terminal
host with ACC-owned screen/scrollback/reattach, local ACL-protected named pipes
with kernel client identity, and a separate constrained privileged helper.
Retain node wire authentication and the Linux/WSL main. Qualify Claude, Codex and
Copilot first; enable each provider only after real native hooks, guard denial,
login and same-conversation resume pass. Port PowerShell/cmd command checks
explicitly; do not reuse the POSIX parser as a Windows security claim.

Native sessions require an owner-only project exception and **Not isolated**
label. No silent fallback, desktop-owner execution, administrator accd, or
container-equivalent claim. Organization policy may prohibit the mode. Optional
AppContainer/Windows Sandbox work and a native main require separate decisions.

The full component contracts, official provider evidence, estimates, acceptance
gates and approval questions are in [WINDOWS-NATIVE](../WINDOWS-NATIVE.md).
Phase 0 is 1–2 engineer-weeks; first-cohort node total 10–16; remaining providers
add 3–5; native main adds 6–10, subject to fresh approval. These are estimates,
not authorization or a delivery commitment.

## Consequences

Native Windows toolchains become possible without a Linux distribution. ACC
must maintain a terminal host, Windows identity/ACL/IPC and shell-policy adapters,
signed release artifacts and Windows regression infrastructure. Same-account
sessions still share authority. Linux-only tools and the approved Linux container
runtime are unavailable natively. A failed mandatory guard/authentication gate
blocks release; it does not justify weakening protection. Windows machine and
provider testing are outstanding, not demonstrated by this design.

## Alternatives considered

- Keep WSL2: smallest change and compatible with Linux toolchains/container plan;
  retain as the supported option while native work is unqualified.
- Native main and node together: larger control-plane/helper/backup/TLS scope,
  delaying useful Windows workload evidence.
- Desktop-owner node: easier prototype but exposes desktop credentials and node
  identity to agents; rejected for the proposed release.
- Third-party multiplexer: evaluate in the spike, but require proven unattended
  service/reattach/control behavior; Windows Terminal tabs are insufficient.
- AF_UNIX: possible IPC alternative, but Linux peer credentials and Python
  asyncio behavior cannot be assumed on Windows.
- AppContainer or Windows Sandbox: promising separate isolation research, not a
  transparent replacement for arbitrary development sessions or Linux images.
