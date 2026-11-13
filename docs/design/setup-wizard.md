# First-run setup wizard

Status: **implemented on the setup-wizard build branch**, 2026-11-05.
The wizard composes existing workflows; its person-owned progress extends the
existing setup-level preference API. Resource actions stay in their existing
screens; checklist confirmations record review, not resource health.

## Goals and boundaries

Give a new owner a short, resumable path from installation to one usable project,
with saved recovery codes and an honest account of unfinished work. Recommend
**1 Hobbyist**; offer 0 Expert, 2 Solo developer, 3 Small team, 4 Growing company,
5 Organization and 6 Enterprise. A level determines guidance, normal navigation
and a named set of recommended settings. It grants no authority and starts no work.

No installer replacement, automatic account provisioning, automatic spending,
publication, telemetry consent, enterprise provisioning or failover. V1 is the
Linux main used through a browser, including phone web; existing nodes keep their
platform limits ([platform decision](../decisions/0073-v1-linux-server-scope.md)).
Enterprise identity and delegated roles/teams remain parked per
[Contributing](../../CONTRIBUTING.md). Existing preview OIDC is not a v1 promise
([identity decision](../decisions/0056-oidc-relying-party.md)). Levels 4–6 use the
level-3 flow and defaults, with this note before selection and in the final summary:
“V1 supports small teams. This level does not enable enterprise identity,
automatic provisioning, delegated administration or automatic failover.”

Source baseline: [Security design](security-settings.md) (its opening status is
historical; the resolver and reviewed apply workflow now exist),
`src/acc/server/security_settings.py`, `src/acc/server/admin_features.py`,
`web/src/pages/Setup.tsx`, `web/src/components/SetupLevelSetting.tsx` and
`web/src/lib/setupLevels.ts`. Today's Setup level is a personal navigation
preference only. Preserve that contract: saving a level never applies Security.
The requested presets become **recommendations with a separate Review and apply**.

The assigned individual-ready gaps audit informed recovery, machine readiness,
provider billing, backups and updates. It is historical evidence: current setup
already displays recovery codes, folder/USB backups exist, and clean tagged
installs support in-app updates. Do not present those old gaps as current failures.

## Steps, writes and deferrals

Before authentication, keep the existing setup-token → owner/password/TOTP flow;
level selection follows sign-in. Every optional step has **Finish later** with a
specific consequence. “Deferred” never means ready. Expert gets concise controls,
not fewer authentication or authorization checks. Back does not undo saved changes.

| Step | Writes only after its explicit action | Level 0 | Levels 1–2 | Levels 3–6 |
|---|---|---|---|---|
| 1. Create owner and secure sign-in | Existing setup begin/complete creates owner, password/TOTP credentials, authenticated session and recovery codes; setup token keeps its existing lifecycle. Confirm codes saved; optional passkey uses existing enrollment. | Required on fresh install; concise help. | Same; explain authenticator and recovery. | Same; no company sign-in setup. |
| 2. Choose level | Existing per-person `level`, `dismissed`, `version` preference; person-owned wizard progress. No Security write. | Show all authorized tools. | Guided personal view. | Team view; 4–6 limitation note. |
| 3. Review protections and where to change them | Existing Security preview/apply writes only selected overrides at a named instance, machine/account or project scope, with existing revision, reauthentication and audit rules. | Compact comparison; optional separate provider-prompt review. | Explain all-On recommendation. | Same; show shared-scope consequences. |
| 4. First machine/node | Reuse an observed local broker/account, or explicitly enroll a node through existing installation/link workflow. Record selected machine/account in wizard progress. No silent OS changes. | Can select ready binding directly. | Default to ready local binding, otherwise show exact installer/readiness action. | Same; optional extra-machine guidance. |
| 5. Provider and keys | Existing machine/account provider login, or explicit encrypted vault-key creation and project grant once a project exists. No key in progress storage. | Skip if already ready. | Guide one provider; explain subscription versus API billing and project key assignment. | Same; personal sign-ins and explicit grants, never shared credentials. |
| 6. First project and optional people | Existing create/clone/register action writes project/location; trust remains a separate reviewed action. Optional existing group/invite actions retain their permissions. Return here to grant a key. | May choose existing project or defer. | One project; skip people/group guidance. | One project; offer people/groups, deferrable; no auto-invites. |
| 7. Backups | Explicit target creation through Host → Backups; retain privileged helper, encryption/recovery-kit and target checks. Reads existing installer timer separately. | Concise status; may defer. | Recommend folder/USB or another supported target; may defer. | Recommend off-machine target; may defer. |
| 8. Updates | Reads install eligibility; explicit save uses existing update configuration. No update installation during setup. | Concise status; may defer. | Explain signed tagged-release path or manual update requirement. | Same; no fleet rollout promise. |
| 9. Choose preview features (optional) | Explicit selections use existing authorized feature workflows, subject to locks and separate consent. No selection means no write. | Skippable; same choices. | Same; one-line descriptions. | Same; retain v1 limits. |
| 10. Review and finish | Completed/deferred checklist and timestamps only. **Start first session** opens existing New session for explicit launch. | Same summary. | Same, with next actions. | Same, including optional people and v1 limits. |

