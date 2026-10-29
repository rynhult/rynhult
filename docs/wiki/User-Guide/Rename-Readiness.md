# Check readiness for the internal Rynhult rename

Owners can preview the proposed internal naming transition without changing an installation. This planning tool
is separate from normal updates; it does not migrate a host or approve an update. Existing paths and settings
continue to work as before.

Environment-name compatibility (phase A) and additive public command/service
aliases (phase B) are already implemented. This read-only tool does not install
those aliases or perform the later path/default/label cutovers. Use
[Updating Rynhult](/User-Guide/Updating#add-the-public-names-without-a-software-update)
for reviewed alias-only deployment and rollback. Real-host rollout qualification
is separate from an offline plan.

## Before you start

Use a source checkout with Python 3.12 or newer and an offline host filesystem tree supplied by the installation
owner. The examples use a fictional snapshot at `/tmp/example-host`. Read access to configuration is needed;
configuration values are never printed. Reports can contain local paths and account names, so review them before sharing.

## Preview the plan

1. From the checkout, run:

   ```sh
   python3 scripts/rename_stage2_dryrun.py --root /tmp/example-host
   ```

2. For JSON output, add `--json`. To inspect an extra configuration file inside the host tree, add
   `--env-file /custom/node.env`. Repeat that option for additional files. A path beginning with `/` refers
   to the snapshot's root, not the computer running the tool.
3. Review each action. `compat-path`, `unit-alias` and `env-fallback` describe proposed compatibility additions.
   `unit-alias-present` means a checked link already points at the legacy unit; both names share one job.
   `keep` means preserve the existing object. `blocked` requires an owner to resolve a conflict or missing evidence.
   Platform cutovers and embedded references require separate review.

No files, service state, enrollment or [sessions](/User-Guide/Glossary#session) are changed. There is no apply option. The report deliberately
keeps database filenames and browser cookies stable. A successful report means only that the inspected files had
no detected blocker; loaded services, live sessions, permissions and custom integrations still need review.

## Troubleshooting

- **Exit status 2:** review `blocked` findings or the argument error. Both old and new paths, conflicting keys,
  malformed files, missing explicit files, and unsupported file types need attention before any future migration.
- **Symlink not followed:** the tool avoids following links out of a snapshot. Even normal platform links can
  produce this finding. Have the owner verify their targets; do not replace them to silence the report.
- **No detected identifiers:** check that the selected directory is the host filesystem root. This result does
  not prove that a custom installation or running service is absent.
- **Windows or Mac jobs:** filesystem findings are only part of the picture. Native job/task state must be checked
  separately, and actual label changes wait until the [node](/User-Guide/Glossary#machine-node)'s sessions are drained.

Continue using the normal [update procedure](/User-Guide/Updating) for released updates. Phases A and B
are additive; future path, default and native job-label cutovers remain separate work, not a setup requirement.
