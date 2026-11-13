# Reviewed account tool proposals

This directory is a versioned review source, **not executable authority**. No recipe
is published or qualified by this build. Do not read a task checkout to authorize
an install. Deployment retains separately reviewed, owner-published immutable
recipe, metadata and artifact bytes in accd's service-owned database.

V1 recipes use the closed schema enforced by `server/install_catalog.py`: `id`,
`revision`, `adapter` (`npm-extractor-v1`), exact npm `name` and `version`, canonical
`registry`, artifact SHA-512, canonical metadata SHA-256, complete normalized file
`manifest` and its digest, `bins`, exact broker `runtime_id`, and fixed or lowered
`limits`. Digests use `approvals.digest` canonical JSON; floats are forbidden.

A maintainer prepares a proposal and canonical-origin fetch in the service
context. An independent person reviews the package identity, artifact, scripts,
dependencies, output modes/bytes and command collisions. The owner approves
publication. The trusted `install_catalog.publish` seam fetches in the service context outside
any database write lock. Its retention step requires distinct named
reviewer/owner identities and reinspects bytes in isolated Python before storage.
The stored `packument_digest` hashes the canonical registry **version document**
(`/<name>/<version>`), not the full package packument.
There is deliberately no agent catalog-publication tool or wildcard consent.

Qualification is separate from review/publication. An owner qualifies the exact
protocol, interpreter, adapter/inspector bytes, local machine, kernel, runtime and
recipe on disposable hosts, after independent implementation security GO.
`QUALIFIED_TOOL_EXECUTORS` stays empty in both accd and broker. Adding `npm` grants
nothing and never enables the legacy four-field consume route.

Qualification must record the pinned interpreter path and SHA-256 for both accd
and broker. Their interpreter bytes must agree; the interpreter hash is cached
once per process, so restart both after upgrading it. The pinned node-tar
differential corpus remains a disposable-host qualification requirement. Common
registry fields and empty `directories` still fail closed pending the schema
decision recorded in the build handoff.

## Mandatory interpreter differential evidence

Owner qualification must run the corpus with the **exact absolute accd and
broker Python paths whose interpreter SHA-256 values the fingerprint pins**.
The pytest driver can use the existing test venv: each role's parser runs in a
separate stdlib-only subprocess under its selected interpreter with `-I` and a
fixed environment. Neither selected interpreter needs pytest. This does not
contact the live service or execute package code.

Set `TEST_PYTHON`, `ACCD_PINNED_PYTHON`, `BROKER_PINNED_PYTHON` and `EVIDENCE_BASE`
to owner-verified paths on a disposable qualification host, then run:

```sh
PYTHONPATH=src "$TEST_PYTHON" -m pytest -q -s \
  tests/test_install_artifact_headers.py::test_fixed_header_mutant_corpus_matches_host_node_tar \
  --install-qualification \
  --install-accd-python "$ACCD_PINNED_PYTHON" \
  --install-broker-python "$BROKER_PINNED_PYTHON" \
  --install-qualification-record "$EVIDENCE_BASE"
```

Equivalent environment parameters are `ACC_INSTALL_QUALIFICATION=1`,
`ACC_INSTALL_ACCD_PYTHON`, `ACC_INSTALL_BROKER_PYTHON` and
`ACC_INSTALL_QUALIFICATION_RECORD`. The mandatory flag/env makes missing Node.js
or node-tar **fail**, never skip. Both interpreter paths and an evidence path are
required; an unavailable selected interpreter fails without fallback. Normal
non-qualification runs default both roles to the test Python and may skip only
when the host oracle is absent. Qualification flags are test controls and cannot
enable an executor or bypass the production root refusal.

A fresh evidence base such as `/tmp/install-corpus/evidence.json` creates
`evidence-accd.json` and `evidence-broker.json`; existing records are never
replaced. Each records the actual **node-tar version, resolved module path and
package.json path**, Node version/path, requested and actual interpreter paths,
interpreter SHA-256/version/prefix, stdlib tarfile path/hash, isolated mode, UID,
inspector path/hash, corpus counts and comparison result. Retain both with the
owner qualification record and the reviewed revision/recipe/runtime/fingerprint.
Compare interpreter SHA-256 with the pinned fingerprint inputs; compare the
inspector hash with the reviewed build. Do not use default-interpreter test
results to qualify different deployed interpreters. Restart and requalify after
changing the pinned runtime or relevant parser bytes. This evidence is one part
of owner qualification and never adds a fingerprint to the empty registry.
