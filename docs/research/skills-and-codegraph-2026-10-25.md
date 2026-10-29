# Skills and code graphs: catalog recommendations, 2026-10-25

**Recommendation:** curate a compact, explicitly invoked Matt Pocock interview
skill and an adapted throwaway-prototype skill. Offer `teach` separately for
learning workspaces. Do not add the complete Superpowers or Matt Pocock plugin
to default profiles. Defer Wayfinder pending tracker/authorization adaptation.
CodeGraph merits a bounded navigation experiment on this repository, but **not
an unmodified default MCP integration or a promised token saving**. Defer Serena
v2: its REPL/editing surface, runtime dependencies and licensing make it a larger
integration than this navigation problem needs.

This is research, not a catalog/configuration change. No software was installed,
no downloaded code or installer was executed, no container/service was started,
and no model API or live Rynhult agent tool was called. Sources were downloaded
as data from public GitHub endpoints into a temporary directory, inspected, and
removed after measurements. No credentials were supplied. Figures below separate
our static measurements, upstream experiments and recommendations.

## Source snapshots and licenses

Findings refer to these exact commits, retrieved on 2026-10-25. Release tags are
context only: a repository HEAD is not necessarily the release's source tree.
In particular, CodeGraph's reviewed HEAD is newer than its latest release;
Serena's reviewed `pyproject.toml` says `2.1.1.dev0`, while its latest release is
`v2.1.0`. Audit the actual distribution before a pilot, not just its version label.

