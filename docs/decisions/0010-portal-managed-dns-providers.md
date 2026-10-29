# 0010 — Portal-managed DNS providers

**Status: accepted. Date: 2026-10-23. Owner approved all six questions.**

## Context

Caddy DNS-01 and Publishing currently use Cloudflare-specific plaintext files provisioned at setup. Ordinary vault Keys can reach agent sessions, so storing infrastructure tokens as ordinary project keys would not satisfy owner-only custody. DNS provider support must preserve Publishing approval, root-helper confinement and the approved but unimplemented public device-gate design.

## Decision

Adopt the [DNS providers proposal](../DNS-PROVIDERS.md): an immutable infrastructure-only vault purpose managed by owners in Keys, provider/zone bindings in Settings, and optional wizard configuration with internal CA as the no-provider default. Separate certificate and Publishing credentials. A narrow root helper renders fixed Caddy templates and restricted credential files, validates/applies a versioned generation and supports rollback; it never accepts arbitrary config, paths, modules or commands.

Distribute CI-built, pinned, checksum-verified Caddy/module release artifacts for amd64/arm64. Introduce an async Python provider protocol and ownership ledger for Publishing; refuse unmanaged records and external drift. Qualify Cloudflare, Azure DNS and Route 53 in the first wave, then Google Cloud DNS, DigitalOcean, Hetzner, Porkbun, Gandi, OVH, deSEC and RFC 2136 for both certificate and record operations. A listed provider is not yet supported.

## Consequences

Owners can rotate infrastructure credentials without giving them to agents or manually maintaining two divergent files. New vault-purpose enforcement, schemas, privileged IPC, release builds, provider qualification and migration are required. accd/root and Caddy remain trusted with the secrets they consume. Some providers cannot offer fine-grained zone/type scopes; disclose that and obtain explicit acceptance. Credential changes may require a controlled Caddy restart. DNS configuration never authorizes public ACC exposure.

The [hardening guide draft](../wiki/User-Guide/Caddy-Hardening.md) distinguishes current controls from proposals. Owner approval authorizes phase 1 implementation, including sandboxing, timeouts and log policy. Live host changes remain a separate operator deployment. The owner wants all major providers over time. Suggest providers from recognized Keys names and require owner confirmation in Settings. Phase 1 ships only Cloudflare; Azure DNS and Route 53 implementation and live qualification follow in phase 2.

## Alternatives considered

- Keep shell-managed Cloudflare files: minimal change, fails portal and portability goals.
- Reuse ordinary private/shared vault keys: existing launch delivery and grants violate infrastructure isolation.
- Install arbitrary caddy-dns modules or run xcaddy from portal requests: expands privileged supply-chain authority and makes updates irreproducible.
- Dynamic build-service downloads for every selection: retain only the existing independent-hash escape hatch; prefer immutable reviewed artifacts.
- One broad token for both consumers: simpler, greater blast radius; require separate tokens by default.
- Delegate all DNS to a Go/libdns helper: possible later, but adds a cross-language runtime and credential boundary; Python adapters fit the current Publishing service.
