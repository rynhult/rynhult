# Vendored skills (repo-scoped, for work on ACC itself)

Markdown only, pinned to a commit, licence files kept. Each file was read in full before vendoring. No hooks, scripts or installers are used.

| Skill | Source | Commit | Licence | Vendored |
|---|---|---|---|---|
| `frontend-design` | github.com/anthropics/skills `skills/frontend-design` | `dbd4588f9e1033efb41dad4bef2f7947c8993d44` (2026-10-09) | Apache-2.0 | 2026-10-18, unmodified |
| `web-interface-guidelines` | github.com/vercel-labs/web-interface-guidelines `command.md` | `434b7f91364665f2f733b310ec54809bf8f37937` (2026-10-05) | MIT | 2026-10-18, wrapped as SKILL.md: frontmatter added, the `$ARGUMENTS` command header dropped, rules unchanged |

**Not used:** Vercel's `install.sh`. It fetches unpinned files from `main` into several tools' config directories.

**Location:** the files now live in `catalog/skills/` (the ACC catalog); these entries are symlinks. Update them through the catalog (see `catalog/manifest.toml`).
