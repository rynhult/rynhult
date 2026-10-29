# 0059 — Dependabot and offline download pin inventory

Date: 2026-10-25

## Context

The repository already used Dependabot for Actions. Python, npm, Rust and
standalone requirements also have locks, while root installers download tools
using independently committed digests. Updating only one occurrence can leave
CI, desktop packaging or deployment on a different version. Arbitrary installer
scripts cannot safely be delegated to a package manager.

## Decision

Extend the existing native GitHub Dependabot configuration: weekly version
updates grouped by ecosystem, separate advisory-triggered security groups, no
cooldown or automatic merging. A repository administrator must enable dependency
graph, alerts and security updates; committing YAML alone cannot enable them.

Use a standard-library-only `scripts/check_pins.py` to inventory source download
pins and package artifacts without executing installers or contacting vendors.
Run it in contributor CI, release CI and pytest. Validate digest formats and
versions that must agree; fail on new unaccounted installer URLs/digest literals.
Keep unrelated environments independent: telemetry, native provider installs and
the experimental session image have separate qualification histories. The
bundled desktop main must agree with the main's Python lock.

Dynamic Caddy downloads retain the existing operator-supplied digest requirement.
Bundled CPython retains uv's embedded checksum manifest. Pending Caddy candidates
remain unqualified until reviewed architecture hashes are committed. The
inventory reports these trust sources explicitly rather than inventing hashes.

## Consequences

No dependency versions, locks, deployment behavior or runtime dependencies change.
Bot PRs still require review, lock regeneration, notices and qualification. In
particular, the Linux and Apple-silicon desktop runtime locks are regenerated with the main
lock, and `requirements-build.lock` is refreshed as a separate hashed build environment;
Dependabot's pip discovery does not recognize its nonstandard extension.
The checker detects format/drift errors; it does not authenticate upstream bytes,
scan vulnerabilities or prove that a release is safe. New download formats need
an adapter and tests. GitHub operation cannot be demonstrated in a no-remote
worktree.

## Alternatives considered

Renovate has custom managers that can propose arbitrary installer updates, but
would add an app/service and a larger configuration surface. Native Dependabot
already fits the package managers; explicit review of privileged downloads and a
local deterministic checker are sufficient for this task. A static list of
expected version/hash constants was rejected because it would duplicate source
pins and silently miss new download locations.
