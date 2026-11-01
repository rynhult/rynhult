"""Self-contained ACC stack for Playwright: temp data, own tmux socket, fake CLIs, real built UI.

    uv run python tests/e2e_server.py <state-dir> <port>
Writes <state-dir>/info.json with the setup-token path and run dir, then serves until killed.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

# Exercise the production Orchestrator default; explicit harness overrides still apply.
import httpx
import uvicorn
from demo_github import SHA, FakeGitHub
from demo_host import live as demo_live
from demo_machine import attach
from helpers import preview_listener
from owned_tmux import Owner, start_time

from acc.broker.config import BrokerConfig
from acc.broker.server import MAX_SOCKET_PATH, serve
from acc.env import environment
from acc.server import host
from acc.server.app import create_app
from acc.server.config import Settings
from acc.server.github import GitHub

REPO = Path(__file__).resolve().parents[1]


def demo_root() -> bytes:
    """A root like Caddy's local one, made for this run (Settings → Certificate, Add machine's fingerprint)."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "Caddy Local Authority - 2026 ECC Root")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=1), critical=True).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM)



def qualify_containers(state: Path) -> None:
    """Containers are a Preview that needs a qualified host. This server's host records a passing report with the real
    record and check, its own uid standing in for root and its state folder for / (tests/test_container_host_record.py),
    so spec 41 can switch a project on; the not-qualified state is driven in the page."""
    from acc import container_host_record as hr
    root = state / "qualified-host"
    (root / "etc/acc/containers").mkdir(parents=True)
    for part in (root, root / "etc", root / "etc/acc", root / "etc/acc/containers"):
        part.chmod(0o755)
    (root / "etc/machine-id").write_text("e2e0" * 8 + "\n")
    report = root / "report.json"
    report.write_text(json.dumps({"version": 1, "status": "scoped-phase1-pass", "go_phase_1": True}))
    args = dict(machine_id=root / "etc/machine-id", root_uid=os.getuid(), top=root)
    record = root / "etc/acc/containers/host-qualified.json"
    hr.record(report, path=record, **args)
    real = hr.check
    hr.check = lambda path=None: real(record, **args)

