# Publishing address inventory

Renumbered on 2026-10-24 from 0011 to 0035.

Date: 2026-10-23

## Context

One environment origin per access level cannot represent a machine with multiple public IPs or a routed edge block. Address choices must stay independent of the proposed DNS provider adapters and retain approval and port-reservation checks.

## Decision

Store a bounded, instance-wide owner inventory in the existing `Setting` row `publishing.addresses`. Each entry has an ID, label, address/network, public/internal access, default and environment/owner provenance. Bootstrap once from the origin variables and pin existing publications in `PublishRequest.context.target_ip`; new requests pin their choice at creation. Defaults never retarget existing requests. Every mutation is audited.

Blocks are /16 or smaller for IPv4 and /48 or smaller for IPv6, with a text host picker instead of enumeration. Public validation excludes non-global unicast and special-purpose ranges. Reject overlaps across both lists. Applied, applying and approved requests reserve their target entries against removal. A failed request with a possibly retained DNS record also reserves its target until a later successful operation on that hostname. Successful rollback of a newly created record clears its record ID and reservation. Pending requests do not reserve an entry and fail the apply membership recheck if it is removed.

Address membership is checked in accd before DNS mutation in the existing approval-to-root path. Root continues to accept only structured routing requests and render fixed Caddy templates. It does not make DNS calls or read a second inventory snapshot: accd owns both approval and DNS, and the existing root helper already trusts accd's approvals. A forged request from compromised accd is outside this boundary, as documented in the existing helper.

## Consequences

No new schema or provider-specific address store. Cloudflare writes A or AAAA based on the pinned host address; a family change updates the same managed record. Detection is a read-only suggestion from two fixed HTTPS services and local interfaces, never proof of ownership. Router/firewall reachability remains an operator responsibility. Existing DNS-update rollback limitations are unchanged and documented in DNS-PROVIDERS.md.

## Alternatives

Reading origin variables on every publish would silently change defaults and lose per-site intent. Expanding blocks into dropdowns is impractical for IPv6. Duplicating the inventory in root would create divergent policy without authenticating an owner against a compromised accd.
