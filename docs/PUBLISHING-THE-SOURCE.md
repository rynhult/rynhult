# Publishing the source

Publish a reviewed snapshot into a new repository. Never publish this checkout's history:
it contains private infrastructure records, including deleted files. This procedure creates
files only; it does not create a repository, change visibility, or push anything.

**The first public release is planned as `v0.15.0-rc.1`, after the feature freeze
(approximately November 9-13, 2026), and still requires explicit owner approval.**
It publishes the Linux-server/browser release candidate with existing Linux,
macOS and WSL nodes. Native applications remain post-v1 work
([decision 0073](decisions/0073-v1-linux-server-scope.md)); their release builds
are not RC deliverables. Do not bump versions, create a repository or enable
release jobs as part of a preparation dry run.
The [October 27 scope decision](decisions/0073-v1-linux-server-scope.md) defines
that v1 as the self-hosted Linux server, existing Linux/macOS/Windows-WSL nodes
and browser access, including the phone web app. Native apps are post-v1, one
platform at a time; the platform order has not been chosen.
Existing experimental desktop/mobile source may be in the reviewed snapshot;
its presence neither makes it a v1 application release nor adds native app
qualification to the publication gate. Existing server/node/browser gates and
separate owner publication approval remain required.

The intended new public repository is `rynhult/rynhult`, site `rynhult.dev`, npm
namespace `@rynhult` (future CLI `@rynhult/cli`), and Docker identity
`joshuafulton/rynhult`. None of these names authorizes creation or upload.
The private reporting address is `security@rynhult.dev`; the owner must verify
mail delivery and GitHub private reporting before release. Choose the author
identity for the new initial commit and approve the public copyright identity.
Existing private commit authors are not exported. Preserve required third-party
copyright and attribution notices, including upstream Apache NOTICE obligations.

The default source URL and root update helper now target the intended public
repository. Explicit root-owned `ACC_SOURCE_URL` / `ACC_UPDATE_REPOSITORY`
overrides remain authoritative; review existing installations before upgrading.
No remote repository or release availability is inferred from a configured URL.

1. Commit all intended changes and inspect `git status`. The exporter reads **HEAD** only;
   untracked and uncommitted files are ignored.
2. Create a UTF-8 denylist **outside every checkout**. Put one nonempty literal identifier
   per line: private domains and suffixes, addresses, machine and project names, people,
   handles and path fragments. Include case variants or encodings where relevant. Lines
   are case-insensitive byte substrings, not regular expressions; there is no comment
   syntax. Never commit this file or paste its contents into public logs.
3. Run with Python 3.12+ and git available, from the private checkout:
   ```sh
   scripts/public-snapshot.sh /tmp/private-denylist.txt /tmp/rynhult-public-source
   ```
   The destination must not exist and must be outside the checkout. Keep both temporary
   paths private until review is complete. The script stages and scans before creating
   the destination and refuses an empty/missing denylist. Any match withholds the snapshot
   without printing matching private data. It scans file contents (including binaries),
   relative names and symlink targets. Safe internal symlinks and executable modes survive;
   external or dangling symlinks and special files are rejected.
4. Review the **entire** resulting tree, including images and other binary artifacts
   (the byte scan cannot read text inside screenshots). Review notices, installation URLs,
   private-document references and security reports separately. Run the documented tests
   from that tree. A denylist is a check, not proof that every private identifier is known.
5. Only after the release-candidate gates and separate publication approval, use this directory as the initial source commit
   of a **new** repository. Do not copy `.git`, add this checkout as a remote, or import
   its branches/tags. Configure the selected author, private vulnerability reporting and
   branch protection. Release jobs require the canonical repository and the repository
   variable `RYNHULT_PUBLICATION_APPROVED=true`; leave it unset until the owner
   confirms the release gates and approves publication. The source job accepts
   `v0.15.*` (the public RC and its stable successor) as well as v1-or-later
   tags, but still excludes older 0.x previews and requires the canonical
   repository, approval variable and exact tag/project-version match.
   Review the resulting public commit and artifacts before any push.
   No repository creation or push is performed by these instructions' script.
6. Delete temporary snapshots and denylist copies when finished; retain the authoritative
   denylist privately. Re-run from the final committed HEAD for every release snapshot.

