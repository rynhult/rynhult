# Dependency updates

Use this runbook when reviewing dependency PRs or replacing a downloaded tool.
A pin is a reviewed version plus its expected digest. Keep version changes,
lock regeneration, notices and qualification in the same review. Never update a
live installation to test a candidate.

## Automation and activation

`.github/dependabot.yml` extends the existing native GitHub updater. It checks
uv, pip requirements, npm, Cargo, container files and Actions every Monday at
08:00 UTC, with groups per ecosystem. Separate security groups are triggered by
advisories, without a weekly wait or cooldown. There is no auto-merge.
Dependabot was chosen over Renovate because it already runs for Actions and
needs no additional app, token or custom code to manage these package locks.
See the [GitHub options reference](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference).

A repository administrator must enable **Dependency graph**, **Dependabot alerts**
and **Dependabot security updates** in the repository's security settings.
Configuration alone cannot switch them on. Check the Dependabot update logs and
confirm that an available security advisory produces a security PR. Here,
“immediate” means advisory-triggered: GitHub processing time, ecosystem support
and availability of a patched dependency still apply. These settings were not
changed from the offline development checkout. Follow
[GitHub's activation instructions](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/secure-your-dependencies/configure-security-updates).

Bot coverage is not universal vulnerability coverage. Arbitrary binary downloads,
uv's bundled CPython manifests and pending Caddy builds need manual review.
Digest-only container images may need an explicit replacement digest selected
from a reviewed upstream release. Requalify the session image for any base change.
The desktop's nonstandard `requirements-linux.lock`, `requirements-macos-arm64.lock`
and `requirements-build.lock` are regenerated manually; Dependabot's pip discovery
does not recognize their `.lock` extension.

## Inspect pins before and after a change

From a reviewed checkout, with Python 3.12 or newer:

```bash
python3 scripts/check_pins.py
python3 scripts/check_pins.py --json
```

The checker performs no network requests and executes no installer. JSON lists
URLs, versions, SHA-256 values and source files for direct downloads, Cargo and
npm artifacts, plus hash-locked requirements. npm's native integrity string is
reported separately. Index-resolved requirements
have no hard-coded artifact URL; their URL is null. Dynamic Caddy and bundled
CPython records identify who supplies verification instead of claiming a
committed hash exists. The Caddy base/module pins are reviewed build inputs,
not preapproved binary digests.

The gate checks SHA-256 format, npm integrity encoding, exact npm/Cargo manifest
agreement with locks, uv installer/workflow agreement, Node URL/extraction version
agreement, and desktop/main Python agreement. It also checks where locks install
from, so a dependency update can't move a package elsewhere: `uv.lock` only from
PyPI (or the project itself), `Cargo.lock` only from crates.io (or a path crate),
and every npm lock, including `web/package-lock.json`, only from
`https://registry.npmjs.org/` with SHA-512 integrity (bundled and linked entries
have no download of their own). Telemetry and session-image locks
are independent environments; their dependency versions need not match the main.
Native provider catalog pins also have separate qualification from session-image
CLI versions. Existing differences are not silently changed by this task.

New source release/archive URLs or digest literals fail until an inventory
adapter covers them. Add a behavioral test, update the adapter and inspect JSON
output. Documentation, test fixtures and generated build directories are excluded
from source discovery. This is a format and drift gate, not a generic shell
parser, signature verifier, vulnerability scanner or approval of upstream bytes.
Do not add broad exclusions to silence a new installer.

## Python and the desktop runtime

1. Review the advisory, upstream release notes, license and compatibility with
   Python 3.12. For a deliberate single-package update, run
   `uv lock --upgrade-package PACKAGE`; use `uv lock --upgrade` only for a reviewed
   broad refresh. Commit `pyproject.toml` changes when constraints need changing.
2. Regenerate the desktop lock from the main lock's exact resolved versions.
   This prevents `uv pip compile` from independently selecting newer packages:

   ```bash
   uv export --frozen --no-dev --no-emit-project --format requirements.txt --output-file /tmp/acc-main-constraints.txt
   uv pip compile pyproject.toml --constraint /tmp/acc-main-constraints.txt --generate-hashes --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --output-file desktop/requirements-linux.lock
   ```

   Repeat the compile command with `--python-platform aarch64-apple-darwin` and
   `--output-file desktop/requirements-macos-arm64.lock` for Apple silicon.
   Review platform-only packages and markers. Remove the temporary constraints
   file after use. Run `uv sync --frozen --python 3.12` for local metadata and
   tests. Never replace `--require-hashes` installs with unverified installs.
3. Bundled CPython's version is in `desktop/build-runtime.sh`. Changing it also
   requires checking the selected python-build-standalone build in the pinned uv
   release's embedded manifest. If its Python minor changes, review every library
   path and packaging assumption in that script. Build and test the desktop on a
   disposable supported Linux or Apple-silicon macOS builder; do not run its
   runtime builder in a live service tree. Preserve bundled runtime dependency
   locks and licenses. On macOS, keep `--require-build-hashes` for the allowed
   http-ece sdist; `desktop/requirements-build.lock` pins packaging, setuptools
   and wheel with hashes for its isolated build. Refresh those exact build-tool
   versions in a reviewed input requirements file, then compile it with
   `uv pip compile --generate-hashes --python-version 3.12 --python-platform aarch64-apple-darwin`
   and `--output-file desktop/requirements-build.lock`. This build environment
   is independent from the main; never relax its hash requirement.
4. Regenerate notices and run the checks below. Desktop runtime packaging is a
   separate qualification from the default Python suite.

Standalone Python environments:

| Pin | Safe update |
|---|---|
| `deploy/telemetry/requirements.txt` | Regenerate hashes using `uv pip compile --generate-hashes --universal --python-version 3.12`, retaining the existing collector's top-level inputs. Re-run collector qualification with temporary servers. This is an independent lock; do not copy the main's versions into it blindly. |
| `deploy/containers/session/requirements.txt` | Resolve the required PyYAML release for CPython 3.12/Linux amd64, retain the reviewed wheel's SHA-256 and `--require-hashes --only-binary` install. Requalify the image; a hash for an sdist or another platform is insufficient. |

## npm, Rust and Actions

| Pin | Safe update |
|---|---|
| `web/package.json` and `web/package-lock.json` | Use `npm install --prefix web PACKAGE@VERSION --save-exact` (add `--save-dev` for build tools); review the lock diff and install scripts. Run `npm ci --prefix web`, build and affected desktop/mobile browser tests. |
| `deploy/mcp/package.json` and lock | Update exact versions with `npm install --prefix deploy/mcp PACKAGE@VERSION --save-exact --ignore-scripts`. Review transitive licenses and regenerate notices. Qualify MCP servers against test fixtures, never a live agent session. |
| `deploy/containers/session/package.json` and lock | Update with `npm install --prefix deploy/containers/session PACKAGE@VERSION --save-exact --ignore-scripts`. Review platform packages and native CLI bytes. Run container qualification in a disposable builder before approving a new image. |
| `desktop/src-tauri/Cargo.toml` and `Cargo.lock` | Update exact `=` constraints deliberately, then `cargo update --manifest-path desktop/src-tauri/Cargo.toml -p CRATE --precise VERSION`. Review all resolved changes; run `cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml` and desktop packaging. Preserve upstream license notices in redistributed desktop artifacts. |
| `.github/workflows/*.yml` Actions | Keep `uses:` pinned to full reviewed commit IDs. Check upstream release provenance, permissions and changed code; keep any version comment accurate. Dependabot proposes these changes, but approval stays manual. |

## Direct downloads and reviewed builds

For each architecture, get the intended immutable artifact and its independently
reviewed expected digest. Check vendor signatures/provenance where available and
verify bytes locally with `sha256sum` before editing a pin. Downloading a checksum
beside a binary is not independent approval. Never derive the expected digest
from an unreviewed binary and treat the resulting comparison as authentication.

| Pin and source | Update all of these together |
|---|---|
| uv: `deploy/lib/install-uv.sh` | `UV_VERSION`, Linux x86_64 and aarch64 tarball digests, and every `uv==` bootstrap in `.github/workflows/`. Review the official release's embedded Python manifest when desktop CPython changes. Keep architecture-specific URLs and digests paired. |
| GitHub MCP: `deploy/lib/install-github-mcp.sh` | `VERSION`, x86_64 and arm64 release tarball digests, release comments and extraction assumptions. Test installer dry-run/fixtures rather than invoking a live server. |
| restic: `deploy/packages/acc-packages` | Catalog `version`, amd64 and arm64 compressed `.bz2` digests, and release-note text. Hash the compressed artifacts the installer actually fetches. Test backup/restore against temporary repositories; preserve a tested recovery path. |
| Native Claude Code: `deploy/packages/acc-packages` (`CLAUDE_NATIVE`) | `version` and the linux-x64 and linux-arm64 SHA-256s, copied from Anthropic's `manifest.json` for that version, plus the note text and `CLAUDE_MAX_BYTES`. Used on machines without apt (docs/LINUX-DISTROS.md). The checker requires both platforms. |
| AppImage tools: `desktop/appimage/fetch-tools.sh` and `tools.sha256` | Each `fetch` line's release tag (in the URL) and download SHA-256, and the matching normalized line in `tools.sha256`. The checker requires the same tools in both files, and the same digest for every tool the script doesn't modify (only linuxdeploy has its AppImage magic blanked). linuxdeploy-plugin-appimage publishes no digest; its digest is first-seen, so re-verify it independently. |
| Android build check: `scripts/android_build_check.sh` | `jdk_version` with both version forms in `jdk_url` and `jdk_sha` (the release's adjacent `.sha256.txt`); `gradle_url`, `gradle_sha` and every `gradle-<version>` path and the plan's `Gradle:` line; `cli_url` and `cli_sha` (the SHA-256 on Android's command-line tools page). The checker requires `jdk_version` to match both forms in `jdk_url`, and the plan's `Gradle:` and `Android command-line tools:` lines to match their URLs; it doesn't check the unpacked `gradle-<version>` paths, so update those by hand. Re-run the disposable-VM check (docs/BUILDING-ANDROID.md). |
| Node: `deploy/containers/Containerfile` | Both version occurrences in the release URL, the extracted directory name, and Linux x64 `.tar.xz` digest. Signed NodeSource apt packages used outside the image are a separate trust/update path. |
| Container base: `deploy/containers/Containerfile` | Reviewed immutable image digest with its matching `-amd64` release tag (`image:TAG@sha256:…`; the checker refuses an untagged base, which made Dependabot propose the stale `latest`), architecture and compatibility documentation in `deploy/containers/README.md`. Inspect inherited packages/licenses and re-run disposable image qualification. A successful digest format check is not image qualification. |
| Go: `deploy/dns/release.json` | `go` and `go_linux_amd64_sha256`. The Caddy builder downloads this host toolchain even when cross-compiling arm64. Check that the workflow still consumes these fields and verifies the tarball. |
| Caddy DNS candidate: `deploy/dns/release.json` | Caddy and xcaddy versions, full provider module commits, candidate ID, resolved Go module locks, SBOM, provenance and qualification evidence. Build both architectures through the candidate workflow. Commit independently approved `artifacts.amd64.sha256` and `artifacts.arm64.sha256` only after qualification; never mark a hashless candidate qualified. Follow `deploy/dns/README.md` for root-helper activation. |
| Legacy Caddy Cloudflare: `deploy/lib/install-caddy-cloudflare.sh` | The build-service URL is dynamic, so there is no committed version/hash. Keep the required operator-supplied `ACC_CADDY_SHA256`; obtain it through an independently reviewed build process, or preinstall a reviewed binary. Never copy a freshly downloaded binary's digest into the approval field as a shortcut. See `deploy/caddy/README.md`. |

Other provider downloads can resolve upstream manifests or registry integrity at
installation time; they are not hard-coded artifact/hash pins. Preserve their
existing bounds and verification and review them separately. This checker does
not convert those paths into pinned releases.

## Notices and acceptance checks

The notices generator reads exact installed Python and web package metadata and
the committed MCP lock. Run it in the frozen environment after installing locked
web dependencies, even if a bot updated only a lock:

```bash
uv sync --frozen --python 3.12
npm ci --prefix web
uv run --frozen python scripts/third_party_notices.py
uv run --frozen python scripts/third_party_notices.py --check
python3 scripts/check_pins.py
PYTHONPATH=src uv run --frozen python -m pytest -q tests/test_check_pins.py tests/test_license.py
```

Review and commit `THIRD_PARTY_NOTICES.md` when it changes. The generator does not
replace license/SBOM review for Rust, container base layers or separately
installed tools. Retain those distributions' own license and notice files and
update their packaging records when required.

Run the full contributor checks in [Development](/Internal/Development), plus the
relevant desktop, installer, telemetry or container qualification. Never merge a
security PR solely because a digest is well formed; prioritize review and tests
without waiting for the next weekly batch. The contributor workflow runs the pin gate before installing dependencies,
and release CI checks it before producing a source bundle. The existing
contributor job is disabled for private repositories; private development must
run these same local gates before merging. This task does not change that policy.
The choice is recorded in `0059-dependency-automation.md` in the repository's
[decision records](/Internal/Decisions-and-Roadmap).
