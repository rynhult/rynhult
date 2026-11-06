# Personal layout storage (P2 foundation)

This implements the persistence boundary in [the continuity contract](continuity-contract.md), S03, AC-016 and AC-024. The new shell's working columns save through this API (P2b). It does not change today's browser-scoped Cards/List choices.

## API contract

A signed-in person can use these routes; installation bearer tokens receive 403 and anonymous requests receive 401. There is no person selector. Even an owner reads and changes only their own layout. All routes also check current project access, returning 404 for an inaccessible project. Private workspaces already have `Project` records (`private_workspace`); use that same stable numeric ID, never a folder, slug or display name. Group projects are outside this personal API.

| Method | Route | Body |
|---|---|---|
| GET | `/api/projects/{project_id}/layouts/{device}/{viewport}` | None |
| PUT | Same | `{expected_revision, layout}` |
| POST | Same plus `/reset` | `{expected_revision}` |
| POST | `/api/projects/{project_id}/layouts/file-refs` | `{location, id, folder?, path}` |

`device` is `desktop`, `tablet` or `phone`; `viewport` is `wide`, `medium` or `narrow`. These are preference classes, not browser fingerprints. There are at most nine records per person/project. GET returns `{scope, revision, schema_version, layout, resources}`. An absent record has revision zero and null layout. Saves and resets use atomic compare-and-swap; a stale request receives **409** with `{detail, current}` containing the current full record. No force-write endpoint exists. A reset saves a null-layout tombstone at the next revision, preventing stale devices from recreating a pre-reset record. Only successful explicit resets audit (`layout.reset`), with project ID, device, viewport and revision; saves/reads produce no layout audit events or content logs.

`scope` contains installation UUID, opaque person UUID, project ID and device/viewport. The installation UUID reuses `organizations.namespace`, lazily initialized in a serialized database transaction. The layout person UUID derives from that namespace and the permanently reserved username. It stays stable when an external sign-in is bound later and cannot inherit a reused integer user ID. This is a layout cache identity, not an authorization credential or a replacement for Organizations person IDs. The database implicitly supplies the installation boundary; restoring its backup preserves that identity.

## Schema version 2

Requests are streamed with a **32 KiB** total bound before JSON parsing. Saves are version 2 only; version 1 and unknown versions are refused with 422 without changing the record, because an older client must reload rather than write a shape the server would guess at. A layout permits at most **32** views and **3** columns:

```json
{
  "schema_version": 2,
  "views": [
    {"id": "t-stable-session-id", "kind": "terminal", "resource": {"kind": "session", "id": "stable-session-id"}},
    {"id": "f-opaqueReference", "kind": "file", "file_ref": "opaqueReference"},
    {"id": "pp-12-preview", "kind": "page", "resource": {"kind": "project", "id": 12}, "tab": "preview"}
  ],
  "columns": [{"tabs": ["t-stable-session-id", "f-opaqueReference"], "active": "f-opaqueReference", "width": 0.6},
              {"tabs": ["pp-12-preview"], "active": "pp-12-preview", "width": 0.4}],
  "focused": 0, "filled": null,
  "sidebar": {"collapsed": false}, "header": {"compact": true}
}
```

View IDs are unique ASCII identifiers (letters, digits, `_`, `-`, 1–64 characters). `kind` is `terminal`, `chat`, `file` or `page`. Terminal and chat views name a session. A page may name a `project`, `session` or `team` resource and a tab (`[a-z-]`, at most 32 characters). Every view sits in exactly one column; each column's `active` is one of its tabs, or null only when it has none; widths are in (0, 1] and sum to 1; `focused` is a column index; `filled` is null or a placed view. There are no names, paths, URLs, arbitrary text, draft/editor bodies, transcripts, credentials, grants or delivery receipts. Unknown fields, including nested fields, are **discarded**, never persisted or echoed. Extensions needing new semantics must update this closed schema, not add an opaque extension bag.

**File references.** A layout never holds a path, and a view ID never encodes one. A file view holds `file_ref`, an opaque random identifier in `layout_file_ref`, the only place the path is kept. POST `file-refs` checks current project access (and that a session location belongs to the project), and returns the same reference for the same person and file. A path is kept only while a saved layout uses it: every accepted save removes that person's references for the project that none of their saved layouts use, so closing a view removes its path at the next save. Reset removes all of that person's references for the project, including any another device class's layout still names, and those views then restore as placeholders. A retained offline copy can name a pruned reference in the same way. A window holding a pruned reference asks for a new one at its next save. A person holds at most 512 references per project, and a request past that is refused with 409 if all are in use.

