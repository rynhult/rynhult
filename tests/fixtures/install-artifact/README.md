B1 review regression archives, reconstructed from the three scenarios in
`handover/review-delegated-installs.md` (the original `/tmp` probes were not
provided). Regenerate with `python3 tests/fixtures/install-artifact/generate.py`.
All bytes are inert fixtures and must never be installed or executed.

- `bad-checksum-duplicate.tgz`: malicious cli, invalid checksum block, benign duplicate cli.
- `bad-checksum-hidden.tgz`: malicious cli, invalid checksum block, hidden file.
- `gnu-prefix.tgz`: consistent GNU magic; `package/x.js` has nonzero prefix bytes.

The inspector must reject all three. Qualification still requires the pinned
node-tar differential corpus on disposable hosts.

Round 2 B2 fixtures are reconstructed from all five scenario classes in the
review (the original fuzz inputs and `/tmp` probes were not supplied):

- `b2-linkname.tgz`: a regular file with a nonempty linkname.
- `b2-devmajor.tgz`: regular-file devmajor starts with `0xff`.
- `b2-{mtime,uid,gid}-base256.tgz`: out-of-safe-integer base-256 fields.
- `b2-prefix-tail.tgz`: byte 475 is nonzero after the first prefix NUL.
- `b2-dropped-header-body.tgz`: the dropped regular-file header's data is itself
  a valid header naming `#!/usr/bin/env node\nconsole.log(1)\n`.

`generate.py` regenerates B1 and B2 fixtures using
`tests/install_artifact_corpus.py`. The B2 archives were accepted at `0ff73361a3`
and must now all refuse. No package code is executed.

`npm-pack-long-path.tgz` is a **real npm pack**, generated with installed npm
11.19.1 (Node 24.22.1, vendored node-tar 7.5.22), offline with scripts disabled.
The source package contains only `package.json`, `cli.js`, and a 120/120/90-byte
component path. The latter's header has both a PAX path and a nonzero ustar
prefix; the full PAX path must win. Regenerate using
`python3 tests/fixtures/install-artifact/generate-npm-pack.py`. This command
uses a temporary HOME, empty temporary config files and cache; it never installs
a package, changes account configuration or uses the network. Tar bytes can
change with the installed npm version; this checked-in archive is the regression.

`test_install_artifact_headers.py` also creates 2,048 deterministic single-byte
header mutants (every byte, values 0, 32, 55 and 255, checksums repaired except
checksum-field probes), plus nine valid seeds covering both formats, safe binary
size, PAX path/size, UTF-8 and prefix lengths around node-tar's 130-byte branch.
All cases accepted by the inspector must match host node-tar listing and
`tar.x({strip:1})` file sets, bytes and directories. The test skips only if host
Node.js/node-tar is absent and installs no replacements. `node-tar-probe.cjs`
only extracts into pytest scratch; it never executes files. It grants owner read
permission **after extraction** solely to observe bytes of mode mutants: npm's
umask-sensitive modes are not compared with the trusted extractor's deliberately
normalized modes. The full differential/fuzz corpus remains an owner
qualification item.

Round 3 N1: `n1-pax-path-directory.tgz` is a zero-byte regular file with PAX path
`package/dd/`; it must refuse. The generator reconstructs it, and the differential
corpus includes it as a tenth seed (nine accepted seeds, one refused seed).
An actual directory header with the same PAX path remains accepted.

The corpus now runs twice, under explicit accd/broker interpreter parameters via
`inspect-corpus.py` with `-I`. The driver can run in the test venv while either
selected interpreter has only the standard library. Mandatory qualification
flags/env and role-specific JSON evidence are documented in
`catalog/tool-recipes/README.md`; node-tar version and resolved path are always
discovered and recorded. Missing oracle binaries/modules may skip ordinary
checks but must fail mandatory qualification. Interpreter errors never skip.
