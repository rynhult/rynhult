# Public source from reviewed snapshots

Date: 2026-10-22. Findings: F6 / D8 / D26.

The private repository and its history contain installation records that are unsuitable
for publication. Cleaning current fixtures does not remove historical data.

Publish only a reviewed export of committed HEAD, with private documents excluded and
an external private denylist checked against the resulting tree. Start any public
repository with that snapshot; do not import the private history. Keep historical account
migration logic for upgrades, but require explicit account assignments in the current
schema. Keep author identities and the public clone URL as maintainer decisions.

This preserves the private development record while making the publication boundary
repeatable. The denylist cannot identify unknown private information or read text in
images, so the exact exported artifact still needs human review. Publication and remote
repository creation remain separate actions. Rewriting private history with filter-repo
was considered and is unnecessary for this export workflow.

Procedure: `docs/PUBLISHING-THE-SOURCE.md`.
