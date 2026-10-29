# ACC source-publication checklist and license options

Prepared 2026-10-22. **Historical license discussion: superseded by the MIT release recorded in CHANGELOG.md.** The current license is `LICENSE` (MIT); package metadata and `THIRD_PARTY_NOTICES.md` describe the current distribution. The options below remain research, not the current license decision. Installers ship unsigned for now. **Release preparation guidance, not a completed security/legal audit or permission to publish.** No repository visibility, history, license or infrastructure was changed. Examples below use artificial identities and reserved domains/addresses.

ACC supervises native third-party CLIs, stores sensitive session history and delegates limited host operations to privileged helpers. Publishing source should make that boundary understandable without exporting the installation that runs it.

## 1. Define the release boundary and authority

- [ ] Identify who owns the original code, documentation, screenshots and contributions, and who can authorize their publication. Check rights to employer/client-funded work and imported snippets; a repository's possession is not proof of rights.
- [ ] Inventory what will become public: default branch, all published branches/tags and history, releases, packages/images, wiki, attachments, issues/discussions, CI logs/artifacts, Pages sites and example data. Decide what intentionally stays private and record an owner for each decision.
- [ ] Choose between publishing reviewed history and exporting a reviewed clean snapshot to a **new** public repository. A snapshot reduces historical exposure but is not a substitute for rights/secret checks; do not rewrite the current repository as part of preparation without explicit approval.
- [ ] Keep runtime state, user account homes, databases, transcript archives, backups, keys and `/etc/default/accd` out of the source release. Include only reviewed examples and synthetic fixtures. `.gitignore` does not remove already tracked or historical content.
- [ ] Resolve the version inconsistency before release: this checkout's manifests say 0.10.0 while the README begins with 0.9.0. Review installation URLs and release instructions against the actual public repository; do not assume a future GitHub URL is already public.

## 2. Names, identities and machine-specific data

- [ ] Search tracked text, filenames, paths and commit messages for real people's names, account names, email addresses, employer/customer names, project codenames and internal repository URLs. Review case-insensitive variants and URL-encoded forms. Keep required, consented copyright/attribution notices; do not indiscriminately erase authorship.
- [ ] Replace incidental deployment identities with fictional examples such as `alex`, `alex-agent` and `example-org`. Distinguish intentional product branding from accidentally leaked installation names. Review the collision/discoverability of "ACC / AI Development Control Center" before establishing public trademarks or package names.
- [ ] Review hostnames, internal DNS suffixes, Cloudflare zone/account IDs, Caddy sites, certificate names, SSH aliases, Git remotes, inventory files, container names, mount labels and backup destinations. A hostname is not a password, but it can identify a person or disclose network structure.
- [ ] Review public/private IPv4, IPv6, MAC addresses, VPN/Tailscale addresses, ports and topology. Private addresses are still installation data. Use `example.com` / `.test` where appropriate and documentation IPs such as `192.0.2.10`, `198.51.100.10` or `203.0.113.10`; keep meaningful generic port ranges and schema fields.
- [ ] Review machine paths and generated launch specs for real home directories, workspace names and session IDs. Keep generic paths such as `/srv/acc/app` where they explain the architecture, but not an actual user's private work location.

## 3. Credentials and sensitive operational material

- [ ] Review for `.env` variants, API/GitHub/Cloudflare tokens, OAuth refresh/device credentials, SSH/private TLS keys, Caddy CA keys, password hashes, MFA seeds/recovery codes, cookies, setup/invite/reset links and push-subscription endpoints/keys. Do not paste findings into public issues or commit messages.
- [ ] Include SQLite databases, dumps, WAL/SHM files, archives, service journals, crash reports, tmux captures, shell histories, screenshots, videos and agent transcripts in scope. They can contain third-party source, customer material, prompts and credentials even when named innocently.
- [ ] Review `deploy/accd.env.example`, service units, root-helper policies, MCP lockfiles, catalog entries and test fixtures. Separate harmless public client identifiers from credentials only after verifying their scope; do not assume an "example" file is safe.
- [ ] If a live credential was ever committed or otherwise exposed, **revoke/rotate it first** through the owner. Deleting a file or rewriting history does not revoke access or remove existing clones, caches and artifacts.
- [ ] Run secret scanning locally with approved tools against current files **and complete intended history**, recording tool/version and coverage. Review exceptions manually. Do not upload private source to an external scanner without authorization. Lack of a detector match is not proof of absence.

## 4. Internal documents, images and agent assets

