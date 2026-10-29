# Internal documentation

For people running, changing or reviewing ACC.

| Page | What it covers |
|---|---|
| [Architecture](/Internal/Architecture) | Components, how they talk, trust boundaries |
| [Components](/Internal/Components) | Each part's job, interfaces and files |
| [Data model](/Internal/Data-Model) | Tables and what they hold |
| [Configuration](/Internal/Configuration) | Settings and environment variables |
| [Security](/Internal/Security) | Auth, secrets, permissions, threat model |
| [Development](/Internal/Development) | Setup, tests, lint, the demo instance, conventions |
| [Operations](/Internal/Operations) | Install, deploy, back up, restore, runbook |
| [Containers qualification runbook](/Internal/Containers-Qualification-Runbook) | Owner-only account/build/pin/setup/collect/rollback order and qualification pitfalls |
| [Design system](/Internal/Design-System) | UI tokens, components and rules |
| [Decisions and roadmap](/Internal/Decisions-and-Roadmap) | Why it is built this way, and what's next |
| [Group Exchange](/Internal/Group-Exchange) | Group exchange publishes explicitly selected committed history |
| [Manual failover](/Internal/Manual-Failover) | PostgreSQL standby setup, explicit authority changes, crash fencing and failback |
| [Device pairing](/Internal/Device-Pairing) | Disabled LAN pairing, signer contract, transient imports and rendered policy boundaries |
| [Telemetry implementation](/Internal/Telemetry) | The main ACC owns the telemetry queue |
| [Node task ownership](/Internal/Node-Task-Ownership) | Main-created machine tasks, lifecycle owners, cancellation and leak tests |
| [Dependency updates](/Internal/Dependency-Updates) | Use this runbook when reviewing dependency PRs or replacing a downloaded tool |
| [Website](/Internal/Website) | How rynhult.dev and its /docs/ are built from `site/` and this wiki |
| [Mobile Devices](/Internal/Mobile-Devices) | This is the experimental server contract for native iOS/Android enrollment and credentials |
