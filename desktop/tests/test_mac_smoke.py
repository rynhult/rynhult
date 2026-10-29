"""Exercise the Mac harness monitor with private, real EOF-owned fixture processes."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import psutil
import pytest


def load_monitor():
    source = Path(__file__).with_name("mac_smoke.py")
    assert source.exists(), "Mac smoke monitor has not been implemented"
    spec = importlib.util.spec_from_file_location("mac_smoke", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fixture_app(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    quit_tool = tools / "osascript"
    quit_tool.write_text(f"#!{sys.executable}\nimport os,signal,sys\nos.kill(int(sys.argv[-1]),signal.SIGUSR1)\n")
    quit_tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    def create(*, ready=True, graceful=True):
        host = tmp_path / "fixture-host"
        child = "import sys;sys.stdin.read();" + (
            "print('desktop_main_stopped',file=sys.stderr,flush=True)" if graceful else "")
        host.write_text(f"""#!{sys.executable}
import json, os, pathlib, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", {child!r}], stdin=subprocess.PIPE)
pathlib.Path("fixture-pids.json").write_text(json.dumps([os.getpid(), child.pid]))
def quit(signum, frame):
    child.stdin.close()
    child.wait()
    sys.exit(0)
signal.signal(signal.SIGUSR1, quit)
if {ready!r}:
    print(f"desktop_main_ready_ms=12 child_pid={{child.pid}}", file=sys.stderr, flush=True)
while True:
    time.sleep(.01)
""")
        host.chmod(0o755)
        return host
    return create


@pytest.mark.parametrize("stop", ["native", "terminate", "kill"])
def test_monitor_measures_ready_processes_and_verifies_eof_shutdown(tmp_path, fixture_app, stop):
    monitor = load_monitor()
    home = tmp_path / "home"
    report = monitor.run_smoke(fixture_app(), home, tmp_path / "app.log", idle_seconds=.05,
                               ready_timeout=3, quit_timeout=3, stop=stop)
    assert report["main_ready_ms"] == 12
    assert report["idle"]["cpu_percent_one_core"] >= 0
    assert report["idle"]["rss_mean_mib"] > 0
    assert "XPC" in report["idle"]["scope"]
    assert report["stop"] == stop
    for pid in json.loads((home / "fixture-pids.json").read_text()):
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


@pytest.mark.parametrize("ready, graceful, message", [
    (False, True, "readiness"),
    (True, False, "graceful"),
])
def test_monitor_rejects_missing_evidence_and_cleans_only_its_processes(
        tmp_path, fixture_app, ready, graceful, message):
    monitor = load_monitor()
    home = tmp_path / "home"
    with pytest.raises(RuntimeError, match=message):
        monitor.run_smoke(fixture_app(ready=ready, graceful=graceful), home, tmp_path / "app.log",
                          idle_seconds=.05, ready_timeout=.5, quit_timeout=3)
    for pid in json.loads((home / "fixture-pids.json").read_text()):
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
