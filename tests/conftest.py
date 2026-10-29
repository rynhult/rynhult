from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from helpers import TOTP_AT, preview_listener, wait_for
from host_isolation import home_environment
from owned_tmux import Owner, alive, pin_process, signal_process
from sqlalchemy.engine import make_url

from acc.broker.config import BrokerConfig
from acc.broker.server import serve
from acc.server import auth
from acc.server import supervisor as supervisor_module
from acc.server.app import create_app
from acc.server.config import Settings

pytest_plugins = ["postgres_matrix"]

REPO = Path(__file__).resolve().parents[1]


def pytest_configure(config):
    # A real agent session exports both spellings. RYNHULT_ wins over ACC_,
    # so fixture-only ACC_RUN_DIR overrides otherwise still target its spool.
    # Never let test subprocesses inherit the live session's hook/guard targets.
    from acc.env import PROTECTED_SESSION_VARIABLES
    for key in PROTECTED_SESSION_VARIABLES:
        os.environ.pop(key, None)
    os.environ['ACC_TEST'] = '1'  # No real telemetry transport in any test process.
    if config.option.basetemp is None:
        # Do not participate in other pytest processes' numbered-directory retention/cleanup.
        root = tempfile.TemporaryDirectory(prefix="acc-pytest-")
        config.option.basetemp = str(Path(root.name) / "tmp")
        config.add_cleanup(root.cleanup)


def fake_cli_processes(directory: Path) -> dict[int, str]:
    """Live fake CLIs below an exclusively owned test directory (pid -> start time)."""
    prefix = (str(directory) + "/").encode()
    found = {}
    for path in Path("/proc").glob("[0-9]*"):
        try:
            fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
            if path.stat().st_uid != os.getuid() or not any(
                    arg.startswith(prefix) and arg.rsplit(b"/", 1)[-1].startswith(b"fake-")
                    for arg in (path / "cmdline").read_bytes().split(b"\0")[:3]):
                continue
        except (OSError, IndexError):
            continue
        if fields[0] != "Z":  # a zombie is its parent's to reap, and already gone
            found[int(path.name)] = fields[19]
    return found


def assert_no_fake_clis(directory: Path):
    deadline = time.monotonic() + 2  # a pane's CLI may still be exiting from its SIGTERM
    while (left := fake_cli_processes(directory)) and time.monotonic() < deadline:
        time.sleep(0.05)
    handles = []
    try:
        for pid, identity in left.items():
            fd = pin_process(pid, identity)
            if fd is not None:
                handles.append(fd)
                signal_process(fd, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while any(alive(fd) for fd in handles) and time.monotonic() < deadline:
            time.sleep(0.02)
        remaining = sum(alive(fd) for fd in handles)
    finally:
        for fd in handles:
            os.close(fd)
    if left:
        pytest.fail(f"test left {len(left)} fake CLI process(es) running: {sorted(left)}; "
                    f"{remaining} remain after cleanup", pytrace=False)


@pytest.fixture(scope="session", autouse=True)
def no_leftover_fake_clis_at_session_end(tmp_path_factory):
    directory = tmp_path_factory.getbasetemp()
    yield
    assert_no_fake_clis(directory)


@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session):
    if hasattr(session.config, "workerinput"):
        return
    try:
        # A crashed worker cannot run fixtures; its directories remain below the controller's base.
        assert_no_fake_clis(Path(session.config.option.basetemp).resolve())
    except pytest.fail.Exception as error:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        print(f"Fake agent leak at session end: {error}", file=sys.stderr)


# Defined before owned_tmux, so its teardown runs after the tmux servers and their panes are gone.
@pytest.fixture(autouse=True)
def no_leftover_fake_clis(request):
    """Fail leaking tests promptly; the worker-session check also covers tests without env and late launches."""
    yield
    env = request.node.funcargs.get("env")
    if isinstance(env, dict) and "home" in env:
        assert_no_fake_clis(env["home"].parent / "bin")


@pytest.fixture(autouse=True)
def isolated_orchestrator_environment(monkeypatch):
    """Legacy scenarios opt in to exactly the slices they exercise.

    Production defaults are tested with these explicit test overrides removed in
    test_orchestrator_defaults.py. Never inherit a live agent/harness environment.
    """
    from acc.env import names
    for flag in ('ACC_ORCHESTRATOR', 'ACC_ORCHESTRATOR_V2', 'ACC_ORCHESTRATOR_V2_DELIVERY'):
        for key in names(flag):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv(flag, '0')


