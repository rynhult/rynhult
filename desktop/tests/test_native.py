"""The real Tauri window on a private GTK Broadway display and a private D-Bus/AT-SPI bus.

Opt-in (DESKTOP_NATIVE_CHECK=1). Needs the release build (or DESKTOP_APP=<binary>, e.g. the
extracted .deb), desktop/runtime from build-runtime.sh, the read-only node_modules link and
PLAYWRIGHT_BROWSERS_PATH. Everything runs in test-owned temporary directories; no TCP port
is opened except kernel-selected loopback ones. DESKTOP_MEASURE=1 adds an idle sample of the
whole app tree (DESKTOP_IDLE_SECONDS, default 300).
"""
import json
import os
import re
import shlex
import shutil
import signal
import socket
import socketserver
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / '.acc/artifacts'
SYSTEM_PYTHON = '/usr/bin/python3'  # PyGObject for the AT-SPI inspector only; never the app's runtime

pytestmark = pytest.mark.skipif(os.environ.get('DESKTOP_NATIVE_CHECK') != '1',
                                reason='Opt-in native window check (DESKTOP_NATIVE_CHECK=1)')


def app_binary():
    return Path(os.environ.get('DESKTOP_APP') or ROOT / 'desktop/src-tauri/target/release/rynhult-desktop-spike')


def report_name(stem):
    return f'{stem}-{os.environ.get("DESKTOP_REPORT", "release")}'


BUS_CONFIG = '''<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-Bus Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig><type>session</type><listen>unix:path={path}</listen><auth>EXTERNAL</auth>
<policy context="default"><allow send_destination="*" eavesdrop="true"/><allow receive_sender="*"/><allow own="*"/></policy>
</busconfig>'''


class _Proxy(socketserver.ThreadingTCPServer):
    """Broadway's web client is HTTP on a unix socket; Chromium needs a (kernel-chosen) TCP port."""
    daemon_threads = True

    def __init__(self, target):
        self.target = target
        super().__init__(('127.0.0.1', 0), _Pipe)


