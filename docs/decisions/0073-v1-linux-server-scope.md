# v1.0 is the self-hosted Linux server

Date: 2026-10-27. Status: accepted by the owner.

## Context

Native platform work adds build, security, packaging, signing and device-testing
cost alongside the server release. Existing desktop and mobile code on main is
not the same as a supported native application. The earlier plan to start native
Windows immediately after rc.1 no longer describes the owner's release plan.

## Decision

Rynhult v1.0 is the **self-hosted Linux server**, used through a web browser on
any client platform, including the phone web app installed from the browser.
The current qualified main-server distributions remain Ubuntu 24.04+ and
Debian 13+ with systemd; this decision does not qualify other distributions or
promise that every browser has completed device/accessibility testing.

Existing nodes remain supported with their current capabilities and limits:
Linux nodes, the macOS node, and Windows nodes running Linux through WSL.
A Mac node is not the macOS standalone desktop app; a WSL node is not native
Windows. A Windows main running inside WSL remains an existing setup path, not
an additional v1 main-server qualification target.

Native applications come **after v1.0**, one platform at a time to reduce total
cost. The order is not chosen yet; the candidates are native Windows, the macOS
standalone desktop app, the Linux desktop app, iOS and Android. For native
Windows, Codex is the first provider (owner answer, Oct 27).

This replaces the native-Windows-after-rc.1 schedule. Shipping rc.1 does not
start that native-platform sequence. No completion dates are promised.

## Consequences

- Release notes, readiness checks and the Guide distinguish the Linux main,
  supported nodes, browser/phone-web access and later native applications.
- Existing desktop/mobile implementation, libraries, server-side enrollment
  contracts, probes and qualification evidence stay on main. They are
  experimental post-v1 work, not v1 application deliverables or proof of a
  qualified native release. This decision changes no code or feature flag.
- Native app compilation, packaging, signing, store distribution and physical
  device qualification do not block v1. Existing server, browser, phone-web and
  supported-node regression checks still apply.
- Current Preview/off-by-default boundaries, node security limitations, RC
  installer availability, release gates and owner publication approval remain
  unchanged. Choosing the v1 scope does not publish or tag a release.

## Alternatives considered

Continuing native Windows immediately after rc.1, or qualifying all desktop and
mobile apps for v1, would keep several platform workstreams and their recurring
costs open. Dropping current Mac/WSL nodes would remove working functionality
unnecessarily. The Linux-server/browser release with existing nodes preserves
that functionality while deferring new native delivery serially.
