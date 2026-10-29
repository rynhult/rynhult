# Isolated, authenticated live previews

Date: 2026-10-22. Status: accepted (owner approved the feature).

## Context

Agent-started dev apps execute arbitrary project JavaScript. Embedding one on ACC's origin would expose login authority and the dashboard. Reserved ports identify permitted targets but do not identify viewers.

## Decision

Use a separate origin per project and reserved port, a sandboxed iframe, and a dedicated gateway before ACC routes. A five-minute HMAC credential binds project, port, hostname and browser login; every request rechecks current authorization. Only literal loopback targets are allowed. Caddy routes the private preview namespace through the same private-network gate, reusing wildcard certificates or using its internal CA. No runtime DNS or Caddy changes are needed.

## Consequences

Private wildcard DNS and client CA trust must be provisioned during deployment. App-managed cookies and service workers are disabled; some dev servers need hostname/HMR configuration. Remote projects use the machine-link streaming protocol added below; older nodes display update guidance. Access renewal reloads the app after five minutes; already-rendered content cannot be recalled.

## Alternatives considered

A same-origin path proxy cannot safely execute project scripts. Direct port links expose unauthenticated apps and do not give a private embedded experience. Runtime publishing would mix private preview with the separate owner-approved publishing feature. A single project hostname would let tabs for different ports overwrite their target cookie, so the port is also part of the origin.


## Remote transport extension (2026-10-22)

Context: remote projects need the same browser security boundary without exposing node ports. The existing machine link already authenticates the execution machine and carries multiplexed broker channels.

Decision: add a negotiated typed preview stream family with bounded credit, queues and deadlines below terminal/control priority. The main remains the browser authorization and sandbox owner. The node independently checks literal loopback and fresh project reservation snapshots. Snapshots are expiring, rebuildable caches owned by the live authenticated link; reconnect clears them. No request is automatically replayed. The wire contract and restart/future connection-owner requirements are in `docs/PREVIEW.md`.

Consequences: no node listener or deployment routing changes. Both ends need updated code; legacy nodes retain the unavailable message with update instructions. A lost link cancels previews, and HTTP mutations may have uncertain results. Remote WebSocket messages are limited to 64 KiB. Three-second reservation leases fail closed when renewal stops.

Alternatives: direct port exposure bypasses the private gateway. A general TCP tunnel would widen the node's authority and lose typed HTTP/WebSocket bounds. A separate machine connection would duplicate authentication and complicate control-traffic priority and connection ownership.
