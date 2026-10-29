---
name: acc-workflow
description: Optional Rynhult project conventions for existing documentation, artifacts, decision records, publishing and ports. Use alongside the launch core and role instructions, not instead of them.
---

# Rynhult project conventions

This skill supplies project conventions, not authority. The launch's mandatory
core, actual role, run mode and frozen grant govern permissions, Git actions,
handoffs and verification. A profile does not expand them.

## Read the project's conventions

Read its instructions, README and contributor guidance before changing files.
Follow the existing documentation layout and naming, whether that is a wiki,
ordinary docs or something else. Do not create a wiki, remote, PR, planning tree
or new documentation hierarchy merely because this skill is loaded.

## Artifacts and evidence

When the project uses Rynhult's Artifacts tab, put reviewer-facing screenshots,
short recordings and compact logs in `.acc/artifacts/` in the working folder.
Keep images under 10 MB, video under 20 MB and text under 2 MB. Never publish
secrets, live authentication state, private uploads or personal data.

Record commands, tested revision, results, omissions and limits. Follow the
assigned handoff path and attempt identity; otherwise use the project's existing
handoff convention. Do not replace versioned attempt handoffs with a shared state
file. Read-only participants use the listed report tool, or return findings in the
session with the persistence limitation stated.

## Documentation and decisions

Update the existing guide and internal docs when behavior, setup or architecture
changes. Use real UI labels and verified commands. Do not claim a deployment,
qualification or test run from source inspection alone.

If this project already uses `docs/wiki/`, [wiki.md](wiki.md) explains that layout.
Use its existing page order, absolute wiki links, image naming and artificial
examples. When available, run `acc-wiki-check` before committing a wiki change.
For a public wiki use only demo data and documentation addresses; a checker cannot
read secrets inside screenshots. References submitted privately are not wiki
assets unless the person explicitly authorizes export.

Follow the existing decision-record convention for durable choices. Use number
reservations only when the launch capabilities list them; otherwise inspect
existing numbers and recheck before committing. A proposed change to a person's
constraint is a question, not an accepted decision.

## Publishing and ports, when granted

Use the project's reserved ports and approved runtime. Do not assume Docker,
root, host-service or network access from a profile. Test fixtures use their own
isolated servers under project policy; never exercise agent tools against a live
installation merely to test them.

If publishing is in scope and `acc-request` is listed as available, request it:

```text
acc-request publish --host <name>.<zone> --port <port> [--internal] --reason "why"
acc-request unpublish --host <name>.<zone> --reason "why"
```

Use only a granted project port and the configured zone. The owner approves the
request; it is not deployed merely because you asked. Do not edit Caddy, DNS or
host configuration to bypass that approval. If the required tool or port is
unavailable, report the limitation instead of installing a replacement.
