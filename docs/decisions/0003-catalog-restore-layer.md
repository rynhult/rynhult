# Restore built-ins through a writable layer

Owner approved 2026-10-22; detailed design: [Catalog restore](../CATALOG-RESTORE.md).

## Context
Installed ACC source is root-owned. Dashboard custom items and profile changes share a writable overlay.
Copying release items into either source or the custom overlay would overwrite protected configuration or
lose the distinction between a restored built-in and a personal item.

## Decision
Use immutable generations under the writable catalog's `restored/` directory and an atomic active pointer.
Load installed items, restored built-ins, custom items, then unchanged profile overlays. Custom IDs always
win, including IDs newly introduced in an ACC upgrade. A collision requires an explicit alternate unused ID
for a separate built-in copy. Use the updater's shared verified stable-release download path.

## Consequences
A whole-catalog validation failure restores the old pointer. Broker skill links resolve the readable
pointer without reading secret-bearing metadata. Keep previous successful generations for running links;
failed generations are removed. Restore never reenables a profile exclusion or rewrites MCP references.
An installed source missing its own files requires a verified release or an installation repair.

## Alternatives considered
Writing the installed source requires privileges and changes installation identity. Replacing local.json
would mix built-ins with custom items and risk profile and secret loss. Resetting whole profiles would
silently discard choices; profile membership remains a separate explicit edit.