async def main(base: Path, port: int, socket_name: str) -> None:
    os.environ['ACC_TEST'] = '1'  # Browser opt-in tests must never reach a real collector.
    # This fixture owns a private server, not the launching agent's tmux session.
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    base.mkdir(parents=True, exist_ok=True)
    state = base / f"run-{uuid.uuid4().hex[:8]}"  # fresh every run; nothing is ever deleted here
    home = state / "home" / "agent"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)
    os.environ["FAKE_HOOK"] = str(REPO / "bin/acc-hook")
    bin_dir = state / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("fake-claude", "fake-codex", "fake-copilot", "fake-antigravity", "fake-cursor", "fake-opencode", "fake-gh"):
        link = bin_dir / name
        if not link.exists():
            link.symlink_to(REPO / "tests/fake_cli.py")
    workspaces = state / "workspaces"
    (workspaces / "demo").mkdir(parents=True, exist_ok=True)
    (workspaces / "mobile-demo").mkdir(parents=True, exist_ok=True)
    run_dir = state / "run"
    run_dir.mkdir(exist_ok=True)
    broker = BrokerConfig(socket_path=state / "broker.sock", run_dir=run_dir, tmux_socket=socket_name,
                          containers_attach_root=state / "containers-attach",
                          catalog_dir=REPO / "catalog",
                          containers_socket=state / "unavailable-containers.sock",  # never the installed root helper
                          allowed_roots=(workspaces,), allowed_uids=frozenset({os.getuid()}),
                          providers={"claude": str(bin_dir / "fake-claude"), "codex": str(bin_dir / "fake-codex"),
                   "copilot": str(bin_dir / "fake-copilot"),
                   "antigravity": str(bin_dir / "fake-antigravity"),
                   "cursor": str(bin_dir / "fake-cursor"), "opencode": str(bin_dir / "fake-opencode"),
                   "gh": str(bin_dir / "fake-gh")},
                          hook_path=str(REPO / "bin/acc-hook"),
                          catalog_local_dir=state / "catalog-local")
    secure = environment.get("ACC_E2E_HTTPS") == "1"
    scheme = "https" if secure else "http"
    origin = f"{scheme}://{environment.get('ACC_E2E_HOST', 'localhost')}:{port}"
    replies_root = state / 'request-replies'
    replies_root.mkdir(mode=0o711)
    settings = Settings(request_replies_root=replies_root, groups_remote_enabled=environment.get("ACC_GROUPS_REMOTE") == "1", host_label="acc-host", data_dir=state / "data", run_dir=run_dir, broker_socket=broker.socket_path,
                        root_run_dir=state / "run-root", broker_env_dir=state / "broker-env", agent_home=home,
                        accounts_dir=state / "accounts", publish_dir=state / "publish", containers_dir=state / "containers",
                        hook_path=str(REPO / "bin/acc-hook"), statusline_path=str(REPO / "bin/acc-statusline"),
                        # Fast loops for browser tests; ACC_E2E_PRODUCTION_INTERVALS=1 keeps the real ones (perf checks).
                        **({} if environment.get("ACC_E2E_PRODUCTION_INTERVALS") == "1" else
                           {"ingest_interval": 0.2, "reconcile_interval": 0.5}), retry_backoff=(0.0, 0.0, 0.0),
                        claude_verify_timeout=5.0, codex_settle_seconds=1.0,
                        # the second origin is the https address Add machine's commands name (nothing serves it)
                        allowed_origins=(origin, "https://acc.example.lan", f"http://acc.preview.localhost:{port}"), cookie_secure=secure,
                        preview_domain="preview.localhost",
                        node_ca_file=state / "data" / "node-ca.pem",
                        catalog_local_dir=state / "catalog-local", workspaces_root=workspaces,
                        hoststate_path=state / "hoststate.json",  # never the real host's snapshot
                        updates_dir=state / "updates", packages_dir=state / "packages",  # nor its acc-packages helper's folder
                        git_dir=state / "git", backup_url=origin,  # git backups push to this server itself
                        containers_enabled=True,  # UI qualification uses an unavailable test-only launcher socket
                        # Past the attach gate (tests/test_containers.py covers it), so spec 41 reaches the launcher; its
                        # socket doesn't exist, so no session ever runs in a container here.
                        containers_attach_qualified=True,
                        organizations_enabled=True,  # Settings → Company sign-in; single sign-on itself stays off
                        # Upgrade rehearsals only: a throwaway PostgreSQL database.
                        database_url=environment.get("ACC_E2E_DATABASE_URL", ""))
    for folder in ("grants", "audit"):
        (settings.containers_dir / folder).mkdir(parents=True, exist_ok=True)
    # A healthy snapshot of its own, refreshed below like acc-hoststate.timer.
    host.live = demo_live  # nor the real host's CPU, memory and load: e2e screenshots go into the wiki
    qualify_containers(state)
    settings.hoststate_path.write_text(json.dumps({
        "collected_at": time.time(), "hostname": "e2e", "kernel": "6.8.0", "uptime_seconds": 3600.0, "reboot_required": False,
        "disks": [], "smart": [], "gpu": [], "services": {"units": {"accd": "active"}, "failed": []}, "containers": [],
        "tailscale": {"state": "Running", "ips": ["100.64.0.1"], "online": True},
        "accounts": [{"account": "alex-agent", "role": "agents", "active": "active", "memory_bytes": 512e6, "memory_max": 16e9,
                      "cpu_percent": 12.5, "cpu_quota_percent": 400, "tasks": 40, "tasks_max": 2048,
                      "workspace_root": "/srv/acc/workspaces", "disk_bytes": 2.1e9}]}))
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if seed := environment.get("ACC_E2E_SEED_DATA"):  # upgrade rehearsals: a prepared synthetic data folder, copied
        shutil.copytree(seed, settings.data_dir, dirs_exist_ok=True)
    (settings.data_dir / "updates-config.json").write_text(json.dumps({"repository":""}))
    (settings.data_dir / "node-ca.pem").write_bytes(demo_root())  # as if Caddy signed ACC's certificate
    # No browser fixture may address the fixed production signer socket. Disabled
    # test-authority mode must refuse explicitly even if the host has a real helper.
    from acc.server import devices

    def uninstalled_signer(_body):
        raise OSError('test device signer is not installed')

    devices.sign = uninstalled_signer
    if environment.get("ACC_E2E_DEVICE_CA", "1") == "1":
        # Test-only authority: real crypto, temporary state, no root helper install
        # or production socket. The production socket client is tested separately.
        import importlib.util

        spec = importlib.util.spec_from_file_location('test_device_signer', REPO / 'deploy/device-ca/acc-device-ca.py')
        signer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(signer)
        authority = state / 'device-authority'
        signer.initialize(authority)
        devices.sign = lambda body: signer.issue(authority, body)
        (settings.data_dir / 'device-ca.pem').write_bytes((authority / 'ca.pem').read_bytes())
    # No default browser fixture may contact a production helper, even if one is
    # installed later. Only the explicit test-owned edge below can perform effects.
    from acc.server import remote_access
    async def uninstalled_edge(message):
        raise OSError('test helper is not installed')
    remote_access.request = uninstalled_edge
    if environment.get("ACC_E2E_REMOTE_EDGE", "1") == "1":
        # Real helper contract/atomic files, simulated Caddy effects. The separate
        # API/harness test validates/reloads/verifies an actual disposable Caddy.
        # This fixture cannot address the production helper or Caddy socket.
        import atexit
        import copy

        from acc.remote_access_helper import RemoteAccessHelper
        edge = state / 'edge-helper'
        edge.mkdir(mode=0o700)
        edge_fd = os.open(edge, os.O_RDONLY | os.O_DIRECTORY)
        atexit.register(os.close, edge_fd)
        base_config = {'admin': {'listen': 'unix//run/caddy/admin.sock'}, 'apps': {'http': {'servers': {'main': {
            'listen': [':443'], 'tls_connection_policies': [{'match': {'sni': ['acc.example.lan']}}],
            'routes': [{'match': [{'host': ['acc.example.lan']}], 'handle': [{'handler': 'static_response', 'status_code': 403}]}]
        }}}}}
        loaded = [copy.deepcopy(base_config)]
        def edge_runner(action, configuration):
            if action == 'reload':
                loaded[0] = copy.deepcopy(configuration)
            return loaded[0] == configuration if action == 'verify' else True
        edge_helper = RemoteAccessHelper(edge_fd,
            {'qualified': True, 'hostnames': ['remote.example.com', 'other.example.com'], 'server': 'main'},
            base_config, (settings.data_dir / 'device-ca.pem').read_text(), edge_runner, owner=os.getuid())
        async def edge_request(message):
            return edge_helper.handle(message)
        remote_access.request = edge_request
    broker_task = asyncio.create_task(serve(broker))
    while not broker.socket_path.exists():
        if broker_task.done():  # it failed to start: say why rather than wait for a socket that won't appear
            await broker_task
        await asyncio.sleep(0.05)
    from acc.server import auth
    pairing_throttles = []
    terminal_throttles = []
    original_throttle = auth.LoginThrottle

    class FixtureThrottle(original_throttle):
        def __init__(self, limit=5, window=600.0, account_limit=10):
            super().__init__(limit=limit, window=window, account_limit=account_limit)
            # Known by the module that makes them, not by budget: native enrollment's buckets
            # (api/mobile.py) share device pairing's 10 per 10 minutes.
            maker = sys._getframe(1).f_globals.get("__name__")
            if maker == "acc.server.api.devices" and (limit, window) == (10, 600):
                pairing_throttles.append(self)
            elif maker == "acc.server.api.context" and (limit, window) == (20, 60):
                terminal_throttles.append(self)

    # Keep the real limiter and its production budget; only capture the test-owned
    # instances so serial cases can restore them between independent tests.
    auth.LoginThrottle = FixtureThrottle
    try:
        app = create_app(settings)
    finally:
        auth.LoginThrottle = original_throttle
    if len(pairing_throttles) != 1 or len(terminal_throttles) != 1:
        raise RuntimeError("Expected one test-owned throttle each for device pairing and terminal snapshots")
    async def demo_tool(request):
        return {"ok": True, "version": "3.6.2" if request.operation == "install" else None,
                "detail": "Demo tool change (no software installed)"}
    app.state.supervisor.tools._carry = demo_tool
    # Test-owned app: only this suite starts and stops it, never a workspace service.
    async def demo_preview(reader, writer):
        try:
            await reader.readuntil(b'\r\n\r\n')
            body = b'<!doctype html><title>Demo app</title><h1>Demo preview</h1><p>Changes appear here.</p>'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
            await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
    with preview_listener(run_id=port) as listener:
        demo_port = listener.getsockname()[1]
        # Transfer a duplicate to asyncio; the server owns it until shutdown.
        demo_app = await asyncio.start_server(demo_preview, sock=listener.dup())
    # No browser fixture may reach a machine's installed privileged folder worker.
    from e2e_folder_service import install as isolate_folder_service
    isolate_folder_service()
    # Separate test-only route is inserted before the SPA catch-all.
    from e2e_team_deadlines import install as install_team_deadlines
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    install_team_deadlines(app)
    app.router.routes.insert(0, Route('/preview-test-port', lambda request: JSONResponse({'port': demo_port})))

    def reset_pairing_throttle(request):
        pairing_throttles[0].failures.clear()
        return JSONResponse({"ok": True})

    app.router.routes.insert(0, Route('/e2e-test/reset-pairing-throttle', reset_pairing_throttle, methods=["POST"]))

    def reset_terminal_throttle(request):
        terminal_throttles[0].failures.clear()
        return JSONResponse({"ok": True})

    app.router.routes.insert(0, Route('/e2e-test/reset-terminal-throttle', reset_terminal_throttle, methods=["POST"]))

    from test_issue_sources import gitlab_issue, linear_issue

    # Synthetic DNS at the resolver boundary; never query a real LAN during browser tests.
    from acc.server.api import issue_sources as source_api
    from acc.server.issue_sources import IssueSources
    real_resolve = source_api.resolve_addresses
    async def demo_resolve(base):
        if base == "https://forge.example.lan" or base.startswith("https://forge.example.lan/"):
            return ["10.0.0.8"]
        return await real_resolve(base)
    source_api.resolve_addresses = demo_resolve

    def issue_http(request):
        if request.url.host == "forge.example.lan":
            item = {"number": 7, "title": "Fix forge parser", "body": "Untrusted task body", "state": "open",
                    "labels": [{"name": "bug"}], "assignees": [{"login": "alex"}],
                    "html_url": "https://forge.example.lan/example/demo/issues/7"}
            if request.url.params.get("type") == "pulls" or request.url.path.endswith("/7"):
                item["pull_request"] = {"merged": False}
            return httpx.Response(200, json=item if request.url.path.endswith("/7") else [item])
        if request.url.host == "gitlab.example.com":
            item = gitlab_issue(title="Closed demo task" if request.url.params.get("state") == "closed" else "Fix demo parser")
            return httpx.Response(200, json=item if request.url.path.endswith("/7") else [item])
        if request.url.host == "api.linear.app":
            item = linear_issue(title="Fix demo queue", team={"id": "demo-team"})
            body = json.loads(request.content)
            data = {"issue": item} if "IssueDetail" in body["query"] else {
                "issues": {"nodes": [item], "pageInfo": {"hasNextPage": False, "endCursor": None}}}
            return httpx.Response(200, json={"data": data})
        return httpx.Response(404, json={})
    app.state.supervisor.issue_sources = IssueSources(transport=httpx.MockTransport(issue_http))
    # GitHub for the Pull request tab: a fake API behind the dashboard token (example-org/demo, branch feature/login,
    # a failing required check with a job log, one unresolved review thread on PR #1); /github-test/pass fixes the check.
    github = FakeGitHub()
    github.check("test", "failure", job=51)
    github.check("lint", "success", job=52)
    github.jobs[51] = {"id": 51, "steps": [{"name": "Run pytest -q", "conclusion": "failure"}]}
    github.logs[51] = ("2026-10-22T10:00:01.0000000Z collected 12 items\n"
                       "2026-10-22T10:00:02.0000000Z FAILED tests/test_login.py::test_wrong_password - assert 200 == 401\n"
                       "2026-10-22T10:00:02.0000000Z ##[error]Process completed with exit code 1.")
    github.thread(1, "src/login.py", 12, ("riley-example", "Compare passwords with hmac.compare_digest, not ==."))
    app.state.supervisor._github = GitHub("demo-token", transport=github.transport())

    def github_pass(request):
        github.check_runs[SHA] = []
        github.check("test", "success", job=53)
        github.check("lint", "success", job=52)
        return JSONResponse({"ok": True})
    app.router.routes.insert(0, Route('/github-test/pass', github_pass, methods=["POST"]))
    # A member's own agent account besides the owners' default one (members never share it); without a broker socket
    # folder it uses the same test broker.
    known = [settings.default_account, "alex-agent", "finn-agent", "board-1440-agent", "board-390-agent"]
    fixture_accounts = state / "fixture-accounts"
    fixture_accounts.mkdir()
    app.state.supervisor.broker.accounts = lambda: known + sorted(p.name for p in fixture_accounts.glob("*-agent"))
    (base / "info.json").write_text(json.dumps({"state": str(state), "preview_port": demo_port,
        "setup_token_path": str(settings.setup_token_path), "run_dir": str(run_dir),
        "workspace": str(workspaces / "demo"), "origin": origin}))
    tls = {}
    if secure:
        from e2e_tls import certificate_files
        tls = certificate_files(state)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", **tls))

    async def demo_machine() -> None:  # another machine, connected: Providers' machine picker, Host's glance
        while not server.started:
            await asyncio.sleep(0.05)
        attach(app, "lab-box", {"os": "linux", "arch": "x86_64", "hostname": "lab-box", "wsl": "0"},
               [settings.default_account], broker.socket_path, {"claude": "2.1.300", "codex": "0.170.0"})
        # a Mac (studio-mac): ACC installs only Codex and Copilot there; the rest show by-hand steps
        mac = attach(app, "studio-mac", {"os": "darwin", "arch": "arm64", "hostname": "studio-mac"},
                     [settings.default_account], broker.socket_path, {"codex": "0.163.0"}, kind="mac")
        mac.by_hand = {"cursor": "2026.10.17"}

    async def keep_snapshot_fresh() -> None:
        while True:
            await asyncio.sleep(60)
            snapshot = json.loads(settings.hoststate_path.read_text())
            snapshot["collected_at"] = time.time()
            pending = settings.hoststate_path.with_suffix(".tmp")
            pending.write_text(json.dumps(snapshot))
            pending.replace(settings.hoststate_path)

    machine_task = asyncio.create_task(demo_machine())
    snapshot_task = asyncio.create_task(keep_snapshot_fresh())
    try:
        await server.serve()
    finally:
        demo_app.close()
        await demo_app.wait_closed()
        machine_task.cancel()
        snapshot_task.cancel()
        broker_task.cancel()
        await asyncio.gather(machine_task, broker_task, return_exceptions=True)
        try:
            await snapshot_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    base = Path(sys.argv[1])
    longest = base / "run-00000000" / "broker.sock"  # main()'s broker socket, the longest path it binds
    if len(os.fsencode(longest)) > MAX_SOCKET_PATH:
        sys.exit(f"e2e_server: ACC_E2E_BASE {base} is too long: its broker socket would be {len(os.fsencode(longest))} "
                 f"bytes, and Unix socket paths can be at most {MAX_SOCKET_PATH} bytes. Use a shorter folder "
                 "(the default is /tmp/acc-e2e).")
    base.mkdir(parents=True, exist_ok=True)
    socket_name = f"acc-e2e-{uuid.uuid4().hex[:8]}"
    owner = Owner()
    owner.register(socket_name)
    (base / 'processes.json').write_text(json.dumps({'pid': os.getpid(), 'start_time': start_time(os.getpid()), 'socket': socket_name}))
    try:
        asyncio.run(main(base, int(sys.argv[2]), socket_name))
    finally:
        owner.close()