- [ ] Review every `docs/` subtree, especially handoffs, research, decisions, council reports and operations notes. Some documents are internal deliberations or third-party material, not public user documentation. Publication should be an explicit selection, not "everything under docs is fine."
- [ ] Review `docs/wiki/Internal` page by page: separate generally useful architecture/runbooks from actual host layouts, access instructions and incident details. A directory called `Internal` is still public if committed to a public repository.
- [ ] Check wiki links and `.order` navigation, including links to material intentionally excluded from release. Review whether `docs/wiki/.public` is appropriate. When wiki content changes, run the repository's `acc-wiki-check`; a link check is not a privacy audit.
- [ ] Recreate screenshots and recordings with the repository's synthetic demo fixtures in an authorized test environment. Inspect visible browser tabs, address bars, tooltips, terminals, notifications, QR codes and image metadata. Prefer a fresh synthetic capture to redacting a production screenshot.
- [ ] Review `catalog/`, `.github/`, agent instructions, skills, subagent definitions, MCP configurations and generated launch templates for hidden internal references, embedded access and third-party licensing. Check that public agent instructions no longer require access to private infrastructure.
- [ ] Review this research and future handoffs too; research outputs are not automatically release-safe. Clearly label suggestions as suggestions and platform plans as unexecuted.

## 5. Git history and publication artifacts

Run these **read-only inventory examples inside the repository being reviewed**; they do not perform a secret scan:

```bash
git status --short
git ls-files
git for-each-ref --format='%(refname)'
git log --all --format='%h %ad %s' --date=short
git log --all --format='%an <%ae> | %cn <%ce>' --use-mailmap
git rev-list --objects --all
git ls-files --stage
```

- [ ] Inspect deleted/renamed files and blobs across all intended refs, not just HEAD. Check annotated tag messages, Git notes, signatures and author/committer metadata. Preserve legitimate provenance while obtaining consent for publishing personal attribution.
- [ ] Review submodules, Git LFS objects and their historical URLs/content, vendored archives and binaries, release attachments and built web assets/source maps. Text-only current-tree search misses these.
- [ ] Inspect the **actual artifact** to publish: archive, source checkout or container image. Check image layers/build caches and package metadata for excluded files. A clean working tree and a safe-looking Docker final layer are insufficient.
- [ ] If history cleanup is approved, do it in a separate release-preparation copy, retain a restricted backup, re-scan the result and verify the exported refs. Coordinate any necessary fresh clones. Do not force-push shared branches or silently rewrite attribution.
- [ ] Remember that making a private repository public can expose associated metadata independently of file contents. Review hosting-platform settings, CI history, releases and linked assets before the visibility change. Once public, assume copies cannot be recalled.

## 6. Dependency, operational and community readiness

