# Private workspace records

Renumbered on 2026-10-24 from 0004 to 0025.

Date: 2026-10-22. Owner approved the feature; design: `docs/PRIVATE-WORKSPACES.md`.

## Context

Research workspaces share an account-level built-in project. Private development needs separate worktrees, ports, vault grants, compose approval and git backup without creating a remote repository.

## Decision

Represent each Development workspace with an individual Project record and a `private_workspace` marker. Reuse project capability APIs and UI, list it under Workspaces, and enforce an unconditional frozen `local_only` session policy. Conversion requires stopped sessions; older sessions cannot resume with their permissive policy. Explicit promotion clears the marker on the same record and preserves its original backup identity when its slug changes.

## Consequences

Existing research/council behavior stays intact. Private workspaces receive the same ownership checks and compose review boundary as projects. Agent push restrictions do not interfere with ACC's internal backup protocol. Existing Development sessions remain local-only after promotion; new project sessions use normal project policy.

## Alternatives

Adding every project capability to a second workspace entity would duplicate authorization, storage and API paths. Treating every repository without a remote as private would change ordinary projects and blur the explicit promotion boundary.
