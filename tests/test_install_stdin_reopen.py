"""Fresh installs stopped at the Caddy root copy (Oct 29 VM test): `runuser -u acc -- install /dev/stdin` makes the
acc user reopen a pipe that root owns, which Linux refuses. Installer steps that pipe into another account must let
that account read the inherited descriptor (cat), never reopen /dev/stdin or /dev/fd/0 by path."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_installer_step_reopens_stdin_as_another_account():
    offenders = []
    for path in sorted((ROOT / 'deploy').rglob('*')):
        if not path.is_file() or path.suffix not in ('', '.sh'):
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(r'runuser\b.*\s/dev/(stdin|fd/0)\b', line):
                offenders.append(f'{path.relative_to(ROOT)}:{number}')
    assert not offenders, offenders


def test_installer_creates_the_backup_folder_the_unit_requires():
    """acc-backup.service names /var/backups/acc in ReadWritePaths without '-', so the folder must exist."""
    unit = (ROOT / 'deploy/backup/acc-backup.service').read_text()
    install = (ROOT / 'deploy/install.sh').read_text()
    if 'ReadWritePaths=/var/backups/acc' in unit:
        assert 'install -d -o root -g root -m 0700 /var/backups/acc' in install
