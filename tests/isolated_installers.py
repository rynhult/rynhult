"""Dry-run shell subjects still read state: redirect probes in test copies, never in production scripts."""
import shlex
import tempfile
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


@contextmanager
def dry_run_subject(argv, environment):
    paths = {str(REPO / 'deploy/node/install.sh'), str(REPO / 'deploy/acc-install'), str(REPO / 'deploy/setup.sh')}
    if len(argv) < 2 or str(argv[1]) not in paths or '--dry-run' not in argv:
        yield argv, {}
        return
    with tempfile.TemporaryDirectory(prefix='installer-subject-') as scratch:
        root = Path(scratch)
        replacements = {}

        def stage(source):
            source = Path(source)
            text = source.read_text()
            for line in text.splitlines():
                if line.startswith('REPO=$(cd '):
                    text = text.replace(line, 'REPO=' + shlex.quote(str(REPO)))
            text = text.replace('acc_compat_lib="$(dirname "$0")/acc-env.sh"',
                                'acc_compat_lib=' + shlex.quote(str(REPO / 'deploy/lib/acc-env.sh')))
            text = text.replace('BROKER_ENV=/etc/acc/broker', 'BROKER_ENV=' + shlex.quote(str(root / 'broker-env')))
            text = text.replace('/etc/acc-node.env', str(root / 'node.env'))
            text = text.replace('/etc/systemd/system/accd.service', str(root / 'main.service'))
            text = text.replace('/etc/default/accd', environment.get('ACC_SETUP_TEST_ACCD', str(root / 'accd.env')))
            if source.name == 'acc-install':
                for relative in ('deploy/node/install.sh', 'deploy/setup.sh'):
                    child = stage(REPO / relative)
                    text = text.replace(f'"$REPO/{relative}"', shlex.quote(str(child)))
            target = root / source.name
            target.write_text(text)
            target.chmod(0o700)
            replacements[str(target)] = str(source)
            return target

        command = list(argv)
        command[1] = str(stage(argv[1]))
        yield command, replacements


def restore_source_names(result, replacements):
    # Assertions about displayed commands retain their production paths; execution used scratch copies.
    for temporary, source in replacements.items():
        result.stdout = result.stdout.replace(temporary, source)
        result.stderr = result.stderr.replace(temporary, source)
    return result
