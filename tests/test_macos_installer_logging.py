"""Exercise only the Mac installer's logger in a disposable HOME; never run installation."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[1] / 'deploy/node/macos/install.sh'


@pytest.mark.parametrize('shell', ['bash', 'dash'])
@pytest.mark.parametrize('status', [0, 23])
def test_logger_drains_before_exit_with_portable_shell_syntax(tmp_path, shell, status):
    # dash rejects process substitution; the logging fragment deliberately needs
    # only syntax also supported by the Mac's Bash 3.2, without coproc or wait -n.
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f'{shell} not installed')
    text = INSTALL.read_text()
    logger = text[text.index('redact()'):text.index('\nREPO=')]
    # Slow the consumer so an exit that fails to drain cannot pass accidentally.
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    awk = bin_dir / 'awk'
    awk.write_text('#!/bin/sh\nsleep 0.1\nexec /usr/bin/awk "$@"\n')
    awk.chmod(0o700)
    script = ('set -eu\n' + logger + '\n'
              'i=0\nwhile [ "$i" -lt 3000 ]; do\n'
              '  printf "log-row-%s\\n" "$i"\n  i=$((i + 1))\ndone\n'
              'printf "final-log-row\\n" >&2\n'
              f'exit {status}\n')
    result = subprocess.run([executable, '-c', script], env={**os.environ, 'HOME': str(tmp_path), 'PATH': f'{bin_dir}:/usr/bin:/bin'},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == status, result.stderr
    assert 'not a child' not in result.stderr
    if status:
        assert 'log-row-2999\nfinal-log-row\n' in result.stderr
        assert 'Full log:' in result.stderr
    log = tmp_path / 'Library/Logs/ACC/install-node.log'
    lines = log.read_text().splitlines()
    assert lines == [f'log-row-{i}' for i in range(3000)] + ['final-log-row']
    assert not list(log.parent.glob('.logger.*'))