**Version 1 rows.** They stay readable: every response migrates them to one column of their views in the given order, with `selected_tab` as the active tab. Geometry has no version 2 meaning and is dropped. Reading changes nothing; the row becomes version 2 at the next explicit save. A view with no resource stays as a reference without one and restores as a placeholder.

**Resolution.** `resources`, keyed by view ID, is recomputed through current authorization on every response, including a 409's `current`. A file view's `{location, id, folder, path}` appears only there, and only for the owner of the reference while its project or session is available. Unknown, foreign, archived or unauthorized references read `{available:false}` and carry no path, title or transcript. Session and team references must belong to the scope's project. Availability is only a hint: opening a resource calls its own authorized API. A reference never grants authority. Project deletion cascades layouts and references; person deletion cascades even when their projects transfer to an owner.

## Client integration

`web/src/lib/layoutStore.ts` exports `PersonalLayoutStore`, layout types and a transport signature compatible with the existing `api()` function (including its CSRF handling and structured `ApiError`). Obtain the canonical scope with an authenticated GET, construct a store with that scope and transport, then call `load()`. Storage defaults to browser localStorage; tests or consumers can inject `Storage`. Do not construct scope from a username typed into the UI or use a previous person's scope after sign-in changes.

`load()` never saves. `stage(layout)` retains a bounded, presentation-only pending copy with its expected server revision. `stage(null)` followed by `save()` performs the explicit audited reset. A failed transport keeps the pending copy and returns `offline`, without automatic retries; quota/read failures return `durable:false` and a visible failure note. An existing unreadable local copy is not treated as empty or overwritten. Pending records are scoped by all five identity dimensions; at most 32 browser records are retained, and a full store refuses persistence rather than evicting pending work. There are no draft contents in these copies.

A 409 or a changed revision on reload returns `conflict`, preserving both the server record and the pending arrangement. The consumer must offer an explicit choice: `resolve('server')` discards the local pending arrangement; `resolve('local')` prepares a new CAS against the reviewed server revision, followed by an explicit `save()`. A later server edit still causes another conflict. An edit made during an earlier save stays pending. The returned state is copied, so mutating it cannot change a save body. Another tab's already-changed local copy is not overwritten or deleted; failure is reported as volatile. This localStorage check is best effort, not a cross-tab transaction: the **server CAS** is the durable concurrency boundary.

`web/src/lib/layoutSync.ts` is the shell's coordinator for one project in one window: it debounces edits into `stage`/`save`, retries a retained copy on reconnection or an offline timer (each still a CAS), and holds a window's edits made before the server could be reached, so they never replace a layout it has not seen. `web/src/lib/workingSerial.ts` converts between the live columns and the saved shape. Scopes with no server key (a group project, or a session with no project) keep columns for the visit and say so.

Consumers must display `status`, `durable` and `note` honestly, and must not claim offline success. Offline resource hints are unavailable until authorization can be refreshed. Call `dispose()` for every open store on sign-out, identity change, project deletion or revocation; it clears that store's owned retained copy and rejects late replies. A shell must not render resource contents from a layout, reattach terminals merely to resolve a reference, or pass drafts to this library. `WorkingArea` owns selection, geometry and the conflict notice. Taking a newer layout keeps views that hold unsaved drafts or terminal control placed in this window. Restored terminals attach as read-only observers.

## Schema and recovery

Migration **0082** includes `layout_file_ref`; its downgrade refuses any records. This undeployed layout addition was folded into 0082 during integration, leaving 0083 for delegated approvals and 0084 for Sources. Migration **0082**, after 0081, creates `personal_layout` with cascading foreign keys to the permanently reserved username and project ID, and a composite key on person/project/device/viewport. It does not rewrite existing projects, people or preferences. Organizations principal inventory and route ownership inventory include the new data. Database transfer and HA use reflected/full database schemas rather than hard-coded table lists; the transfer regression verifies row count/checksum and layout contents.

Downgrade refuses any records, including reset tombstones, rather than silently losing preferences or revision history. Preferred rollback: stop the disposable/new application and restore its pre-upgrade consistent backup using the existing backup procedure; layout changes since that backup are lost. A deliberate empty-store downgrade requires explicitly removing these personal preference records while the application is stopped; reset is not data deletion. Never bypass the guard on a running install. Tests exercise upgrade from 0081, a refused nonempty downgrade, SQLite backup/restore into an isolated copy, verified database transfer, empty downgrade and re-upgrade. PostgreSQL execution is not claimed by those SQLite recovery tests.