- [ ] Inventory Python, npm, MCP, vendored code, fonts, icons, screenshot assets and build/container base images; record versions, provenance, licenses and required attribution. Review the shipped bundle as well as direct manifest dependencies.
- [ ] Clarify what ACC distributes versus merely invokes/downloads. ACC's chosen license does **not** relicense Claude Code, Codex, Copilot, Antigravity, Cursor or their services. Check each provider's redistribution, automation, account-sharing and authentication terms; require users to obtain their own valid access.
- [ ] Review root helpers, sudo/systemd/AppArmor policies, installer trust/download verification and upgrade/backup procedures through a separate authorized security review. Do not advertise hook patterns/worktrees as a universal sandbox. Explain the private-network requirement and the risks of permissive CLI execution.
- [ ] Validate installation on a clean supported Linux target and meet the [platform support gates](../wiki/Internal/Development.md#running-the-checks) before claiming those targets. Do not publish unsupported-platform claims based solely on tmux availability.
- [ ] Provide a real `LICENSE`, consistent package/SPDX metadata, third-party notices as needed, contribution guidance and a supported-version policy. Review whether a DCO or contributor agreement matches governance needs; a contributor agreement is not automatically required for open source.
- [ ] Establish a private vulnerability-reporting channel and `SECURITY.md` disclosure policy before inviting reports. Add issue templates, a code of conduct and a maintainer/support policy proportionate to the project.
- [ ] Keep CI fork workflows least-privileged, with no production secrets available to untrusted PR code. Review artifact retention and dependency automation. Do not connect a public repository's workflows directly to a privileged production ACC host.

## License comparison for ACC

These are all open-source licenses that allow commercial use. "Copyleft" does not mean "non-commercial," and permissive licensing does not eliminate notice obligations. This is a practical comparison, not legal advice; obtain qualified advice on ownership, dependencies and the intended distribution model.

| Question | MIT | Apache-2.0 | AGPL-3.0 |
|---|---|---|---|
| Basic model | Short, permissive copyright license. | Permissive license with more explicit contribution/patent terms. | Strong copyleft, including a condition for modified network-served versions. |
| Can someone sell ACC or build a hosted service? | Yes, including proprietary modifications, subject to notices. | Yes, including proprietary modifications, subject to conditions. | Yes, but covered distribution and modified remote-network versions have source-sharing obligations. Not a ban on competing hosting. |
| Source disclosure | No requirement to publish modifications. | No requirement to publish modifications. | Conveyed covered works must satisfy source/corresponding-source conditions. Section 13 requires a modified network-interactive version to prominently offer its Corresponding Source to remote users, at no charge. |
| Patent terms | No express patent grant or patent-litigation termination clause in the text; implied rights can be a legal question. | Express contributor patent grant for specified claims; patent grant terminates under the stated patent-litigation condition. | Contributor patent provisions in section 11; not identical to Apache's wording. |
| Notices on redistribution | Retain copyright and permission notice in copies/substantial portions. | Include license, preserve applicable notices, mark changed files, and carry applicable `NOTICE` attribution when provided. | Retain notices/license, mark changes as required, comply with source delivery and applicable interactive legal notices. |
| Proprietary integration | Most permissive/simple for downstream embedding. | Broadly friendly to enterprise/proprietary use, with more compliance details. | Combining into one covered work may require AGPL-compatible licensing; a separate service/process is a legal boundary to analyze, not an automatic exemption. |
| ACC-specific upside | Lowest friction for small self-hosters and integrations. | Low adoption friction plus explicit patent/contribution framework; suitable for infrastructure used by organizations. | Encourages modified hosted ACC control planes to remain inspectable by their users; fits a reciprocity goal. |
| ACC-specific tradeoff | Hosted forks need not share improvements; less explicit patent protection. | Hosted forks still need not share improvements; longer notice/compliance requirements. | More enterprise procurement/compliance friction; release-matched source access and licensing boundaries need deliberate maintenance. |

### Important AGPL boundaries

- Section 13 is not limited to publicly accessible SaaS: it addresses **users interacting remotely through a network** with a modified version. Evaluate internal deployments too. It does not simply demand that every operator publish all company code to the general public.
- Merely running an unmodified copy is not the same trigger as modifying it for network use; preserve applicable existing license/source notices and assess distribution separately.
- Corresponding Source means the preferred source for modifying the covered program plus required build/install/run material, not a dump of deployment secrets, user databases, private repositories or transcripts.
- Code that users ask an independently running agent to generate is **not automatically AGPL** because ACC launched the process. The question changes if output incorporates covered code or other licensed material.
- Interfacing with proprietary CLIs over process/terminal boundaries is not itself proof that they must be relicensed. Conversely, bundling/linking/adapting third-party code requires a real compatibility assessment.
- `AGPL-3.0-only` and `AGPL-3.0-or-later` are distinct SPDX choices; decide deliberately. Do not choose "or later" merely because a template contains it.

### Recommendation, not a license decision

**Prefer Apache-2.0 if ACC's primary goal is broad self-hosted adoption and integration.** It permits commercial and proprietary use like MIT while explicitly addressing contributor patents. MIT is reasonable if minimal legal text is the dominant goal and that patent distinction is acceptable.

**Prefer AGPL-3.0 if the owner deliberately prioritizes reciprocity for modified hosted control planes over lowest-friction adoption.** Budget for compliance guidance and a visible source/version link. It cannot prevent commercial competitors from operating compliant services.

Do not apply a license in this research change. First settle ownership, dependency compatibility and the desired contribution model, then record the chosen license in an ADR and update release metadata consistently. Later relicensing of contributed code can require contributor permission unless rights were arranged in advance; licenses already granted to existing copies are not simply recalled. Dual licensing is an ownership/governance option, not a guaranteed escape hatch.

## Release gate and evidence

- [ ] A named release owner signs off the file/ref/artifact inventory, remediation record and any intentionally retained public identity data.
- [ ] No unresolved credential or unauthorized-publication finding remains; exposed credentials have been rotated and intended release artifacts/history have been rechecked.
- [ ] Rights/license/notice review is complete, including third-party assets and agent distribution terms.
- [ ] Public documentation is synthetic and accurate; supported installation paths have actual recorded results; limits are explicit.
- [ ] A second authorized human reviews the exact proposed public artifact and hosting settings. Only then approve the separate publication operation.

Keep sensitive findings in a restricted tracker, not in the proposed public source. This checklist is **not evidence that ACC has passed these gates**.

## Primary references

Read 2026-10-22:

- [MIT license text, Open Source Initiative](https://opensource.org/license/mit).
- [Apache License 2.0 text](https://www.apache.org/licenses/LICENSE-2.0), especially sections 3-6.
- [GNU AGPLv3 text](https://www.gnu.org/licenses/agpl-3.0.html), especially sections 1, 5, 6, 11 and 13.
- ACC [README](../../README.md), [configuration example](../../deploy/accd.env.example), [setup script](../../deploy/setup.sh) and [development guide](../wiki/Internal/Development.md) for the release surfaces and existing checks to review.