| Project | Reviewed commit | Latest release observed | License at reviewed commit |
|---|---|---|---|
| [Superpowers](https://github.com/obra/superpowers/tree/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b) | `2ce8aa15d05bd57213e6f746f953aafb7ded7a8b` | v7.0.1, Oct 21 | [MIT](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/LICENSE) |
| [Matt Pocock skills](https://github.com/mattpocock/skills/tree/5beb35b040e3de97e54fa9e386d7e42d15c295c2) | `5beb35b040e3de97e54fa9e386d7e42d15c295c2` | v1.3.1, Oct 4 | [MIT](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/LICENSE) |
| [CodeGraph](https://github.com/colbymchenry/codegraph/tree/69af19ace270f2e940444d514de6dde971df12df) | `69af19ace270f2e940444d514de6dde971df12df` | v1.6.2, Oct 3 | [MIT](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/LICENSE); bundled Node/grammars/dependencies need their own notices |
| [Serena](https://github.com/oraios/serena/tree/abb0a0f01b94bc8050086f0fb860077d17730324) | `abb0a0f01b94bc8050086f0fb860077d17730324` | v2.1.0, Oct 19 | [Application GPL-3.0-or-later; SolidLSP MIT](https://github.com/oraios/serena/blob/abb0a0f01b94bc8050086f0fb860077d17730324/LICENSE) |

Serena's older MIT reputation is insufficient for v2. Its licensing overview
explicitly describes the combined `serena-agent` distribution as GPL, while
preserving SolidLSP's MIT license. Using a separate MCP process is an integration
option, not a conclusion about redistribution obligations for Rynhult. Do not
copy the v2 application/prompts into an MIT-only catalog without reviewing those
terms. Retain upstream copyright/license notices for any vendored skill.

For reproducibility, SHA-256 of each downloaded
`https://codeload.github.com/OWNER/REPO/tar.gz/COMMIT` source archive:

```text
obra/superpowers       da34247c51339cdd6175b3590261f16e25b08d46669afd5b6fc3f842ab303e20
mattpocock/skills      846bd1de940f9b086f41c9189b3a5c9877e0e809f70ae8fdd7844087b7a3c0d2
colbymchenry/codegraph 7040dfc8c34edaa9a5cb2bcf12d3f889010b339134bcf837b5977eb0bb708f69
oraios/serena         dd56dfa93dbe30919316149240bef70acd4eaafae948dd6bf00cd0d16c95ad1a
```

These are locally measured archive digests, not signed provenance or a license
for automatic future updates. A full commit identifies the reviewed source;
reviewed per-file digests are preferable if GitHub regenerates an archive.

## 1. Superpowers: distinguish selected skills from the framework

**What it is.** An agent workflow framework: brainstorming, planning, TDD,
debugging, implementer/reviewer loops and skill routing. The full plugin's
[session-start hook](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/hooks/session-start)
injects `using-superpowers`. That
[skill](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/using-superpowers/SKILL.md)
aggressively requires looking for relevant skills before responding.
[Subagent-driven development](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/subagent-driven-development/SKILL.md)
adds fresh contexts and spec/code-quality reviews around tasks. Those can improve
quality, but instruction length is only part of their cost.

**Reads/sends.** Skills direct the existing agent to read code, plans and Git
state, run tests and delegate work. That information enters the agent/provider's
ordinary context; a Markdown skill is not an independent offline sandbox.
The optional brainstorming companion starts a local server and serves prototype
content. Its browser page loads a versioned Prime Radiant logo by default;
that request reveals normal connection metadata plus the version, even though
upstream says it does not send project/prompt contents. Disable it with
`SUPERPOWERS_DISABLE_TELEMETRY=1`; upstream also recognizes `DISABLE_TELEMETRY`
and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`. The static selected skills do
not contain this companion. See the
[disclosure](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/README.md#visual-companion-telemetry)
and [implementation](https://github.com/obra/superpowers/blob/2ce8aa15d05bd57213e6f746f953aafb7ded7a8b/skills/brainstorming/scripts/server.cjs).
A general absence of telemetry across all clients/dependencies was not proven.

**Install/update/integrity.** Upstream recommends agent marketplaces/plugins;
OpenCode also supports Git package refs, including a fixed tag. Those paths
inherit each host's update policy and are not equivalent to an ACC-reviewed
commit pin. Static files can be vendored at an immutable commit, with locally
recorded hashes and MIT notices, without running an installer. Rynhult already
does this: [catalog/manifest.toml](../../catalog/manifest.toml) pins four skills
at `bb92a77741419a4ab5f06e711a283343f1ada0c3`, and defers brainstorming's server.
The four entry files are byte-identical to their counterparts at the reviewed
newer upstream HEAD; that does not establish equality of their other files.

**Token evidence/verdict.** Our measurements below support a difference between
the small verification checklist, large planning/delegation flows and the whole
framework. They do not establish a universal cost multiplier. Public complaints
such as [issue 762](https://github.com/obra/superpowers/issues/762) are anecdotal,
not a controlled Rynhult comparison. Avoid adding the bootstrap/plugin or more
automatic orchestration. Keep the existing pinned quality skills pending a
quality-controlled trial of shorter ACC checklists; deleting TDD/debugging/
verification discipline is not justified by prompt size alone.

## 2. Matt Pocock: useful, but not uniformly tiny

At this snapshot the teaching skill is named **`teach`**, not `teach-me`.
The selected material is MIT Markdown/reference material. It contains no
standalone telemetry client or daemon; its instructions still cause agent file,
web, shell, subagent and tracker activity. The model provider receives the
material the agent reads. External websites receive research queries/requests;
trackers receive whatever an authorized workflow publishes. Generated lessons
or prototypes require their own asset/network review.

| Skill | What it reads/does | Recommendation and catalog fit |
|---|---|---|
| [`grill-me`](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/skills/productivity/grill-me/SKILL.md) + [`grilling`](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/skills/productivity/grilling/SKILL.md) | Wrapper invokes the interview primitive. Reads conversation and looks up facts, including via subagents; asks dependency-ordered question rounds and waits for the human. The wrapper carries no persistent workspace state. | **Add an adapted, explicit-only interview skill.** Flatten the wrapper/primitive to avoid unresolved cross-skill names; permit inline research when delegation is unavailable/unauthorized. Suitable for Architect or an opt-in planning session, not a mandatory gate on every coding task. |
| [`teach`](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/skills/productivity/teach/SKILL.md) | Reads mission, learning records/resources/assets and external primary sources. Writes `MISSION.md`, `RESOURCES.md`, `NOTES.md`, learning records and HTML lessons/reference assets; may open a lesson. No fixed external teaching backend. | **Add as a separate optional learning skill**, not a token optimization for coding. Prefer a dedicated standalone workspace/profile; personal learning notes should not enter a production repository. The current Researcher/Codex read-only default cannot simply host this write-oriented workflow. Require local assets and authorized browser use. |
| [`prototype`](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/skills/engineering/prototype/SKILL.md) | Reads nearby UI/domain code; loads either `LOGIC.md` or `UI.md`. Produces an in-memory HTML logic demo or UI variants. The UI branch normally retains the existing page's data fetching/auth. Requests throwaway branches and an issue/context pointer. | **Add an adapted explicit-only skill** for UX/Frontend. Use artificial data and an isolated test app; retain `docs/design/DESIGN.md` rules. Upstream's “no tests” is acceptable only for disposable demonstrations, never production code copied from them. Do not inherit live data fetching or publish/push merely because the template mentions it. |
| [`wayfinder`](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/skills/engineering/wayfinder/SKILL.md) | Maintains a map, child decision issues, blocking links, claims and resolution comments on a tracker. Invokes grilling, domain modeling, research and sometimes prototypes; dispatches research subagents and asks them to push branches. Reads repository/tracker configuration. | **Defer unchanged.** Useful for explicit multi-session planning, but not a small generic prompt replacement. Needs a local Markdown/ACC map adapter, concrete tracker-write authorization, concurrency handling and removal of automatic push requirements in local-only workspaces. Potential Architect/Orchestrator workflow, not a baseline skill. |

**Install/update/integrity, common to these skills.** The
[upstream README](https://github.com/mattpocock/skills/blob/5beb35b040e3de97e54fa9e386d7e42d15c295c2/README.md#installation-30-second-setup)
describes auto-updating plugins and `npx skills@latest add/update` for editable
copies. Neither is the curated-catalog delivery path. Vendor only reviewed
files plus all needed references at the full commit; retain MIT notices and
record hashes. Do not copy the entire plugin, execute setup/linking scripts or
add the moving installer as a session MCP command. The separate `vercel-labs/skills` installer was inspected narrowly at commit
`a80e4c9d12d607631249d4d7d059f835d4b2f13a` (package 1.7.4, MIT). Its
[telemetry module](https://github.com/vercel-labs/skills/blob/a80e4c9d12d607631249d4d7d059f835d4b2f13a/src/telemetry.ts)
is enabled unless `DISABLE_TELEMETRY` or `DO_NOT_TRACK` is set. Its schema can
carry source/skill identifiers and relative paths, selected/detected agents,
search queries, install metadata, CLI version and CI status to
`https://www.skills.sh/tele/t`; audit lookups send source/skill identifiers to
`https://www.skills.sh/tele/audit`. Actual fields also depend on caller filtering;
this is not an audit of every installer call site or dependency. The
[README](https://github.com/vercel-labs/skills/blob/a80e4c9d12d607631249d4d7d059f835d4b2f13a/README.md#telemetry)
says `DISABLE_TELEMETRY=1` or `DO_NOT_TRACK=1` disables both telemetry and audit
requests. These opt-outs do not stop package/source fetching or file installation.
Pinned file vendoring avoids that execution path entirely. Updates should be
reviewed diffs/new pins, not marketplace startup updates.

Preserve explicit invocation in both forms: `disable-model-invocation: true`
and `agents/openai.yaml`'s `policy.allow_implicit_invocation: false`. Upstream
includes both for grill-me, teach and Wayfinder; prototype is model-invocable
upstream, so the proposed adaptation deliberately changes that. Validate actual
provider behavior before claiming the flag is an authorization boundary.

## 3. Measured instruction size, not a token-bill benchmark

No tokenizer library (`tiktoken`, `tokenizers`, `transformers`) was available in
the existing application environment, and none was installed. We measured UTF-8
bytes, Unicode characters and whitespace-delimited words without importing
upstream code. The final column is **ceil(characters / 4), a size proxy only**:
it is not a Claude/Codex token count, input charge, total conversation cost or
cached-token price. References are loaded on demand, not necessarily together.

| Material read | UTF-8 bytes | Characters | Words | char/4 proxy |
|---|---:|---:|---:|---:|
| Superpowers startup `using-superpowers` entry | 3,192 | 3,180 | 492 | 795 |
| Superpowers brainstorming entry | 13,602 | 13,602 | 2,359 | 3,401 |
| Superpowers writing-plans entry | 10,335 | 10,317 | 1,639 | 2,580 |
| Superpowers subagent-driven-development entry | 19,506 | 19,391 | 2,855 | 4,848 |
| Rynhult's four existing Superpowers entries combined | 33,089 | 32,977 | 5,148 | 8,245 |
| All 15 upstream Superpowers skill entries, hypothetical combined load | 141,020 | 140,484 | 21,816 | 35,121 |
| Matt grill-me wrapper only | 157 | 157 | 22 | 40 |
| Matt grill-me **plus required grilling** | 2,206 | 2,194 | 350 | 549 |
| Matt teach entry | 9,733 | 9,733 | 1,522 | 2,434 |
| Matt teach with all four Markdown format references | 18,066 | 18,062 | 2,815 | 4,516 |
| Matt prototype entry + logic branch | 8,967 | 8,959 | 1,512 | 2,240 |
| Matt prototype entry + UI branch | 9,844 | 9,832 | 1,605 | 2,458 |
| Matt Wayfinder entry only | 12,380 | 12,380 | 2,075 | 3,095 |
| Wayfinder + grilling + domain-modeling + research entries | 18,569 | 18,433 | 3,027 | 4,609 |

The last Wayfinder row is a **partial dependency scenario**, excluding prototype,
repo-specific tracker notes and all spawned transcripts. The whole-Superpowers
row is not its startup cost. Rynhult's profiles select subsets and providers may
load just discovery metadata until a skill is invoked. Re-reading, review
cycles, model choice, interviews and subagents can outweigh entry-text size.
Thus “Matt is lighter” is defensible for the interview primitive, not a blanket
statement about teach/prototype/Wayfinder versus every Superpowers skill.

## 4. CodeGraph: promising local retrieval, with nonlocal defaults

**What it is/reads.** A TypeScript/Node CLI and MCP server that parses source into
SQLite symbol/reference/call graphs using Tree-sitter, with incremental sync,
worktree support and an optional local viewer. It reads project source, Git/
ignore/config metadata and returns **verbatim source** as well as structure.
This inspected retrieval path uses local parsing/querying, not a required cloud
embedding/model service. Its
[default MCP surface](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/src/mcp/tools.ts)
is only `codegraph_explore`; other defined tools can be re-enabled. Tool schemas
permit `projectPath`, and the implementation supports opening other projects.
Starting in one worktree is therefore not an enforced one-project boundary.
Indexes are sensitive derived source, even if a tool is called read-only.

**Writes/side effects.** Indexing writes databases/cache; synchronization can run
watchers and a detached daemon. Installer/init paths configure agents and may
install prompt/Git hooks. The server's
[initialize instructions](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/src/mcp/server-instructions.ts)
strongly steer the agent away from Read/Grep verification. That instruction is
not acceptable authority over ACC's security review or freshness checks. Do
not run `install`/`init` as a substitute for an explicit catalog integration.

**Network/telemetry.** “Local” describes code analysis, not zero egress.
[Telemetry](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/src/telemetry/index.ts)
is **default-on**. It POSTs to `https://telemetry.getcodegraph.com/v1/events`:
a random persisted machine ID, version, OS/architecture, Node major, CI status,
selected-agent names, MCP client names/versions, command/tool counts/errors, language names
and bucketed file counts/index durations. Daily rollups and bounded local queues
reduce frequency, not the need for consent. The
[disclosure](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/TELEMETRY.md)
says source, paths and query text are excluded; the inspected client payload
supports that narrower claim. The endpoint necessarily receives a connection;
repository code cannot prove the deployed infrastructure's logging/retention.

A separate [background GitHub release check](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/src/upgrade/update-check.ts)
can run daily. For any future trial, set **all** `DO_NOT_TRACK=1`,
`CODEGRAPH_TELEMETRY=0`, `CODEGRAPH_NO_UPDATE_CHECK=1`; the first also suppresses
the release check. Disable automatic prompt hooks (`CODEGRAPH_NO_PROMPT_HOOK=1`)
and install refresh (`CODEGRAPH_NO_INSTALL_REFRESH=1`), and actually deny runtime
egress. These switches are defense in depth, not proof that an unexecuted binary
makes no other connections. MCP source results still reach the chosen LLM through
the normal agent, even with CodeGraph egress blocked.

**Install/update/integrity.** The
[standalone installer](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/install.sh)
downloads a bundled Node/runtime release, defaults to latest, permits
`CODEGRAPH_VERSION`, edits executable symlinks and prunes older bundles. Its
reviewed Unix path does **not** verify the published checksums. The
[npm shim](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/scripts/npm-shim.js)
also downloads a release bundle; checksum verification is best-effort and can
skip when the sums file or entry is unavailable. Pinning npm alone is not a
complete binary-integrity policy. `codegraph upgrade` may fetch/run the installer
from moving `main`; a pinned release argument does not pin that installer.

Release v1.6.2 does publish
[SHA256SUMS](https://github.com/colbymchenry/codegraph/releases/download/v1.6.2/SHA256SUMS).
GitHub's release API reported the Linux x64 asset digest as
`ef0af416092128fb1ccc723786000b7edf4a6971fe604acd08bf966e4f37b828`.
We did **not** fetch/verify or execute that binary. A future operator should
review an exact release/source, record and require the archive hash, inspect
bundled licenses, provision a read-only versioned path and perform explicit
upgrades. Never put curl-to-shell or `npx ...@latest` in catalog `command`.
The source lockfile can support a reproducible source-build path, but it too
requires an actual build/distribution audit before making that claim.

### What the token evidence actually establishes

The [current upstream README](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/README.md#benchmark-results)
reports seven repositories, one architecture question per repository, medians
of four runs per arm on Claude Opus 4.8, remeasured 2026-08-05. It reports averages
of 62% fewer processed tokens and 44% lower cost, alongside fewer tool calls.
Tokens include repeated/cached inputs, cache creation and output across turns;
this is throughput, not peak context size. Recomputing from the rounded seven
rows gives 1.098M versus 3.458M processed tokens in summed per-repository medians,
and about **61.5% mean per-repository reduction**. This checks the arithmetic of
an upstream table; it is not an independent reproduction or our savings rate.

Two important counterweights are published by the same project:

- The [older 37-cell experiment](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/docs/benchmarks/codegraph-ab-matrix.md)
  reports fewer reads but **higher summed cost**, approximately $15.4 with versus
  $13.8 without. It used one run per arm and different questions/builds. It cannot
  be pooled with the later seven-repository result.
- The [retained-context experiment](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/docs/benchmarks/residual-context-occupancy.md)
  reports roughly **80% more retrieval context resident** with CodeGraph, with
  dense results remaining in the window. Some older delegation/context claims
  are explicitly flagged unverified in the
  [direct-versus-Explore writeup](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/docs/benchmarks/answer-directly-vs-explore-agent.md).

These are project-maintainer experiments, not Rynhult coding-task results. They
make a plausible case for navigation latency and bounded relevant context;
they do not prove universal lower bills, better answers, or savings after
indexing, MCP startup, debugging stale results and actual edits/tests.

## 5. Serena: a different and larger MCP alternative

**What it is/reads.** An IDE-like symbol/navigation/editing system backed by
language servers or a JetBrains plugin, not just a persisted call graph. It reads
source, language-server/project configuration, project memories and its own
user/project configuration. The reviewed v2
[configuration template](https://github.com/oraios/serena/blob/abb0a0f01b94bc8050086f0fb860077d17730324/src/serena/resources/serena_config.template.yml)
defaults to a **Python REPL interface**, with interactive/editing modes and a
local dashboard enabled. The tool inclusion list for the classic interface is
not a restriction on REPL APIs. It can edit files, execute commands and invoke
language tools; its own
[security model](https://github.com/oraios/serena/blob/abb0a0f01b94bc8050086f0fb860077d17730324/docs/02-usage/070_security.md)
assumes trusted repository/configuration/client and recommends sandboxing.
That conflicts with treating arbitrary cloned agent workspaces as a trusted
MCP execution/configuration source.

**Network/telemetry.** On startup it sends version, OS, backend, dashboard status,
context and interface to `https://oraios-software.de/serena_usage.php` unless
`SERENA_USAGE_REPORTING=false` (or recognized CI settings). See
[implementation](https://github.com/oraios/serena/blob/abb0a0f01b94bc8050086f0fb860077d17730324/src/serena/agent.py)
and [privacy statement](https://github.com/oraios/serena/blob/abb0a0f01b94bc8050086f0fb860077d17730324/docs/01-about/070_privacy.md).
The dashboard can check PyPI for updates. Language-server provisioning makes
additional package/download requests: the
[Rust adapter](https://github.com/oraios/serena/blob/abb0a0f01b94bc8050086f0fb860077d17730324/src/solidlsp/language_servers/rust_analyzer.py)
can install a rust-analyzer component through rustup, and the TypeScript adapter
can invoke npm installations. An opt-out for usage reporting does not disable
those other operations.

Also distinguish token estimators: the default configuration uses a local
character estimator. Optional tiktoken setup downloads encoding data; the
[Anthropic estimator](https://github.com/oraios/serena/blob/abb0a0f01b94bc8050086f0fb860077d17730324/src/serena/analytics.py)
passes the text being counted to the remote count-tokens API and can load an
API key from dotenv/environment. “No project data sent” cannot describe that
optional configuration. A future trial would need usage reporting off, dashboard
and remote estimators off, no credentials, preprovisioned pinned language servers,
and runtime egress denied. MCP results still enter the primary agent's context.

**Install/update/integrity.** The
[runbook](https://github.com/oraios/serena/blob/abb0a0f01b94bc8050086f0fb860077d17730324/docs/02-usage/010_installation.md)
uses `uv tool install -p 3.13 serena-agent` and `uv tool upgrade serena-agent`.
A version/commit can be pinned, but top-level pinning does not freeze transitive
packages, Python provisioning or language-server downloads. The repo has
`uv.lock` and a
[download-hash table](https://github.com/oraios/serena/blob/abb0a0f01b94bc8050086f0fb860077d17730324/src/solidlsp/resources/downloaded_dependency_hashes.json);
its generic downloader's
[checksum argument is optional](https://github.com/oraios/serena/blob/abb0a0f01b94bc8050086f0fb860077d17730324/src/solidlsp/ls_utils.py).
Audit each selected language's provisioning path; do not infer universal hash
verification from the existence of that table. Rynhult would need a frozen,
operator-built environment and explicit redistribution/license review.

**Token evidence/verdict.** Upstream publishes an
[evaluation](https://github.com/oraios/serena/blob/abb0a0f01b94bc8050086f0fb860077d17730324/docs/04-evaluation/000_evaluation-intro.md)
in which agents perform and self-assess roughly twenty tasks using both toolsets.
That is useful capability evidence, especially for semantic refactors, but not
an independently reproduced billing benchmark for our Python/React repository.
Defer default inclusion. If semantic rename/reference needs later justify it,
evaluate a constrained classic read-oriented interface separately from the
REPL/editor, with fresh security and license review.

## 6. Is our repository large enough?

Measured **committed main `eaf2cbadea36cec5b076e36c76454a5b3ff05e6c`**, before
this research was added. Counts use Git blobs, so ignored node_modules, virtual
environments, build outputs, local artifacts and other branches are absent.
“Lines” means newline count plus a final unterminated line in UTF-8 text without
NUL bytes, not logical statements/SLOC. Categories overlap and should not be summed.

| Scope | Files/entries | Physical lines |
|---|---:|---:|
| All tracked entries | **1,884** (1,882 regular blobs, 2 symlinks) | — |
| UTF-8 text | 1,592 | **275,235** |
| Code extensions/shebang scripts, including tests/vendor/generated | **841** | **148,859** |
| Production/tool code in src, web/src, desktop source, deploy, scripts, bin | 441 | 75,555 |
| Python application `src/acc/**/*.py` | 234 | 44,946 |
| React `web/src` TS/TSX, excluding generated api-types | 104 | 14,368 |
| Test code by tests/web-e2e/spec/test naming | 341 | 63,636 |
| Docs text | 444 | 65,391 |
| Vendored catalog text/data | 138 | 28,614 |
| Lockfiles | 8 | 17,184 |
| Files matching CodeGraph's extension map, excluding docs/catalog/generated api-types | **771** | **133,991** |

Regular tracked data is 45,784,062 bytes; UTF-8 text is 17,674,913 bytes. The
771-file figure is a **static candidate count, not a measured CodeGraph index**:
ignore defaults, generated files, file-level YAML handling and extractor coverage
will change it. Several important extensionless Python helpers under `bin/` and
`deploy/` are absent from this suffix-based estimate. Do not assume the graph
covers guard/root-helper logic merely because Python is supported. The suffix
map is in
[grammars.ts](https://github.com/colbymchenry/codegraph/blob/69af19ace270f2e940444d514de6dde971df12df/src/extraction/grammars.ts).

**Judgment:** this is a medium-sized graph candidate, not a giant monorepo.
Repeated API → broker → node/provider investigations could benefit now; isolated
one-file fixes may be cheaper with `rg` and targeted reads. Cross-process RPC,
FastAPI dependency injection, dynamic Python and React state behavior need
source/tests regardless of graph edges. Total docs/lockfile lines overstate the
code-graph problem. There is enough structure for a trial, not evidence yet for
always-on indexing in every profile/worktree.

## 7. Rynhult integration and guard constraints

The catalog already separates skills, MCP servers and profiles; imports pin
GitHub commits for review. See [catalog.py](../../src/acc/server/catalog.py),
[catalog_import.py](../../src/acc/server/catalog_import.py),
[catalog/manifest.toml](../../catalog/manifest.toml) and
[profiles](../../catalog/profiles). Native plugins are deliberately disabled in
Rynhult's Codex baseline. Do not reintroduce an auto-updating plugin as a shortcut.
Also note the loader's historical `providers = ["claude", "codex"]` convention
means **all supporting providers**, not a strict two-provider restriction; new
invocation/permission metadata needs actual provider qualification.

**Skills:** follow existing vendoring/provenance/license conventions, namespace
or flatten cross-skill dependencies, keep all linked references, and make
interview/teaching/prototyping opt-in. Reconcile the instructions with house
workflow, design rules, local-only Git, user time/authorization and session
artifacts. Review updates through new commits; regenerate third-party notices
with `scripts/third_party_notices.py` when items are actually added. Do not claim
this research has added or enabled any skill/profile.

**MCP:** provision an exact reviewed binary/environment at a read-only versioned
operator path, not an installer in catalog `command`. Prefer session-scoped
stdio, one authorized worktree/snapshot and a private index; do not share a
home-wide daemon/index across projects, groups or accounts. A fixed launch cwd
or `--path` alone does not neutralize CodeGraph's tool-level `projectPath`.
Constrain/reject that argument and MCP roots/project switching in an audited
wrapper or server fork; isolate the filesystem as well. Disable hooks, viewer,
update/telemetry traffic and unattended dependency installs. The real CodeGraph
entrypoint is `serve --mcp` with optional `--path`/`--no-watch`, not an assumed
`mcp` command. Disabling a watcher requires a trustworthy explicit refresh/
stale-result policy. No known flag alone establishes all these restrictions.

**Guard:** [acc-guard](../../bin/acc-guard) checks recognized commands and explicit
path/search arguments. Internal recursive indexing, arbitrary REPL code,
language-server subprocesses, opaque query strings and server-initiated network
requests are not a per-file/per-action authorization boundary. Hidden commands
inside an MCP tool are not automatically re-run through shell approval. The
[Guide's guard limits](../wiki/User-Guide/Guardrails.md) still apply. A selected
catalog item is trusted executable capability, not just descriptive metadata.
Keep secrets, sign-in stores, vault data, run folders, `.git`, caches containing
another project's code and escaping symlinks outside its readable filesystem.
Do not assume `.gitignore` is a secrecy control: tracked secret-like files and
configuration overrides still matter. Returned code/comments are untrusted
content, not higher-priority instructions; never accept “do not verify” guidance
over a security or correctness check. Do not treat the still-unqualified
production container design as an already-enforced isolation guarantee.

## 8. Proposed qualification before a CodeGraph catalog entry

This proposal was **not run**. Use a disposable, credential-free environment,
with resources limited and network only for fetching reviewed dependencies,
then deny egress during execution. Mount a clean allowed snapshot read-only;
write only a private disposable index. Do not let installers edit agent homes,
hooks, Git config or real workspaces. Audit the actual build's license bundle
and verify required hashes rather than using best-effort installer verification.

Compare the existing `rg`/read workflow with a narrow CodeGraph tool on the same
commit/model/instructions. Use at least eight representative tasks, repeated
and order-balanced: two small lookups, two cross-file flow questions, two
change-impact questions, a source edit/refresh case and a permission/security
question requiring an extensionless helper. Grade answers against independently
identified source/test evidence, including explicit unresolved edges. Keep the
baseline free of CodeGraph hooks/CLI/daemon access and account for subagents.

Record separately: fresh/cache-read/cache-write/output tokens, actual price,
processed-token totals, retained/peak context, tool-result bytes, tool calls,
wall time, cold indexing CPU/RAM/disk and warm refresh cost. Test two worktrees,
stale files, uncommitted edits, renamed/deleted symbols, symlink escapes,
projectPath/roots switching, secret-like fixtures, opaque MCP commands and
network attempts. No real credentials or production API calls are necessary.

Suggested acceptance bar, not a claimed result: no authorization/data-boundary
regression; no lower answer/correctness grade; a meaningful repeated navigation
latency improvement; and at least 20% lower median **actual model cost** on the
cross-file tasks after fixed MCP/index overhead is accounted for. If latency
wins but cost does not, label it a navigation option rather than a token saver.
Start as explicit Researcher/Architect navigation, keep Full-stack/Frontend/
Backend defaults unchanged until this evidence exists.

## Reproducing the static measurements

Read committed blobs using `git ls-tree -rlz REV` and a single
`git cat-file --batch` process; do not count the working directory. Code suffixes
were `.py .ts .tsx .js .jsx .mjs .cjs .rs .sh .ps1 .cs .c .h .cpp .swift`, plus
extensionless files beginning `#!`. Production scopes were `src/`, `web/src/`,
`desktop/src/`, `desktop/src-tauri/src/`, `deploy/`, `scripts/`, `bin/`; tests were
`tests/`, `web/e2e/` or names containing `.spec.`/`.test.`. Generated
`web/src/lib/api-types.ts` was excluded from application/candidate scopes if
present. Parse the upstream `EXTENSION_MAP` object as text to obtain the separate
771-file candidate count; do not import its module. Text/catalog/docs/locks
counts use the same UTF-8/newline rules.

For each skill row, read only the named `SKILL.md` and the explicitly listed
closure/reference files at the pinned snapshot, concatenate in that order, and
calculate `len(raw_bytes)`, `len(raw_bytes.decode('utf-8'))`,
`len(re.findall(r'\S+', text))`, and `ceil(len(text)/4)`. Do not count the full
plugin docs as automatically loaded instructions. Raw measurement JSON, source
pins/file hashes and the independent measurement script are retained in the
session's `.acc/artifacts/skills-research/`; no upstream code is needed in the
installed host environment. Runtime token savings, packet traces, extraction
quality and stale-index behavior remain unverified by this research.