class _Pipe(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.socket(socket.AF_UNIX)
        upstream.connect(self.server.target)

        def pump(source, sink):
            try:
                while data := source.recv(65536):
                    sink.sendall(data)
            except OSError:
                pass
            finally:
                sink.close()
        threading.Thread(target=pump, args=(upstream, self.request), daemon=True).start()
        pump(self.request, upstream)


@contextmanager
def display():
    if getattr(os, "geteuid", lambda: None)() == 0:
        pytest.skip("Real desktop main/WebKit require an ordinary owner; run native integration unprivileged")
    runtime = Path(tempfile.mkdtemp(prefix='rdk.', dir='/tmp'))  # short: unix socket paths are limited
    processes, proxy, driver = [], None, None
    try:
        # A private bus that only carries AT-SPI: no service directories, so nothing is ever activated.
        flags = shlex.split(subprocess.run(['pkg-config', '--cflags', '--libs', 'gtk+-3.0'],
                            check=True, capture_output=True, text=True).stdout)
        subprocess.run(['cc', '-shared', '-fPIC', str(ROOT / 'desktop/tests/gtk_dpi.c'),
                        '-o', str(runtime / 'gtk-dpi.so'), *flags, '-ldl'], check=True, timeout=30)
        (runtime / 'bus.conf').write_text(BUS_CONFIG.format(path=runtime / 'bus'))
        bus = subprocess.Popen(['dbus-daemon', f'--config-file={runtime}/bus.conf', '--nofork',
                                '--print-address=1'], stdout=subprocess.PIPE, text=True)
        processes.append(bus)
        address = bus.stdout.readline().strip()
        processes.append(subprocess.Popen(['broadwayd', ':1', '--unixsocket', str(runtime / 'http.sock')],
                                          env={'XDG_RUNTIME_DIR': str(runtime)},
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for _ in range(100):
            if (runtime / 'http.sock').exists():
                break
            time.sleep(.05)
        proxy = _Proxy(str(runtime / 'http.sock'))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        # Connect a display client before GTK/WebKit initialize. Otherwise Broadway
        # starts with an invalid monitor DPI and Tauri caches a wrong scale factor.
        driver = subprocess.Popen(['node', str(ROOT / 'desktop/tests/broadway.mjs'),
                                   f'http://127.0.0.1:{proxy.server_address[1]}/'],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                                  env={**os.environ, 'PLAYWRIGHT_BROWSERS_PATH': '/srv/acc/tools/ms-playwright'})
        assert driver.stdout.readline().strip() == 'ok'
        yield runtime, address, driver
    finally:
        if driver:
            driver.stdin.close()
            driver.wait(30)
        if proxy:
            proxy.shutdown()
            proxy.server_close()
        for process in processes:
            process.terminate()
            process.wait(5)
        shutil.rmtree(runtime)


@contextmanager
def launch(runtime, address, home, python=None):
    # Broadway has no XSettings daemon; its default -1 DPI breaks WebKit CSS units.
    # Set only this test profile, never the account's GTK configuration.
    gtk = home / 'config/gtk-3.0'
    gtk.mkdir(parents=True, exist_ok=True)
    (gtk / 'settings.ini').write_text('[Settings]\ngtk-xft-dpi=98304\ngtk-application-prefer-dark-theme=1\n')
    log = home / 'app.log'
    env = {'HOME': str(home), 'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_RUNTIME_DIR': str(runtime),
           'PATH': '/usr/bin:/bin', 'DBUS_SESSION_BUS_ADDRESS': address, 'AT_SPI_BUS_ADDRESS': address,
           'GDK_BACKEND': 'broadway', 'BROADWAY_DISPLAY': ':1', 'LD_PRELOAD': str(runtime / 'gtk-dpi.so')}
    if python:
        env['RYNHULT_SPIKE_PYTHON'] = str(python)
    started = time.monotonic()
    with log.open('w') as output:
        host = subprocess.Popen([str(app_binary())], cwd=home, env=env, stdout=output, stderr=output)
    try:
        yield host, started, log
    finally:
        if host.poll() is None:
            host.kill()
            host.wait()


def tree(address):
    result = subprocess.run([SYSTEM_PYTHON, '-I', str(ROOT / 'desktop/tests/atspi.py'), address],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def nodes(apps):
    stack = list(apps)
    while stack:
        item = stack.pop()
        yield item
        stack.extend(item.get('children', []))


def wait_for(address, predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        apps = tree(address)
        if found := [item for item in nodes(apps) if predicate(item)]:
            return found[0], apps
        time.sleep(.2)
    raise AssertionError(f'Timed out; last tree: {json.dumps(apps)[:4000]}')


def main_pid(log):
    for _ in range(300):
        if match := re.search(r'child_pid=(\d+)', log.read_text()):
            return int(match[1])
        time.sleep(.1)
    raise AssertionError(log.read_text())


def setup_field(item):
    return item['name'] == 'Projects' and item['role'] == 'heading' and 'showing' in item['states']


def focused(address):
    return [(item['role'], item['name']) for item in nodes(tree(address)) if 'focused' in item['states']]


def wait_gone(pids, timeout):
    began = time.monotonic()
    while time.monotonic() - began < timeout:
        if not any(psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
                   for pid in pids):
            return time.monotonic() - began
        time.sleep(.05)
    raise AssertionError(f'Still running after {timeout}s: {[p for p in pids if psutil.pid_exists(p)]}')


def database_check(home):
    database = home / 'config/rynhult-desktop-spike/data_dir/acc.db'
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
        return connection.execute('PRAGMA quick_check').fetchone()[0]


def test_window_keyboard_accessibility_and_quit(tmp_path):
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    report = {'binary': str(app_binary()), 'display': 'GTK Broadway (software rendering), private D-Bus as AT-SPI bus'}
    with display() as (runtime, address, driver), launch(runtime, address, tmp_path) as (host, started, log):
        wait_for(address, setup_field)
        report['launch_to_interactive_ms'] = round((time.monotonic() - started) * 1000)
        report['main_ready_ms'] = int(re.search(r'desktop_main_ready_ms=(\d+)', log.read_text())[1])
        pid = main_pid(log)
        interpreter = str(Path(f'/proc/{pid}/exe').resolve())
        report['main_interpreter'] = interpreter
        assert 'desktop/runtime/python/bin/python3' in interpreter, interpreter  # bundled, not system Python

        try:

            def send(command):
                driver.stdin.write(command + '\n')
                driver.stdin.flush()
                assert driver.stdout.readline().strip() == 'ok'
            steps = [('start', focused(address))]
            send('click 700 140')  # the title bar: gives the toplevel keyboard focus, as a window manager would
            for key in ['Tab'] * 10 + ['Shift+Tab'] * 3:
                send(f'press {key}')
                steps.append((key, focused(address)))
            send(f'shot {ARTIFACTS / (report_name("desktop-native-window") + ".png")}')
        finally:
            driver.stdin.close()
            driver.wait(30)
        report['keyboard_focus'] = steps
        names = {name for _, now in steps for _, name in now if name}
        assert len(names) >= 3, steps

        apps = tree(address)
        (ARTIFACTS / (report_name('desktop-native-atspi-tree') + '.json')).write_text(json.dumps(apps, indent=1))
        roles = {(item['role'], item['name']) for item in nodes(apps)}
        assert ('heading', 'Projects') in roles, roles
        assert ('push button', 'New project') in roles, roles

        app = psutil.Process(host.pid)
        if os.environ.get('DESKTOP_MEASURE') == '1':
            report['idle'] = idle_sample(app, float(os.environ.get('DESKTOP_IDLE_SECONDS', '300')))
        family = [app.pid, *(child.pid for child in app.children(recursive=True))]
        report['processes'] = sorted({psutil.Process(p).name() for p in family})

        close = next(item for item in nodes(apps) if item['role'] == 'push button' and item['name'] == 'Close')
        quit_began = time.monotonic()
        subprocess.run([SYSTEM_PYTHON, '-I', str(ROOT / 'desktop/tests/atspi.py'), address, '--action',
                        close['bus'], close['path']], check=True, timeout=30)
        assert host.wait(20) == 0, log.read_text()[-3000:]
        report['quit_ms'] = round((time.monotonic() - quit_began) * 1000)
        wait_gone(family, 5)
        assert database_check(tmp_path) == 'ok'
        assert 'desktop_main_stopped' in log.read_text()  # lifespan shutdown completed
        assert 'Main shutdown' not in log.read_text()  # and the host never had to kill it
    (ARTIFACTS / (report_name('desktop-native-report') + '.json')).write_text(json.dumps(report, indent=1))


@pytest.mark.parametrize('signal_', [signal.SIGKILL, signal.SIGTERM], ids=['crash', 'terminate'])
def test_host_crash_stops_main_and_webkit(tmp_path, signal_):
    report = {'signal': signal_.name}
    with display() as (runtime, address, _), launch(runtime, address, tmp_path) as (host, _, log):
        wait_for(address, setup_field)
        pid = main_pid(log)
        family = [host.pid, *(child.pid for child in psutil.Process(host.pid).children(recursive=True))]
        assert pid in family
        os.kill(host.pid, signal_)  # a crash, or a session logout: no Tauri exit handler runs
        host.wait()
        # The main's stdin pipe closes with the host; it shuts down gracefully, WebKit follows.
        report['main_gone_after_s'] = round(wait_gone([pid], 10), 2)
        report['all_gone_after_s'] = round(wait_gone(family, 10), 2)
        assert 'desktop_main_stopped' in log.read_text()  # graceful, not killed with the host
        assert database_check(tmp_path) == 'ok'
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / (report_name(f'desktop-native-{signal_.name.lower()}') + '.json')).write_text(json.dumps(report, indent=1))


def idle_sample(app, seconds):
    def snapshot():
        tree_ = [app, *app.children(recursive=True)]
        by_name = {}
        for process in tree_:
            # PSS splits shared WebKit/GTK pages between processes; summed RSS counts them twice.
            times, memory = process.cpu_times(), process.memory_full_info()
            entry = by_name.setdefault(process.name(), [0.0, 0, 0])
            entry[0] += times.user + times.system
            entry[1] += memory.rss
            entry[2] += memory.pss
        return by_name
    before, began, samples = snapshot(), time.monotonic(), []
    while time.monotonic() - began < seconds:
        time.sleep(1)
        now = snapshot().values()
        samples.append((sum(entry[1] for entry in now), sum(entry[2] for entry in now)))
    after, elapsed = snapshot(), time.monotonic() - began
    return {'seconds': round(elapsed, 1), 'scope': 'host + WebKit web/network processes + Python main; '
            'excludes the Broadway display server',
            'cpu_percent_one_core': round(sum(after[n][0] - before.get(n, [0])[0] for n in after) / elapsed * 100, 3),
            'rss_sum_mean_mib': round(sum(rss for rss, _ in samples) / len(samples) / 2**20, 1),
            'rss_sum_peak_mib': round(max(rss for rss, _ in samples) / 2**20, 1),
            'pss_mean_mib': round(sum(pss for _, pss in samples) / len(samples) / 2**20, 1),
            'pss_peak_mib': round(max(pss for _, pss in samples) / 2**20, 1),
            'per_process_end': {name: {'cpu_percent': round((after[name][0] - before.get(name, [0])[0]) / elapsed * 100, 3),
                                       'rss_mib': round(after[name][1] / 2**20, 1),
                                       'pss_mib': round(after[name][2] / 2**20, 1)} for name in after}}
