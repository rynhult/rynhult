# Catalog restore

Owner approved, 2026-10-22. Restore repairs catalog content, without resetting personal configuration.

## Existing states

`CatalogStore.remove` refuses built-ins; assigning an item to fewer profiles writes a profile change
(`remove.skills`, etc.). Old whole-profile overrides also exist. These exclusions must survive restore:
show excluded profiles and direct owners to Profiles to opt back in. Restore never silently re-enables a tool.
Custom items live in `catalog-local/local.json`, with skill/agent files alongside it. The current loader
rejects a custom id that a later release adds to the built-ins. Restore must instead preserve that custom
item. Built-in source files can be missing/modified through an incomplete install or manual changes;
new releases can change or add manifest entries. Restore from this version uses the installed catalog,
so cannot reconstruct bytes missing from that source: report the error and suggest the verified release.

## Sources and layering

Installed `catalog/` stays read-only. Add `catalog-local/restored/` with immutable generations, a small
atomic `active.json` pointer and item metadata/files. Load installed built-ins, then restored built-ins,
then custom items, then unchanged profile overlays. Skill paths and broker links must resolve this layer.
Profiles, deferred entries and global policy always come from the installed version; restore handles
skills, agents and MCP entries only. Restored items retain source version and builtin identity.
Local ids win collisions (including ids newly introduced by an ACC upgrade). Preview reports collision;
Restore all skips it. An explicit alternate unused id restores a separate built-in copy, preserving the
custom item's bytes, metadata and profile references. No automatic rename or deletion.

Latest release uses the configured Updates repository and optional token. Extract the updater's asset
fetch/checksum/unpack/build verification into shared code and call it from both paths. Resolve only the
latest stable GitHub release, never branch files; validate asset URLs, SHA256SUMS and the entire archive.
Throttle latest-source checks, including failures. Temporary downloads are removed after preview.

## Preview and apply

Owner-only endpoints produce an expiring server-held preview with source/version, per-item new,
missing, changed or unchanged status, text diffs, risk flags, compatibility refusals, collisions and
profile exclusions. Unknown providers or unknown/unreadable frontmatter refuse an item. Reuse import
risk review; red flags require explicit acceptance, as imports do. Metadata diff excludes MCP env values;
never expose existing secrets. Preserve existing built-in MCP env/vault references when replacing content.

Apply one or all eligible items from the reviewed snapshot. Compare a state fingerprint to refuse stale
previews. Serialize writes, stage a complete generation, atomically switch the pointer, reload the whole
catalog and undo the pointer on any load failure. Never write installed sources or local.json. Orphaned
failed generations are removed. Audit `catalog.restore` with ids, source and version after success.

## UI and verification

Catalog owners get Restore built-ins, source selection, preview rows with per-item Restore, Restore all,
text diffs, explicit risk acceptance, alternate-id choice for collisions and a result. Use shared design
primitives and accessible loading/error/status states. Guide explains profile exclusions and collisions.
Tests cover missing/stale/new items, single/all, custom preservation, collisions, incompatibility, verified
release failures, rollback, stale previews, ownership/audit/rate limiting, broker resolution and Playwright.