@pytest.fixture(autouse=True)
def umask_is_restored():
    """The umask is process-wide: a test that leaves 077 behind makes later tests' folders 0700 in that worker (under
    xdist, whichever tests it happens to run next). Restore it, and fail the test that changed it."""
    before = os.umask(0o022)
    os.umask(before)
    yield
    after = os.umask(before)
    if after != before:
        pytest.fail(f"test left the umask at {after:04o} (was {before:04o})", pytrace=False)


@pytest.fixture(autouse=True)
def owned_tmux(monkeypatch):
    from acc.broker import ops

    owner = Owner()
    original = ops.tmux

    def tmux(config, *args, **kwargs):
        owner.register(config.tmux_socket)
        return original(config, *args, **kwargs)

    monkeypatch.setattr(ops, 'tmux', tmux)
    try:
        yield owner
    finally:
        owner.close()


@pytest.fixture(autouse=True)
def fixed_totp_time(monkeypatch):
    # Replace this module's clock, not time.time globally: throttles and asyncio keep their real monotonic clock.
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: TOTP_AT, monotonic=time.monotonic))


@pytest.fixture
def env(tmp_path, monkeypatch, owned_tmux, request):
    """Isolated world: own HOME, tmux socket, run dir, workspace root and fake CLIs."""
    from acc.server import devices, host, remote_access

    def no_signer(_body):
        # The device CA signer is the host's root helper (/run/acc-device-ca.sock); background stages ask it for the
        # authority (edge.authority). A test machine has none: tests that need one install their own fake.
        raise FileNotFoundError("no device CA signer in tests")
    monkeypatch.setattr(devices, "sign", no_signer)

    async def no_remote_access_helper(_message):
        raise FileNotFoundError("no remote access helper in tests")  # the host's /run/acc-remote-access.sock
    monkeypatch.setattr(remote_access, "request", no_remote_access_helper)
    monkeypatch.setattr(host, "live", lambda: {
        "cpu_percent": 5.0, "cpu_count": 4, "load": [0.1, 0.1, 0.1],
        "memory": {"total": 16 * 1024**3, "used": 4 * 1024**3, "percent": 25.0},
        "swap_percent": 0.0, "net": {"sent": 1024, "received": 2048}, "measured_at": time.time(),
    })
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    (home.parent / "xdg-run").mkdir(mode=0o700, exist_ok=True)
    for key, value in home_environment(home).items():
        monkeypatch.setenv(key, value)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("fake-claude", "fake-codex", "fake-copilot", "fake-antigravity", "fake-cursor", "fake-opencode", "fake-gh"):
        (bin_dir / name).symlink_to(REPO / "tests/fake_cli.py")
    monkeypatch.setenv("FAKE_HOOK", str(REPO / "bin/acc-hook"))
    workspaces = tmp_path / "workspaces"
    (workspaces / "demo").mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    socket_name = f"acc-test-{uuid.uuid4().hex[:8]}"
    owned_tmux.register(socket_name)
    broker = BrokerConfig(
        socket_path=tmp_path / "broker.sock", run_dir=run_dir, tmux_socket=socket_name,
        container_policy=tmp_path / "container-policy.json",
        allowed_roots=(workspaces,), allowed_uids=frozenset({os.getuid()}),
        providers={"claude": str(bin_dir / "fake-claude"), "codex": str(bin_dir / "fake-codex"),
                   "copilot": str(bin_dir / "fake-copilot"),
                   "antigravity": str(bin_dir / "fake-antigravity"),
                   "cursor": str(bin_dir / "fake-cursor"), "opencode": str(bin_dir / "fake-opencode"),
                   "gh": str(bin_dir / "fake-gh")},
        hook_path=str(REPO / "bin/acc-hook"), catalog_local_dir=tmp_path / "catalog-local",
        catalog_dir=REPO / "catalog", containers_socket=tmp_path / "containers.sock",
        containers_attach_root=tmp_path / "containers-attach",
        git_remote_base=str(tmp_path / "remotes"),
    )
    settings = Settings(
        data_dir=tmp_path / "data", run_dir=run_dir, root_run_dir=tmp_path / "run-root", broker_socket=broker.socket_path,
        hook_path=str(REPO / "bin/acc-hook"), statusline_path=str(REPO / "bin/acc-statusline"),
        ingest_interval=0.1, reconcile_interval=0.3, retry_backoff=(0.0, 0.0, 0.0),
        claude_verify_timeout=5.0, codex_settle_seconds=1.0, changes_interval=0.3, history_interval=0.3,
        allowed_origins=("http://acc", "http://127.0.0.1:18740"), cookie_secure=False,
        static_dir=tmp_path / "static", publish_dir=tmp_path / "publish", publish_timeout=10.0,
        workspaces_root=workspaces, github_interval=1e9, hoststate_path=tmp_path / "hoststate.json",
        catalog_local_dir=tmp_path / "catalog-local",
        origin_public_ip="203.0.113.10", origin_internal_ip="10.0.0.10", wildcard_zone="example.com",
        publish_zones=("example.com", "example.lan"), broker_env_dir=tmp_path / "broker-env",
        updates_dir=tmp_path / "updates", packages_dir=tmp_path / "packages", git_dir=tmp_path / "git",
        accounts_dir=tmp_path / "accounts", agent_home=tmp_path / "homes" / "agent",
        containers_dir=tmp_path / "containers",  # audit ingestion runs even when containers are off; never read host data
        containers_record=tmp_path / "host-qualified.json",  # not the host's record: no qualification unless a test seeds one
    )
    # Opt-in tests select the database before api/project (and their dependent fixtures) start.
    if "backend_url" in request.fixturenames:
        settings = replace(settings, database_url=request.getfixturevalue("backend_url"))
    for sub in ("requests", "results"):
        (tmp_path / "publish" / sub).mkdir(parents=True)
    yield {"broker": broker, "settings": settings, "workspaces": workspaces, "home": home, "run_dir": run_dir,
           "root_run_dir": tmp_path / "run-root"}
    # The outer owned_tmux fixture collects children before stopping the server.


