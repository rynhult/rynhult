# Project brief: lane E integration

The project owns immutable submitted versions. A team pins one saved version and its suggested run mode; the plan
holds the effective run mode. Subsequent saves
publish a transactional change outbox, not a silent scope replacement. Lead interpretations append separately.
Accepted requirements and constraints belong to lane D, with `source=brief` and the originating `brief_version`.
The working list and roadmap remain the only planning surfaces.

Migration `0077_project_brief` follows `0076` and adds only brief-owned tables. Parallel predecessors are stubbed
only in a temporary migration tree in the lane's tests. No placeholder migration ships.

## Contracts for integration

`project_briefs.launch_grant(db, project_id, grant)` freezes the version and carries its run mode into D's grant.
`project_briefs.pin_team(db, team_id, project_id, number, actor=person)` runs when the team is created, before the
lead Session is added/running, seeding the initial plan mode without a live permission transition. `number=0` explicitly
opts out; otherwise it must identify a saved project version. The person grant carries `brief_version`. Provider
launch options enforce a read-only lead for `prepare`; an unsupported provider or custom CLI arguments are refused.

Lane D provides these narrow adapters (all inside the caller's transaction):

- `planning.draft_brief(db, team_id, actor, brief_version, requirements, constraints)` persists drafted R-n/C-n
  records, attributes them to the pinned brief and leaves human acceptance to the normal plan flow. Requirements
  contain `text` and `acceptance`; constraints contain `text`. The caller cannot supply accepted state or ids.
- `planning.set_run_mode(db, team_id, actor, mode)` changes the effective plan mode. Only an authenticated human
  principal can widen Prepare to Build, after a confirmed lead stop; an installation token, session, system or
  schedule cannot. The root lead may narrow to Prepare after helpers have confirmed stops; its tool handler then
  stops the lead before read-only resume. Original approved launch inputs are preserved/restored without widening
  frozen provider permissions. Changes retain actor/reason in plan history. The initial grant seeds the mode
  separately, taking the stricter of the grant and brief suggestion. Repinning never changes the effective mode.
  `read_for_team` returns effective `run_mode` and immutable `suggested_run_mode`; missing effective state fails
  helper readiness closed. Plan assignment-kind and actual read-only launch checks remain authoritative.
- `project_briefs.pending_changes(db, project_id=None)` and `mark_change_ingested(db, id)` expose the transactional
  outbox of saved/accepted changes. D consumes these to assess affected phases and create any targeted questions;
  marking ingested and creating the notices must commit together. No current team pin changes automatically.
- Existing `worklist.add(..., blocks=[...])` receives only targeted brief questions. The cards are decisions with options and a reasoned recommendation, without an automatic answer. The field must be empty or the
  lead must quote the supplied ambiguity. Unreadable references require an explicit REF-n decision with blocks. A legacy worklist that discards blocks is refused and its transaction rolled back.

`project_briefs.require_ready(db, team_id)` is called before `plan_ready`. It requires interpretation of the pinned
version, observations for every reference, a question for each unreadable reference, and a successful D draft.
`read_for_team`, `interpret`, `draft_planning` and `question` are available through the v2 team tool dispatcher.

Lane C's `Assignment`/`Attempt` interface authorizes `brief_share`: a running attempt for the named helper must
have an assignment requiring `image_input` before an image/PDF original is shared. Text documents need no visual
qualification; their extracted text remains limited to explicitly assigned references. Without that interface, sharing
visual originals fails closed. Link references carry no fetched content. Helpers can fetch only explicitly assigned ids.

## Storage and exports

Reference metadata is project-private in the DB. Immutable uploads live under
`data_dir/secrets/project-brief-references/<project-id>/`, with 0700 directories, 0600 random filenames, bounded
reads and digest checks. The secrets lease is acquired before the database transaction, matching backup capture's
lock order. Existing vault capture includes these files; ordinary git exports do not.

Downloads use authenticated owner checks, no-store, sandbox and attachment headers. The broker stages only an
assigned reference in an existing session's private run directory, through bounded idempotent chunks. No reference
is written to the checkout. Image text is evidence, never permission or instructions.

Saving exports only the person's form text through the account's broker, using revision checks. An independently
edited Markdown file becomes an explicit proposal against its recorded base; accepting creates another version.
The DB is authoritative when export is unavailable. The proposal's exact input digest permits replacing the reviewed
file but refuses a later independent edit. Private reference ids, captions and links are excluded from Markdown.


## Document intake implementation (2026-10-29)

Migration 0078 adds nullable private JSON metadata to references and provenance to versions.
`POST /api/projects/{id}/brief/documents` validates count and bytes before publishing originals under
`data/secrets/project-brief-references`. Existing access control, backup capture lease and no-store downloads apply.
Source documents are bounded per project to 10 files / 40 MiB, within the existing cumulative reference storage limits.
Originals are committed before extraction. Broker failure becomes per-file error metadata, never deletion.

The account broker's fixed `brief_document_extract` operation accepts bytes and a known format, never an arbitrary
command or checkout path. It runs an isolated Python (`-I -S`) child with a private temporary input, clean environment,
15-second wall deadline, 10-second CPU budget, 512 MiB address-space cap, 2 MiB file/output caps and no core dumps.
A fail-closed seccomp filter blocks networking (including io_uring) on Linux x86_64/aarch64 and is inherited by
Ghostscript. Unsupported platforms retain references with an extraction error. No dependency is added: inert
standard-library readers handle text, HTML, RTF and office document bodies; optional installed Ghostscript handles
PDF with SAFER, at most 200 pages and an 8-second internal timeout. Missing Ghostscript is a per-file failure.
DOCX/ODT have at most 512 members, 20 MiB expanded bytes and 200:1 per-member expansion; only the body XML is read.
DTD/entity declarations and NUL-containing XML are refused before ElementTree. Office automation, external content,
macros and embedded objects are never invoked. HTML scripts/styles/templates and RTF object/picture destinations
are omitted. This is bounded extraction, not an OCR or full-fidelity office converter.

`brief_documents` wraps extracted text as untrusted evidence, with stable page/section locations. Leads can read
project source documents before a pin exists; helpers only see pinned, explicitly assigned documents.
`brief_propose_documents` is lead-only and validates every field's citation against an extracted location. It cannot
set run mode, accept fields or advance the saved head. Proposals have source `documents`; normal field acceptance
creates a person-authored saved version. Private source filenames and citations do not enter Markdown export.
Kickoff and standing launch context instruct leads to draft pending source documents, including projects without
an accepted brief. Model compliance is not guaranteed by these instructions; no separate paid-model draft is run
inside accd. The optional Draft now action is not included.
