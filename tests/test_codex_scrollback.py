"""Inline mode is requested by default and gated against the account's actual CLI."""
import subprocess

import pytest

from acc.providers import codex
from acc.server.compiler import LaunchInputs, build_args


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("supported", [False, True])
def test_inline_launch_and_resume(monkeypatch, resume, supported):
    calls = []

    def help(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "--no-alt-screen" if supported else "--help", "")

    monkeypatch.setattr(codex, "run", help)
    codex.supports_inline.cache_clear()
    args = build_args(LaunchInputs(provider="codex"), run_dir="/r", tmux_name="t",
                      native_id="native-id", resume=resume)
    assert "--no-alt-screen" in args
    for _ in range(2):
        actual = codex.supported_launch_args("/custom/codex", args)
        assert ("--no-alt-screen" in actual) is supported
        assert (actual[:2] == ["resume", "native-id"]) is resume
    assert calls == [["/custom/codex", "--help"]]
    codex.supports_inline.cache_clear()


def test_inline_disabled_does_not_probe(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("disabled inline mode must not probe CLI")

    monkeypatch.setattr(codex, "run", unexpected)
    args = build_args(LaunchInputs(provider="codex", options={"keep_scrollback": False}),
                      run_dir="/r", tmux_name="t", native_id=None, resume=False)
    assert "--no-alt-screen" not in codex.supported_launch_args("/custom/codex", args)


@pytest.mark.parametrize("failure", [OSError(), subprocess.TimeoutExpired("codex", 5)])
def test_failed_probe_preserves_older_cli(monkeypatch, failure):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(codex, "run", fail)
    codex.supports_inline.cache_clear()
    assert codex.supported_launch_args("missing", ["--no-alt-screen", "--model", "x"]) == ["--model", "x"]
    codex.supports_inline.cache_clear()


@pytest.mark.parametrize("supported", [False, True])
async def test_broker_launch_checks_selected_cli(api, project, env, monkeypatch, supported):
    import json

    from helpers import wait_for
    from test_supervisor import launch, state_is

    checked = []

    def support(exe):
        checked.append(exe)
        return supported

    monkeypatch.setattr(codex, "supports_inline", support)
    session = await launch(api, project, provider="codex")
    await wait_for(state_is(api, session["id"], "idle"))
    rows = (env["run_dir"] / session["id"] / "fake.argv.jsonl").read_text().splitlines()
    argv = json.loads(rows[-1])
    assert ("--no-alt-screen" in argv) is supported
    assert checked == [env["broker"].providers["codex"]]


@pytest.mark.parametrize("provider", ["codex", "claude"])
async def test_mouse_stays_off(api, project, env, provider):
    from helpers import wait_for
    from test_supervisor import launch, state_is

    session = await launch(api, project, provider=provider)
    await wait_for(state_is(api, session["id"], "idle"))
    result = subprocess.run(
        ["tmux", "-L", env["broker"].tmux_socket, "show-options", "-v", "-t",
         session["tmux_name"], "mouse"], capture_output=True, text=True, check=True,
    )
    # -A includes inherited defaults for the untouched non-Codex session.
    inherited = subprocess.run(
        ["tmux", "-L", env["broker"].tmux_socket, "show-options", "-Av", "-t",
         session["tmux_name"], "mouse"], capture_output=True, text=True, check=True,
    )
    assert inherited.stdout.strip() == "off"
    assert result.stdout.strip() == ""


@pytest.mark.parametrize("operation", ["send_text", "interrupt"])
async def test_delivery_leaves_copy_mode(api, project, env, operation):
    """Exercise the real tmux copy-mode boundary on a private test session."""
    from helpers import wait_for
    from test_supervisor import launch, state_is

    from acc.broker import ops

    session = await launch(api, project, provider="codex")
    await wait_for(state_is(api, session["id"], "idle"))
    config = env["broker"]
    name = session["tmux_name"]
    ops.scroll_history(config, name, -1)
    assert ops.tmux(config, "display-message", "-p", "-t", f"={name}:", "#{pane_in_mode}").stdout.strip() == "1"
    if operation == "send_text":
        ops.send_text(config, name, "copy-mode message intact", "codex")
        received = env["run_dir"] / session["id"] / "fake.received"
        assert "copy-mode message intact" in received.read_text()
    else:
        # Escape is inert in canonical mode: submit it to prove it reached the CLI.
        ops.interrupt(config, name, "codex")
        ops.tmux(config, "send-keys", "-t", f"={name}:", "Enter", check=True)
        received = env["run_dir"] / session["id"] / "fake.received"
        async def escaped():
            return received.exists() and "\x1b" in received.read_text()
        await wait_for(escaped)
    assert ops.tmux(config, "display-message", "-p", "-t", f"={name}:", "#{pane_in_mode}").stdout.strip() == "0"


@pytest.mark.parametrize('provider', ['claude', 'opencode', None])
def test_non_codex_delivery_never_probes_copy_mode(monkeypatch, provider):
    from acc.broker import ops

    def unexpected(*args, **kwargs):
        pytest.fail('non-Codex delivery must not query tmux')

    monkeypatch.setattr(ops, 'tmux', unexpected)
    ops.cancel_scrolled_codex(None, 'acc-test', provider)


@pytest.mark.parametrize('wheel,scrolled,expected', [('off', 'on', False), ('on', 'off', False), ('on', 'on', True)])
def test_delivery_probes_only_wheel_scrolled_panes(monkeypatch, wheel, scrolled, expected):
    from acc.broker import ops

    cancelled = []
    monkeypatch.setattr(ops, 'tmux', lambda config, *args, **kwargs: subprocess.CompletedProcess(
        args, 0, {'@acc-wheel': wheel, '@acc-scrolled': scrolled}[args[-1]], ''))
    monkeypatch.setattr(ops, 'cancel_copy_mode', lambda *args: cancelled.append(True))
    ops.cancel_scrolled_codex(None, 'acc-test', 'codex')
    assert bool(cancelled) is expected


@pytest.mark.parametrize('result', [None, subprocess.CompletedProcess([], 1, '', 'failed')])
def test_failed_copy_mode_probe_preserves_scroll_state(monkeypatch, result):
    from acc.broker import ops

    calls = []

    def tmux(*args, **kwargs):
        calls.append(args)
        return result

    monkeypatch.setattr(ops, 'tmux', tmux)
    with pytest.raises(ops.OpError, match='copy mode'):
        ops.cancel_copy_mode(None, 'acc-test')
    assert len(calls) == 1  # No keys or state reset after an unknown mode result.
