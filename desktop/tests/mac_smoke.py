"""Mac-only qualification entry point; its process monitor is fixture-tested on Linux."""
import argparse
import json
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

READY = re.compile(r"desktop_main_ready_ms=(\d+) child_pid=(\d+)")
QUIT = """function run(argv) {
    ObjC.import("AppKit");
    var app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(Number(argv[0]));
    if (!app.terminate) throw new Error("Native quit request was refused");
}"""


def live(process):
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def wait_gone(processes, timeout):
    deadline = time.monotonic() + timeout
    while any(live(process) for process in processes):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Owned processes still running: {[p.pid for p in processes if live(p)]}")
        time.sleep(.05)


def snapshot(app, tracked):
    cpu, rss = {}, 0
    for process in [app, *app.children(recursive=True)]:
        try:
            times = process.cpu_times()
            rss += process.memory_info().rss
        except psutil.NoSuchProcess:
            continue
        tracked.add(process)
        cpu[process] = times.user + times.system
    return cpu, rss


def run_smoke(binary, home, log, *, idle_seconds=300, ready_timeout=40, quit_timeout=15,
              stop="native", expected_python=None):
    if idle_seconds <= 0 or stop not in {"native", "terminate", "kill"}:
        raise ValueError("Positive idle duration and a recognized stop method are required")
    home.mkdir(mode=0o700, parents=True)
    began = time.monotonic()
    with log.open("w") as output:
        host = subprocess.Popen([str(binary)], cwd=home, stdout=output, stderr=output,
                                env={"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                                     "PATH": "/usr/bin:/bin"})
    app = psutil.Process(host.pid)
    tracked = {app}
    try:
        deadline = time.monotonic() + ready_timeout
        while not (ready := READY.search(log.read_text())):
            if host.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f"Authenticated main readiness missing; see {log}")
            time.sleep(.05)
        main = psutil.Process(int(ready[2]))
        if main.ppid() != host.pid:
            raise RuntimeError("Reported main is not a child of the launched host")
        tracked.add(main)
        interpreter = Path(main.exe()).resolve()
        if expected_python is not None and interpreter != expected_python.resolve():
            raise RuntimeError(f"Main did not use bundled Python: {interpreter}")
        report = {"platform": sys.platform, "stop": stop, "main_ready_ms": int(ready[1]),
                  "observed_ready_ms": round((time.monotonic() - began) * 1000),
                  "startup_scope": "Fresh profile, warm cache; authenticated main ready, NOT UI interactive",
                  "main_interpreter": str(interpreter)}
        time.sleep(min(2, idle_seconds))
        initial, _ = snapshot(app, tracked)
        latest = dict(initial)
        rss = []
        idle_began = time.monotonic()
        while True:
            remaining = idle_seconds - (time.monotonic() - idle_began)
            if remaining <= 0:
                break
            time.sleep(min(1, remaining))
            if host.poll() is not None or not live(main):
                raise RuntimeError("Host or main exited during idle sampling")
            now, memory = snapshot(app, tracked)
            latest.update(now)
            rss.append(memory)
        elapsed = time.monotonic() - idle_began
        report["idle"] = {
            "scope": "Host + observed descendants only; independent WebKit XPC services may be excluded. "
                     "RSS counts shared pages repeatedly; not Linux PSS or a whole-app memory qualification.",
            "seconds": elapsed,
            "cpu_percent_one_core": (sum(latest.values()) - sum(initial.values())) / elapsed * 100,
            "rss_mean_mib": sum(rss) / len(rss) / 2**20, "rss_peak_mib": max(rss) / 2**20,
            "observed_process_names": sorted({process.name() for process in tracked if live(process)}),
        }
        quit_began = time.monotonic()
        if stop == "native":
            subprocess.run(["osascript", "-l", "JavaScript", "-e", QUIT, str(host.pid)],
                           check=True, timeout=quit_timeout, capture_output=True, text=True)
        else:
            host.send_signal(signal.SIGTERM if stop == "terminate" else signal.SIGKILL)
        status = host.wait(timeout=quit_timeout)
        expected = {"native": 0, "terminate": -signal.SIGTERM, "kill": -signal.SIGKILL}[stop]
        if status != expected:
            raise RuntimeError(f"Unexpected host exit: {status}, expected {expected}")
        wait_gone(tracked, quit_timeout)
        text = log.read_text()
        if "desktop_main_stopped" not in text or "Main shutdown:" in text:
            raise RuntimeError(f"Missing graceful main shutdown evidence; see {log}")
        report["shutdown_ms"] = round((time.monotonic() - quit_began) * 1000)
        return report
    finally:
        if host.poll() is None:
            tracked.update(app.children(recursive=True))
            host.terminate()
            try:
                host.wait(timeout=quit_timeout)
            except subprocess.TimeoutExpired:
                host.kill()
                host.wait(timeout=quit_timeout)
        for process in tracked:
            try:
                if live(process):
                    process.kill()
            except psutil.NoSuchProcess:
                pass
        wait_gone(tracked, quit_timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--idle-seconds", type=float, default=300)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("The qualification entry point requires a logged-in macOS desktop")
    if args.idle_seconds <= 0:
        parser.error("--idle-seconds must be positive")
    args.artifacts.mkdir(parents=True, exist_ok=True)
    binary = args.binary.resolve(strict=True)
    python = binary.parents[1] / "Resources/desktop/runtime/python/bin/python3"
    for stop in ("native", "terminate", "kill"):
        log = args.artifacts / f"desktop-macos-{stop}.log"
        with tempfile.TemporaryDirectory(prefix="rdm.", dir="/tmp") as scratch:
            report = run_smoke(binary, Path(scratch) / "home", log, expected_python=python, stop=stop,
                               idle_seconds=args.idle_seconds if stop == "native" else 1)
        path = args.artifacts / f"desktop-macos-{stop}.json"
        path.write_text(json.dumps(report, indent=2) + "\n")
        print(path)


if __name__ == "__main__":
    main()
