"""Node 22.23+ rejects a boolean process.exit() code (ERR_INVALID_ARG_TYPE), which broke the installers' Node check
on a fresh Fedora 44 VM (Oct 30). Every Node version check must pass a number."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_node_version_checks_exit_with_a_number():
    bad = []
    for path in sorted((ROOT / 'deploy').rglob('*')):
        if path.is_file() and path.suffix in ('', '.sh'):
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if 'process.exit(' in line and re.search(r'process\.exit\([^)]*<[^?]*\)', line) and '?' not in line:
                    bad.append(f'{path.relative_to(ROOT)}:{number}')
    assert not bad, bad
