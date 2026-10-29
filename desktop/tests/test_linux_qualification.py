"""The account-creating qualification probe must refuse host execution."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux disposable systemd qualification only')


def probe():
    path = Path(__file__).with_name('linux_phase1_probe.py')
    spec = importlib.util.spec_from_file_location('linux_qualification', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('change', ['host-map', 'root-outer-map', 'init', 'container', 'uid'])
def test_account_probe_refuses_host_or_wrong_identity(monkeypatch, change):
    module = probe()
    files = {'/proc/1/comm': 'systemd\n', '/proc/self/uid_map': '0 1004 1\n1 427680 65536\n',
             '/run/systemd/container': 'podman\n'}
    if change == 'host-map':
        files['/proc/self/uid_map'] = '0 0 4294967295\n'
    elif change == 'root-outer-map':
        files['/proc/self/uid_map'] = '0 0 65536\n'
    elif change == 'init':
        files['/proc/1/comm'] = 'bash\n'
    elif change == 'container':
        files['/run/systemd/container'] = 'docker\n'
    monkeypatch.setattr(module, 'Path', lambda name: SimpleNamespace(read_text=lambda: files[name]))
    monkeypatch.setattr(module.os, 'geteuid', lambda: 1000 if change == 'uid' else 0)
    with pytest.raises(RuntimeError, match='disposable rootless Podman'):
        module.require_disposable(root=True)


def test_probe_accepts_systemd_with_non_root_host_mapping(monkeypatch):
    module = probe()
    files = {'/proc/1/comm': 'systemd\n', '/proc/self/uid_map': '0 1004 1\n1 427680 65536\n',
             '/run/systemd/container': 'podman\n'}
    monkeypatch.setattr(module, 'Path', lambda name: SimpleNamespace(read_text=lambda: files[name]))
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    module.require_disposable(root=True)


@pytest.mark.parametrize('manager,command,listing', [
    ('pacman', ('pacman', '-Qlq', 'rynhult-linux-spike'), '/usr/lib/Rynhult Linux spike/desktop/setup.py\n'),
    ('dpkg-query', ('dpkg-query', '-L', 'rynhult-linux-spike'), '/usr/lib/Rynhult Linux spike/desktop/setup.py\n'),
])
def test_resources_uses_installed_distribution_package_database(monkeypatch, manager, command, listing):
    module = probe()
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/bin/' + name if name == manager else None)
    calls = []

    def query(*args):
        calls.append(args)
        return SimpleNamespace(stdout=listing)

    monkeypatch.setattr(module, 'run', query)
    assert module.resources() == Path('/usr/lib/Rynhult Linux spike')
    assert calls == [command]
