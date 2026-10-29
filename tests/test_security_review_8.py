"""Security review 8 (2026-10-26): one regression per fixed finding."""
import pytest

from acc.broker import ops
from acc.server import vault


def launch_with(env, variables, provider='claude'):
    return ops.launch(env['broker'], {'acc_id': 'abcdef123456', 'tmux_name': 'acc-sr8', 'provider': provider,
                                      'args': [], 'cwd': str(env['workspaces'] / 'demo'), 'files': {}, 'env': variables})


# SR8-1: Rynhult writes each CLI's guard hooks into its default config folder (and Claude's per-session settings);
# a project key that moves the folder starts the CLI without them, or with another folder's config and git config.
@pytest.mark.parametrize('name', ['CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'COPILOT_HOME', 'CURSOR_CONFIG_DIR',
                                  'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'GNUPGHOME', 'NPM_CONFIG_NODE_OPTIONS'])
def test_config_folder_variables_refused_at_both_boundaries(name, env):
    with pytest.raises(vault.VaultError):
        vault.check_variable(name)
    with pytest.raises(ops.OpError, match='not allowed'):
        launch_with(env, {name: '/tmp/untrusted'})


@pytest.mark.parametrize('name', ['OPENCODE_CONFIG', 'OPENCODE_CONFIG_DIR', 'OPENCODE_CONFIG_CONTENT', 'OPENCODE_PERMISSION'])
def test_opencode_settings_cannot_come_from_a_key(name):
    # Rynhult passes its own OPENCODE_ values to the broker, so only the vault can tell a key's from Rynhult's.
    with pytest.raises(vault.VaultError):
        vault.check_variable(name)


def test_ordinary_tool_credentials_stay_allowed():
    for name in ('COPILOT_GITHUB_TOKEN', 'CODEX_API_KEY', 'OPENAI_API_KEY', 'NPM_TOKEN'):
        assert vault.check_variable(name) == name


# SR8-2: SO_PEERCRED names the process that called listen(). The guard used to listen before exec'ing
# `podman run --detach`, which exits once the container is up, so the pane found no cgroup for that PID and refused
# the real relay (and a reused PID was all the unit check rested on). The relay child, which lives as long as the unit,
# must be the listener.
def test_the_pane_trusts_the_relay_after_the_guard_process_exits(tmp_path):
    import os
    import subprocess
    import sys
    import textwrap
    import time
    from pathlib import Path

    from acc.broker import container_attach
    folder = tmp_path / 'attach'
    folder.mkdir(mode=0o700)
    path = folder / 'attach.sock'
    guard = Path(__file__).resolve().parents[1] / 'deploy/containers/acc-container-exec'
    script = textwrap.dedent(f'''
        import importlib.machinery, importlib.util, os, signal, sys
        loader = importlib.machinery.SourceFileLoader('guard', {str(guard)!r})
        guard = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(guard)
        def relay(listener, runtime, uid):  # the real relay serves; this one holds the listener until killed
            print(os.getpid(), flush=True)
            signal.pause()
        guard.relay = relay
        guard.start_relay(guard.listen({str(path)!r}, os.getuid()), 'runtime', os.getuid())
        # the guard process now execs `podman run --detach`, which exits once the container is up
    ''')
    guard_process = subprocess.Popen([sys.executable, '-I', '-c', script], stdout=subprocess.PIPE, text=True)
    relay_pid = int(guard_process.stdout.readline())  # the relay child holds the pipe open: read one line only
    guard_process.stdout.close()
    try:
        assert guard_process.wait(timeout=30) == 0  # the listen()/exec process is gone before the pane connects
        conn = container_attach.connect(str(path), wait=5)
        with conn:
            unit = container_attach.cgroup_of(os.getpid())  # the relay child stays in the guard's (here: our) cgroup
            assert container_attach.relay_is_the_units(conn, unit)
    finally:
        os.kill(relay_pid, 9)
        for _ in range(50):
            try:
                os.kill(relay_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.05)


def test_the_build_runbook_names_the_build_account_risk():
    # SR8-3 (listed for an owner decision): the image is built where the building account's other processes can write.
    from pathlib import Path
    readme = (Path(__file__).resolve().parents[1] / 'deploy/containers/README.md').read_text()
    assert 'SR8-3' in readme and 'runs no sessions' in readme and 'ps -u' in readme
