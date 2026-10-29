# Vault-backed issue sources

Renumbered on 2026-10-24 from 0004 to 0028.

Owner approved 2026-10-22; design: [Issue sources](../ISSUE-SOURCES.md).

## Context
GitHub's owner-only snapshot cannot safely supply member credentials or represent a self-hosted GitLab remote.
An issue description is untrusted content, not an agent instruction.

## Decision
Use server-side GitLab REST and Linear GraphQL adapters behind project ownership checks. Connections hold only
project-granted vault ids. Mark those grants server-only, including after disconnection; refuse their use as MCP
credentials in that project. Fetch issue details on preview and launch, enclosing them as untrusted JSON data.
Use per-token request budgets, backoff and a bounded 30-second cache.

## Consequences
Source keys do not reach new session environments. Previously running sessions retain keys already delivered:
use dedicated source keys and stop/resume existing sessions after changing a key's purpose. Removing a connection
does not silently convert a server credential into an agent credential. Explicitly removing and giving a key again
resets the grant's purpose. PR-link comments require a project opt-in and a session's own worktree branch.
Mark a send durably before the mutation: a crash or ambiguous failure may require a manual comment, but cannot
cause automatic duplicates.

## Alternatives considered
Plain settings tokens bypass vault permissions and redaction. Client-side calls expose credentials. Giving agents
source tokens makes accidental writes possible. Retrying an ambiguous comment risks duplicates without a provider
idempotency key.
