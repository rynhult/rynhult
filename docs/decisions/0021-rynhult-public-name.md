# Rynhult public name, stage 1

Date: 2026-10-24. Status: accepted by the owner.

## Context

The product has a new public identity: Rynhult, spoken RIN-hult. Existing installations
and automation must continue to work after upgrading. A persistent identifier migration
would require a separate rollout and is outside this stage.

## Decision

Use Rynhult in product text, the README and Guide, setup, notifications, authenticator
labels, installer display names and distribution metadata. Public commands are
`rynhult`, `rynhultd`, `rynhult-node` and `rynhult-guard`; legacy `acc*` commands remain
available. Node installers accept either enrollment-command spelling. Installer files
use `Rynhult-Setup-<version>.exe` and `Rynhult-<version>.pkg`.

Keep the Python import package `acc`, `ACC_*` variables, existing filesystem paths,
systemd units, launchd labels, Windows installation identities and task names,
databases, tmux sockets, API paths, cookies, headers and DNS ownership markers.
Only new authenticator enrollment labels change; stored secrets and existing codes
continue working.

The site is rynhult.dev, docs are docs.rynhult.dev, private security reports go to
security@rynhult.dev, and general contact goes to contact@rynhult.dev. The existing
telemetry default is already https://telemetry.rynhult.dev; it remains reserved and
undeployed. Consent defaults stay off. The npm CLI name is `@rynhult/cli`; this tree
has no npm CLI implementation, so no placeholder package is introduced or published.
The initial stage retained the old source and update repository URLs. The publication
readiness refresh in [decision 0050](0052-rynhult-publication-readiness.md) supersedes
that choice with the owner-specified intended repository; it does not publish it.

## Consequences

People use the new name while existing scripts, hooks, service definitions and data
continue to work. Some technical instructions still name compatibility paths or
commands. A later stage may migrate these identifiers with an explicit upgrade plan.
The Python distribution is named `rynhult` and still packages `acc`; its wheel includes
the existing guard script so both guard entry points use the same isolated execution.

## Alternatives considered

Renaming persistent identifiers now would risk breaking upgrades and violates the
stage boundary. Removing legacy commands would break automation. Creating an empty
npm CLI package would claim a deliverable that does not exist.

## Visible identity

Stage 1 uses only the supplied favicon, PWA/Apple icon exports and standalone mark
from the owner’s Rynhult brand kit. The compact slots use the standalone mark with
at least half its visible height in clear space. Light ink is shown on dark surfaces
and dark ink on light surfaces. The existing layout and application color tokens
are retained; the palette adoption is a separate design pass. The old icon generator
now validates the approved exports without overwriting their artwork.