In step 3, briefly explain what guardrails protect: repository changes, files and
secrets, tool installation, provider prompts and budget checks. Show and link
**Settings → Security** (`/settings#security`; [guide](../wiki/User-Guide/Settings.md#security))
as the place to inspect or change them later; the old Guardrails link opens the
same scoped editor. Viewing this note or saving a level applies no policy.

Only owner creation/TOTP is an authentication gate. Keep recovery-code saving in
that flow before onboarding continues; do not require optional infrastructure to
enter the dashboard. If recovery acknowledgement was interrupted, route to the
existing authenticated recovery workflow, never recreate the owner or claim codes
were saved. Provider readiness is checked again for the selected machine/account
and project's effective key at launch. A disconnected machine stays unavailable.

Backup status distinguishes “target configured”, “successful capture” and “recovery
kit saved”. Same-disk timer copies are labelled as such, never disk-loss protection.
Show manual/helper actions where required; no invented one-click restore. Updates
show configured repository and eligibility; an untagged development checkout keeps
its manual path. No wizard level opts into automatic updates or unsigned releases.

## Concrete preset recommendations

These are **existing switch IDs**, not seven new permission modes. On = `true`.
Values apply to all seven levels; differences are guidance and navigation. A review
compares recommendations with effective values and sources, including explicit Off
and inherited overrides. Matching defaults need no write. Scope children remain
visible exceptions; do not flatten their policies.

| Security switch | 0 Expert | 1 Hobbyist | 2 Solo | 3 Small team | 4 Growing | 5 Organization | 6 Enterprise |
|---|---|---|---|---|---|---|---|
| `force_push` | On | On | On | On | On | On | On |
| `push_default` | On | On | On | On | On | On | On |
| `merge_pr` | On | On | On | On | On | On | On |
| `rm_outside` | On | On | On | On | On | On | On |
| `secrets` | On | On | On | On | On | On | On |
| `custom_rules` | On | On | On | On | On | On | On |
| `allowed_folders` | On | On | On | On | On | On | On |
| `tool_install_check` | On | On | On | On | On | On | On |
| `provider_prompts` | On | On | On | On | On | On | On |
| `budget_checks` | On | On | On | On | On | On | On |
| `risk_labels` | On | On | On | On | On | On | On |

`allowed_folders` is reviewed only for a concrete machine/account; it cannot be
written at instance/project scope. On does not create custom patterns, budgets,
roots or filesystem grants. Provider prompts retain qualified provider mappings;
Expert may separately request Off with sandbox consequences shown. Unrestricted
is never selected by level. Audit, vault/role boundaries, privileged approvals,
read-only assignments, qualification and publication consent always remain.

Feature recommendations retain installation defaults at **every level 0–6**,
subject to operator locks and existing owner choices. **The new layout is the only
layout from rc.4; the classic layout is retired (Josh, Nov 5).** This supersedes the older source
baseline's `new_shell` Off/opt-in assumption. Availability is separate from usage,
consent and navigation; the wizard changes nothing without an explicit selection.

| Existing Admin feature IDs | Fresh default, every level | Wizard treatment |
|---|---|---|
| `chat`, `organizations`, `groups_remote`, `containers`, `mobile` | Off | Preserve. No automatic preview enablement. |
| `new_shell` | Always on from rc.4 (classic retired) | Not offered; not a choice. |
| `multirun`, `schedules`, `automation`, `live_preview`, `publishing`, `issue_sources`, `groups`, `teams`, `orchestrator`, `remote_access`, `telemetry`, `local_models`, `notifications`, `explanations`, `risk_labels` | On | Preserve. No launches, schedules, public access, reporting consent or gate opt-in. |

Navigation uses existing `pathVisible`: 0 shows all authorized areas; 1 the basic
view; 2 adds Workspaces/Grid/Schedules/Publishing/Catalog; 3–6 also show
Groups/Board/Audit when authorized and available. The wizard runs in the new layout only. More tools,
saved links, Stop and recovery remain reachable. Reusable team profiles and
remembered account-wide tool consent are not existing setup features.

Step 9, **Choose preview features**, is skippable. There is no layout choice: the classic
layout is retired. List current
preview features from the live registry, each with a one-line description: company
sign-in (configured identity provider), own-machine groups (member-machine
placements), containers (qualified session isolation), native mobile enrollment
(device credentials), and direct public remote access (public access settings and
device pairing). The layout is not listed; it is not a choice.
Nothing is switched on unless the person picks it; no changes are preselected.
Skipping preserves the installation default and existing choices. Show unavailable
or locked choices with reasons; selection never bypasses owner permissions,
qualification, separate configuration or consent. Feature availability alone does
not enroll a device, expose public access or provision enterprise identity.

## Resume and later changes

**Progress storage, not a new Security switch:** server-side, person-owned wizard
record with schema/preset version, chosen binding and resource IDs, per-step
pending/completed/deferred state, timestamps and revision. Store no passwords,
setup tokens, TOTP seeds, recovery codes, provider device codes or vault values.
Authentication secrets remain in existing flows. Save progress after confirmed
resource writes; retry reconciles resource IDs and current readiness before
creating anything. Reopening a provider login uses its existing lifecycle.
A crash after creation but before progress save must discover the result or ask
the person to select it, rather than blindly replaying creation.

Refresh/restart or another browser resumes the first unfinished step after sign-in.
Concurrent progress saves use revision conflicts and reload/review, not last-write
wins. Resource deletion, revoked keys, changed machine identity or lost authority
invalidates readiness; it never triggers recreation or privilege escalation.
Security previews/reauthentication expire normally and are never saved as reusable
apply authority. Partial folder-policy application stays pending/repair until the
target acknowledges it. Finished onboarding does not imply every target is healthy.

**“Continue setup / Review setup”** in Overview and Settings → Setup level
reopens the checklist. Existing installations see an optional entry, never a forced
reset; existing people keep their unset level until they choose. Members get only
authorized personal steps and an owner-action explanation for installation steps.
Changing level saves presentation only. A separate **Review recommended settings**
shows current, proposed, source and affected scope; no differences are preselected
on re-run. The person selects exact changes before the existing Security apply.
Hand edits, inherited choices, feature locks and child overrides survive. A newer
preset version offers review, never migration by silent overwrite. Undo is another
fresh diff and reviewed apply, not a stale snapshot restore over newer edits.

## Accessibility, verification and review

Reuse the design system's Page/Card, labelled fields, QueryState, ErrorNote and
review dialogs. Use a semantic ordered step list with current/completed/deferred
text, heading focus on navigation and announced save/error status. Support keyboard
Back/Continue, visible focus, inline errors linked to fields, password managers,
manual TOTP key and copy/download recovery codes; never rely on QR, colour or hover.
At 320–390 px stack one step and its actions; wrap long names/paths, keep 44 px
controls, respect safe areas and zoom, and avoid sticky actions hiding errors or
the phone keyboard. Progress remains readable without a horizontal stepper.

Tests to add when implemented: backend authorization and progress ownership;
all seven recommendation rows versus the live registries; no policy/feature writes
from level save; explicit-Off/inheritance/child-override and operator-lock retention;
stale preview/reauthentication and pending target handling; interrupted setup at
each write boundary, lost recovery-code response, duplicate submissions and
cross-browser conflicts; provider/project/key readiness and revocation; honest
backup/update/deferred states. Browser tests cover fresh owner and existing member,
skipped/unselected preview choices causing no writes, explicit
selections respecting locks/permissions/consent, keyboard/screen-reader announcements, light/dark axe,
320/390 px and 200% zoom, and working Stop/terminal during re-run. No real paid
launch, publication, installer execution or provider sign-in in fixture tests.

## Decided

Josh’s answers, 2026-11-05: Q1–Q4, Q6 and Q7 agreed as recommended; Q5 revised.
The Security location/protection note in step 3 accompanies Q1/Q2.

1. Should all levels keep eleven protections On? **Yes**; Expert shortens guidance and offers separate prompt review, never automatic Unrestricted.
2. Should level save apply a Security preset? **No**; retain presentation-only saving and require a separate scoped diff/apply.
3. What should 4–6 offer in v1? **The level-3 flow**, retaining the selected label with the explicit enterprise limitation note.
4. Must every step block dashboard access? **No**; retain owner/TOTP and recovery-code flow, then allow explicit deferrals with unfinished reminders.
5. Should setup enable the new layout or other previews? **“Not by default, maybe a selection screen.”** Follow-up: "The old view should be completely retired, so choosing the layout shouldn't be a thing, only features." Offer the optional **Choose preview features** screen above; no change without a selection. New layout is the only rc.4 layout; classic is retired.
6. What is the default backup recommendation? **Off-machine when possible**; offer supported folder/USB with same-disk limitations, never label configuration as recovery proof.
7. How should upgrades and re-runs treat personal edits? **Preserve all choices**, offer an unselected diff, and require fresh confirmation for each policy change.
