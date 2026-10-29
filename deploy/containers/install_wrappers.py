#!/usr/bin/python3 -I
"""Wrap the pinned image binaries in place; never install on the native host."""
import ast
import glob
from pathlib import Path

# The source tree's copy beside this file (tests, deploy/containers), else the image's (the build runs this from
# /opt/acc/build, with the policy already in /opt/acc/runtime).
POLICY_SOURCES = (Path(__file__).with_name('sandbox_policy.py'), Path('/opt/acc/runtime/sandbox_policy.py'))


def ownership_rule(source):
    """sandbox_policy's own root_uids() and INITIAL_MAP, as source: the wrapper must decide whose files the policy may be
    before it may load the policy, so it carries the policy's rule verbatim instead of a second copy."""
    tree = ast.parse(source)
    parts = [ast.get_source_segment(source, node) for node in tree.body
             if (isinstance(node, ast.FunctionDef) and node.name == 'root_uids')
             or (isinstance(node, ast.Assign) and [getattr(t, 'id', None) for t in node.targets] == ['INITIAL_MAP'])]
    if len(parts) != 2:
        raise SystemExit('sandbox_policy.py must define INITIAL_MAP and root_uids() once each')
    return '\n\n'.join(parts) + '\n'


def wrap(executable, kind, policy_source=None):
    executable = Path(executable)
    real = executable.with_name(executable.name + '.acc-real')
    executable.rename(real)
    write(executable, kind, real, policy_source)


def write(executable, kind, real, policy_source=None):
    if policy_source is None:
        policy_source = next(path for path in POLICY_SOURCES if path.is_file()).read_text()
    executable.write_text('''#!/usr/bin/python3 -IB
import os
import runpy
import stat
import sys
from pathlib import Path

# Root's files show as the overflow uid in the rootless session container (attempt 15). Copied from sandbox_policy.py
# when the image is built:
''' + ownership_rule(policy_source) + '''
owners = root_uids()
policy = Path('/opt/acc/runtime/sandbox_policy.py')
for path in (policy, *policy.parents):
    info = path.lstat()
    expected = stat.S_ISREG if path == policy else stat.S_ISDIR
    sticky = path != policy and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
    if info.st_uid not in owners or (info.st_mode & 0o022 and not sticky) or not expected(info.st_mode):
        sys.exit('unsafe container policy')
''' + f"runpy.run_path(str(policy))['execute']({kind!r}, {str(real)!r}, sys.argv[1:])\n")
    executable.chmod(0o755)


if __name__ == '__main__':
    for pattern, kind in [('/opt/acc/cli/node_modules/@openai/codex-linux-x64/vendor/*/bin/codex', 'codex'),
                          ('/ms-playwright/chromium-*/chrome-linux*/chrome', 'chrome')]:
        matches = glob.glob(pattern)
        if len(matches) != 1:
            raise SystemExit('one pinned executable required: ' + kind)
        wrap(matches[0], kind)
    # Copilot is the pinned Node running the unpacked package; its wrapper checks its hooks before every start.
    write(Path('/usr/local/bin/copilot'), 'copilot', '/usr/local/bin/node')
