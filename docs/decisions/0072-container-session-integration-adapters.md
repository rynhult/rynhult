# 0072: Gated container credential and HTTPS adapters

Date: 2026-10-25. Status: accepted implementation slice; real qualification pending.

Context: phase 2 needs provider sign-ins and Internet egress without mounting an account home, sharing refresh
state or relaxing the phase-1 loopback-only container boundary. This work must be testable without root/runtime
access. Release scope follows decision 0073: the Linux server used from browsers, with Linux, macOS and WSL nodes.
These adapters do not qualify native desktop applications or Windows-native nodes.

Decision: require the existing container gates plus explicit root-owned pinned-image adapter policy. Copy only
selected access credentials through a broker-authenticated, generation-bound one-shot bootstrap into a sealed,
read-only memfd mount. Refuse unsupported refresh layouts and never copy credentials back. Preserve immutable
image hooks. Keep `network=none`; optionally offer HTTPS CONNECT through a session-only broker gateway reached
by an image-local loopback bridge. Validate all DNS results, pin numeric dials and deny non-public/transition
addresses and explicit host/control-plane exclusions. Close the gateway on stop or failure; broker loss fails
closed and requires session recreation.

Consequences: narrow, fake-tested integrations without new API/sudoers/runtime socket authority. Proxy-ignoring
clients, non-HTTPS transports, local-service grants, transparent refresh and automatic reconnect are unsupported
in this slice. The owner must qualify the rebuilt/pinned image, provider auth/hooks, transport/mounts, exclusions,
isolation and lifecycle before any rollout. Defaults remain off.

Alternatives: direct slirp/pasta would require a separately qualified mandatory network filter and changing the
Option A network gate. Whole-home or writable shared auth mounts violate the credential boundary. Separate session
login remains an option for unsupported providers; no unqualified fallback is selected automatically.