@pytest.fixture
async def broker(env):
    task = asyncio.create_task(serve(env["broker"]))
    try:
        async def ready():
            if task.done():
                await task
                raise AssertionError("broker exited before opening its socket")
            return env["broker"].socket_path.exists()
        await wait_for(ready)
        yield env["broker"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
async def supervisor_threads(monkeypatch):
    tasks = []

    async def to_thread(function, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        tasks.append(task)
        cancelled = False
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not cancelled:
                tasks.remove(task)  # the caller consumed its result, including any stage-handled error

    monkeypatch.setattr(supervisor_module, "asyncio",
                        SimpleNamespace(**(vars(asyncio) | {"to_thread": to_thread})))
    yield tasks
    # A cancelled stage cannot consume worker errors through its normal exception handler.
    # Join every worker without promoting those background-stage results to teardown errors.
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
async def api(env, broker, supervisor_threads, request):
    app = create_app(env["settings"])
    if "backend_url" in request.fixturenames:
        assert app.state.db.kw["bind"].url == make_url(request.getfixturevalue("backend_url"))
    loop_task = asyncio.create_task(app.state.supervisor.run())
    token = env["settings"].api_token()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://acc",
                                 headers={"Authorization": f"Bearer {token}"}) as client:
        client.app = app
        client.supervisor_task = loop_task
        client.supervisor_threads = supervisor_threads
        try:
            yield client
        finally:
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
            tasks = list(app.state.supervisor.tools.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.state.supervisor.broker.close()
            app.state.db.kw["bind"].dispose()


@pytest.fixture
def tmux_has(env):
    def check(name: str) -> bool:
        return subprocess.run(["tmux", "-L", env["broker"].tmux_socket, "has-session", "-t", f"={name}"],
                              capture_output=True).returncode == 0
    return check


@pytest.fixture
async def project(api, env):
    response = await api.post("/api/projects", json={
        "slug": "demo", "name": "Demo", "workspace_path": str(env["workspaces"] / "demo")})
    assert response.status_code == 201, response.text
    project = response.json()
    response = await api.post(f"/api/projects/{project['id']}/trust")
    assert response.status_code == 200, response.text
    return response.json()


MEMBER_ACCOUNTS = ("alice-agent", "bob-agent", "dana-agent", "spare-agent")


@pytest.fixture
def member_accounts(api):
    """Agent accounts members can be given: the default one ("agent") is the owners'. Without a broker socket folder
    every account shares the test broker."""
    broker = api.app.state.supervisor.broker
    known = [broker.default_account, *MEMBER_ACCOUNTS]
    broker.accounts = lambda: known
    return MEMBER_ACCOUNTS


@pytest.fixture
def preview_socket():
    with preview_listener() as listener:
        yield listener