The exporter excludes `docs/PLAN.md`, all `docs/archive/` (including historical plans), `docs/OPERATIONS.md`,
`docs/SESSION_LOG.md`, `docs/handoffs/`, `docs/qualification/`,
`docs/research/node-test-plan.md`, `docs/RELEASE-RC1-CHECKLIST.md` (owner-only release steps),
`docs/RENAME-STAGE-2.md` (private rollout inventory), all `host/`, and
all `docs/reviews/` (including reports that describe the operator's network). It also excludes `docs/security/`,
`docs/drafts/`, `docs/decisions/drafts/`, and the operator-specific decision records
host-specific `docs/runbooks/` and `docs/drills/`. Security reviews and
owner placement/hosting briefs are private regardless of which review folder
they use. Developer pages under `docs/wiki/Internal/`, the scrubbed multi-user
architecture and accepted technical ADRs are published after genericizing.
Do not exclude developer documentation merely because its section is named
Internal. Private execution records stay in the private checkout.
This is the same policy used by source bundles,
node downloads and release archives. Matching is case-insensitive and covers descendants.
Both paths also exclude generated/runtime folders (including `.acc/` artifacts), secrets,
environment files such as `.env.local`, databases, caches and dependency/build output.
A checkout with present but invalid Git metadata refuses bundle creation instead of
including untracked files. A source tree with no Git marker remains an installed tree;
review new folders and arbitrary contents explicitly, since names cannot establish privacy.
The reusable GitHub ruleset lives at
`deploy/github/ruleset-protect-main.json` and is included. Private documents stay in the
private working repository; historical account migrations remain intact for upgrades.
They are historical compatibility records, not current defaults. Existing database
account assignments are preserved: administrators must correct any obsolete assignments
explicitly rather than having an upgrade guess a replacement Linux user.

See `docs/wiki/Internal/Security.md` for current supply-chain limits and
`CONTRIBUTING.md` for contribution guidance. Do not publish a snapshot until all remaining
release decisions and security findings have been resolved or explicitly accepted.

The pinned Apache upstream trees were checked during this preparation: anthropics/skills
has no NOTICE file at its catalog pin; pbakaus/impeccable's NOTICE.md is retained beside
its vendored skill and included in the generated notices inventory. Repeat that check
when changing upstream pins.

## Repeatable dry-run checks

From the exported tree, run the directory secret scan (not a history scan):
`gitleaks dir . --redact --no-banner`. Use a locally available executable;
no scanner installation or remote vulnerability upload is required by this dry run.
Run `PYTHONPATH=src python3 scripts/check_public_links.py` for public Markdown
links and `python3 scripts/third_party_notices.py --check` with the matching web
dependencies available. Run the contributor test commands documented in the wiki.
Inventory every image, including app icons, and visually review each at readable
resolution for artificial identities, domains, secrets and host telemetry. Fixed
metrics in screenshot harnesses do not attest old captures; review the actual bytes.
Record file hashes and the exact source HEAD in a private review report.

Native installers must be built only from the approved sanitized source, with a
fresh local initial commit if their builder needs Git. Their platform staging is
not an alternative privacy boundary. Do not run release workflows from this private
checkout; native builds, signing and installation require separate qualification.
Delete temporary trees and denylist copies after recording results.
Before distributing supported-node installers, verify that enrollment codes cross
process boundaries through stdin (`--code -`), not argument lists. Track outstanding
installer findings privately; neither a source snapshot nor this checklist attests
native installer qualification.

## Local artifact qualification (including zero-major release candidates)

A dry run does not authorize a tag or publication. Build only in disposable clean checkouts, temporarily setting
`pyproject.toml`, the root package entry in `uv.lock` (PEP 440: `0.15.0rc1`), and both web package manifests to
`0.15.0-rc.1`. Commit those temporary changes only inside the disposable repository. Keep the real release branch's
version unchanged. Record Python, Node, npm, uv and the resolved Hatchling version with the results.

1. Build the web assets with `npm ci` and `npm run build -- --configLoader runner` from `web/`.
2. Build Python distributions with `uv build --out-dir <python-output>`. Python sdists contain the Python package and
   its wheel inputs only; they are not the complete Linux server installer. Neither the Python wheel nor the source
   tarball currently embeds the built dashboard. The Linux source installer builds it during setup. Do not present
   the wheel alone as an offline server installation.
3. Build the Linux source/installer tarball with `python3 -I src/acc/bundle.py --source . --out <linux-output>`.
   The installer is `deploy/acc-install` inside this archive; there is no separate Linux binary installer in
   `releases.yml`. Extract its `.acc-build` into `BUILD`, as the workflow does.
4. Repeat from a second checkout of the exact temporary commit, with different checkout timestamps. Compare every
   wheel, sdist and source tarball byte-for-byte, plus a sorted SHA-256 inventory of all built web files.
   Source archive member timestamps are normalized to zero; identity still covers contents and executable modes.
5. From the artifact directory, run `sha256sum acc-*.tar.gz BUILD > SHA256SUMS` and `sha256sum -c SHA256SUMS`.
   If distributing Python artifacts or web archives separately, checksum those too in their own manifest.
   Verify the manifest again after transferring artifacts over the trusted channel.
6. Run the fake-root installer tests and updater tests against the extracted source. `ACC_INSTALL_ROOT` is a
   non-root, private `/tmp` test seam, with privileged setup actions stubbed: it does not qualify a real root install.
   The updater's SemVer parser orders `0.15.0-rc.1` above `0.14.0`, but automatic updates and update requests accept
   stable releases only. Use the reviewed manual RC upgrade procedure; do not remove that safeguard for a dry run.
7. Reapply the owner-reviewed publication scrub list and external denylist to the final source, all archives and
   built web files. Check binaries and screenshots separately. Never publish the private working repository's
   automatic GitHub source archives or its history. The prepared public tree must be reviewed again after merges.

### Checksums are not signatures

The workflow emits SHA-256 checksums and a content `BUILD` identity. Checksums alone
detect transfer corruption, not publisher identity: replacing both a download and
its checksum defeats that protection. The owner signs **only `SHA256SUMS`**, offline.
Its verified hashes authenticate listed files; archives, `BUILD`, release tags
and attestations are not separately signed by this process.
The updater in later source builds verifies the detached signature using only
its installed public key before using any manifest hash. The privileged helper
repeats the check. GnuPG is required. Missing signatures block normal/automatic
updates; only an explicit, audited, one-update owner exception permits a missing
signature, never an invalid one. Catalog release restore has no exception.
The updater **as shipped in rc.1** did not perform this verification; installing
a later verifier does not retroactively authenticate an earlier update.
Node synchronization uses the enrolled main's separately authenticated rebuilt
bundle, not independent verification of the public release archive.

For key rotation, ship the next primary inside a release signed by an already
installed key before switching signers. Revocations must likewise reach the
installed anchor through a trusted update or reviewed manual reinstall; no
network key refresh occurs. See the Guide's [update controls](wiki/User-Guide/Updating.md#release-signatures).

The owner-generated public key is
[`keys/rynhult-release-key.asc`](../keys/rynhult-release-key.asc), copied by both
site phases to https://rynhult.dev/keys/rynhult-release-key.asc:

- Primary fingerprint: `0C2C E126 57BD 833F B5B5 63B2 B555 F6E1 2799 0DB5`; certification only, expires 2031-10-28.
- Signing subkey fingerprint: `B072 4912 051F F1F2 74CD EE7F EDE4 F41B 077B D2A7`; expires 2028-10-28.

For a published release, users download the key, compare **both full fingerprints**
against an independently trusted copy of these values, then import and verify:

```sh
curl --fail --show-error --location --output rynhult-release-key.asc \
  https://rynhult.dev/keys/rynhult-release-key.asc
gpg --show-keys --with-subkey-fingerprint rynhult-release-key.asc
# Stop here unless both fingerprints match your independently trusted copy.
gpg --import rynhult-release-key.asc
gpg --verify SHA256SUMS.asc SHA256SUMS && sha256sum -c SHA256SUMS
```

On macOS use `shasum -a 256 -c SHA256SUMS` for the checksum step. Missing signatures
and failed signature/checksum checks are stop conditions, never a reason to bypass
verification. A signature does not prove freshness or absence of vulnerabilities.

### Owner's offline signing and rc.1 attachment

The private key stays on the owner's Mac/offline signing environment, never in
this repository, a test checkout, CI secrets or a server. CI does not sign.
The script requires GnuPG; no secret key is generated, exported or printed.
Use a reviewed checkout and an already-reviewed final checksum manifest.

1. After the approved workflow has built the release, download its exact
   `SHA256SUMS` and assets and transfer the manifest to the signing environment.
   Check its hashes against the reviewed artifacts; do not sign an unreviewed
   replacement manifest.
2. On the Mac, run either form:

   ```sh
   bash scripts/sign-release.sh /path/to/release-folder
   # Or pass /path/to/release-folder/SHA256SUMS directly.
   ```

   The script also runs with `zsh scripts/sign-release.sh ...`. It defaults
   `GNUPGHOME` to `$HOME/rynhult-signing`; an explicit `GNUPGHOME` overrides it.
   GnuPG handles passphrase entry through the owner's pinentry. If terminal
   pinentry needs it, set `GPG_TTY` to `$(tty)` on that Mac.
   The script checks the primary and subkey fingerprints, signs with
   `--local-user B0724912051FF1F274CDEE7FEDE4F41B077BD2A7!`, then verifies the
   detached signature against the repository's public key in a temporary
   public-only keyring before creating `SHA256SUMS.asc`. It refuses an existing
   signature rather than overwriting it. The public verifier uses a short,
   private directory under `/tmp` so long release paths do not exceed GnuPG's
   socket-path limit; the private signing keyring is not moved. Signature
   staging stays beside the manifest for same-filesystem, no-overwrite
   publication. Both temporary directories are removed on exit. On failure,
   the helper shows up to 4096 bytes of GnuPG's diagnostic, never a secret-key
   export.
3. Transfer only `SHA256SUMS.asc` back to the publication machine and upload it
   beside the unchanged manifest:

   ```sh
   gh release upload v0.15.0-rc.1 /path/to/SHA256SUMS.asc --repo rynhult/rynhult
   ```

   The source workflow has an optional `dist/SHA256SUMS.asc` attachment hook;
   the normal build has no private key and explicitly announces that manual
   signing/upload remains. For rc.1 the command above is the intended route.
   Do not claim a signed release until the signature is attached and checked.
4. Download the published manifest/signature and verify them again against the
   checked public key, then verify every listed asset. When updating
   `site/release.json` for publication, link the signature alongside the checksum
   manifest and downloads. The site build publishes the public key without
   changing its Content-Security-Policy.

Replacing `SHA256SUMS` after signing invalidates the signature; review and sign
again if any manifest bytes change. Never leave an old signature advertised for a
new manifest. This preparation does not upload, deploy, publish or authorize a
release, and does not attest that rc.1 has already been signed.
