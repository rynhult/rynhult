"""A demo ACC with artificial data only: for the public wiki's screenshots, and for trying ACC safely.

    uv run python scripts/demo_instance.py <empty-dir> [port, default 18761]

Use a neutral directory (e.g. /srv/acc-demo): its path appears in screenshots.

Everything is fictional and self-contained: fake Claude/Codex CLIs (tests/fake_cli.py), a private tmux
socket, temp data under <state-dir>, the user "alex", host "acc-host", projects under example-org, sites under
example.com, documentation IP ranges. Nothing here reads real accounts: login health, plan usage, models and
the host snapshot are seeded, and their refreshers are turned off. Writes <state-dir>/harness.json
({origin, cookie, csrf, member_cookie, member_csrf}; "sam" is a member) for web/scripts/wiki-shots.mjs. Ctrl-C stops it; delete <state-dir> afterwards.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
import uvicorn
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import select

from acc.broker.config import BrokerConfig
from acc.broker.server import serve
from acc.server import auth, host
from acc.server.app import create_app
from acc.server.config import Settings
from acc.server.models import AttentionItem, AuditLog, Observation, PublishRequest, Setting, User

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from demo_host import live as demo_live  # noqa: E402  (fixed CPU, memory and load, never this machine's)
from demo_machine import attach  # noqa: E402  (the e2e server's demo machine)

REPO = Path(__file__).resolve().parents[1]
NOW = time.time()

PROJECTS = [  # slug, name, repo, notes
    ("demo-api", "Demo API", "example-org/demo-api", "Order and inventory API (FastAPI, Postgres)."),
    ("demo-web", "Demo Web", "example-org/demo-web", "Customer storefront (React)."),
    ("handbook", "Team Handbook", None, "Internal handbook site, local only."),
]

WIKI = {  # a small sample wiki for the demo project (Azure DevOps layout)
    ".order": "Home\nUser-Guide\nInternal\n",
    "Home.md": "# Demo API\n\nThe order and inventory API behind the Demo storefront.\n\n- [User guide](/User-Guide): calling the API\n"
               "- [Internal](/Internal): architecture and operations\n",
    "User-Guide.md": "# User guide\n\nStart with [Getting started](/User-Guide/Getting-Started).\n",
    "User-Guide/.order": "Getting-Started\nOrders\n",
    "User-Guide/Getting-Started.md": "# Getting started\n\n1. Request an API key from the team.\n2. Call `GET https://api.example.com/v1/health`.\n\n"
                                     "| Environment | Base URL |\n|---|---|\n| Staging | `https://staging-api.example.com` |\n| Production | `https://api.example.com` |\n",
    "User-Guide/Orders.md": "# Orders\n\n- [x] Create an order\n- [x] List orders\n- [ ] Refunds (next release)\n",
    "Internal.md": "# Internal\n\nSee [Architecture](/Internal/Architecture).\n",
    "Internal/Architecture.md": "# Architecture\n\n```\nclient -> API (FastAPI) -> Postgres\n               \\-> worker (queue)\n```\n",
}


SCREEN = """\x1b[2J\x1b[H\
\x1b[1m> Add a refunds endpoint: POST /orders/{id}/refunds, partial refunds allowed, never more than was paid.\x1b[0m

\x1b[32m●\x1b[0m I'll look at how orders and payments are modelled first.

\x1b[32m●\x1b[0m \x1b[1mRead\x1b[0m(app/orders/models.py)
  \x1b[2m└  Read 142 lines\x1b[0m
\x1b[32m●\x1b[0m \x1b[1mRead\x1b[0m(app/payments/service.py)
  \x1b[2m└  Read 88 lines\x1b[0m

\x1b[32m●\x1b[0m Orders keep \x1b[36mamount_paid\x1b[0m; there's no refund record yet. Plan:
  1. a \x1b[36mRefund\x1b[0m model and migration
  2. \x1b[36mPOST /orders/{id}/refunds\x1b[0m, rejecting totals above amount_paid
  3. tests for full, partial and over-refunds

\x1b[32m●\x1b[0m \x1b[1mWrite\x1b[0m(app/refunds/models.py)
  \x1b[2m└  Wrote 31 lines\x1b[0m
\x1b[32m●\x1b[0m \x1b[1mWrite\x1b[0m(migrations/0014_refunds.py)
  \x1b[2m└  Wrote 24 lines\x1b[0m

\x1b[33m╭──────────────────────────────────────────────────────────────────────────────╮\x1b[0m
\x1b[33m│\x1b[0m \x1b[1mBash command\x1b[0m                                                                 \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m                                                                              \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m   uv run pytest tests/test_refunds.py -q                                     \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m   Run the new refund tests                                                   \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m                                                                              \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m Do you want to proceed?                                                      \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m \x1b[36m❯ 1. Yes\x1b[0m                                                                     \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m   2. Yes, and don't ask again for uv run pytest commands                     \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m   3. No, and tell Claude what to do differently (esc)                        \x1b[33m│\x1b[0m
\x1b[33m╰──────────────────────────────────────────────────────────────────────────────╯\x1b[0m
"""

PHONE_SCREEN = """\x1b[2J\x1b[H\
\x1b[1m> Add a refunds endpoint\x1b[0m

\x1b[32m●\x1b[0m \x1b[1mWrite\x1b[0m(app/refunds/models.py)
  \x1b[2m└  Wrote 31 lines\x1b[0m
\x1b[32m●\x1b[0m \x1b[1mWrite\x1b[0m(migrations/0014.py)
  \x1b[2m└  Wrote 24 lines\x1b[0m

\x1b[33m╭──────────────────────────────────╮\x1b[0m
\x1b[33m│\x1b[0m \x1b[1mBash command\x1b[0m                     \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m   uv run pytest -q               \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m Do you want to proceed?          \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m \x1b[36m❯ 1. Yes\x1b[0m                         \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m   2. Yes, and don't ask again    \x1b[33m│\x1b[0m
\x1b[33m│\x1b[0m   3. No (esc)                    \x1b[33m│\x1b[0m
\x1b[33m╰──────────────────────────────────╯\x1b[0m
"""  # the fake CLI can't redraw for a narrow window the way a real one does; wiki-shots prints this for the phone shot


DEMO_KEYS = [  # name, variable, shared, other machines, note, given to
    ("OpenAI (team)", "OPENAI_API_KEY", True, False, "The team's account, billed to engineering", ["demo-api", "demo-web"]),
    ("Stripe (test mode)", "STRIPE_SECRET_KEY", True, False, "Test mode only: no real payments", ["demo-api"]),
    ("Sentry", "SENTRY_AUTH_TOKEN", False, True, None, ["demo-web"]),
    ("Maps API", "MAPS_API_KEY", True, True, "Restricted to *.example.com", []),
]


def seed_db(db, project_ids: dict[str, int]) -> None:
    now = auth.now()
    with db() as s:
        def observe(key, value, source):
            s.merge(Observation(key=key, value=value, source=source, observed_at=NOW))
        observe("claude.rate_limits", {"five_hour": {"used_percentage": 38, "resets_at": NOW + 7200},
                                       "seven_day": {"used_percentage": 22, "resets_at": NOW + 3 * 86400}}, "statusline")
        observe("codex.rate_limits", {"plan": "pro", "primary": {"used_percent": 12, "window_minutes": 10080,
                                                                  "resets_at": NOW + 5 * 86400}}, "app-server")
        observe("auth.health", {"claude": {"logged_in": True, "email": "alex@example.com", "subscription": "max",
                                           "rate_limit_tier": "max_5x", "login_expires_at": NOW + 24 * 86400},
                                "codex": {"logged_in": True, "email": "alex@example.com", "plan": "pro",
                                          "last_refresh": (now - timedelta(hours=3)).isoformat()},
                                "checked_at": NOW}, "cli")
        observe("models", {"claude": [{"id": "opus", "label": "Opus (latest)", "source": "cli alias", "description": ""},
                                      {"id": "sonnet", "label": "Sonnet (latest)", "source": "cli alias", "description": ""}],
                           "codex": [{"id": "gpt-example", "label": "GPT Example", "efforts": ["low", "medium", "high"],
                                      "source": "app-server", "description": ""}],
                           "fetched_at": NOW}, "broker")
        snapshot = {"fetched_at": NOW, "repo": {"full_name": "example-org/demo-api", "html_url": "https://github.com/example-org/demo-api",
                    "private": True, "default_branch": "main", "pushed_at": now.isoformat(), "description": "Demo API"},
                    "branches": [{"name": "main", "protected": True, "sha": "a1b2c3d"}, {"name": "feat/refunds", "protected": False, "sha": "d4e5f6a"}],
                    "commits": [{"sha": "a1b2c3d", "message": "Add order pagination", "author": "alex", "date": now.isoformat(), "url": "https://github.com/example-org/demo-api"},
                                {"sha": "9f8e7d6", "message": "Validate inventory counts", "author": "sam", "date": (now - timedelta(hours=5)).isoformat(), "url": "https://github.com/example-org/demo-api"}],
                    "pulls": [{"number": 42, "title": "Refunds endpoint", "author": "alex", "draft": False, "head": "feat/refunds", "updated_at": now.isoformat(), "url": "https://github.com/example-org/demo-api/pull/42"}],
                    "issues": [{"number": 17, "title": "Rate-limit the search endpoint", "author": "sam", "labels": ["enhancement"], "updated_at": now.isoformat(), "url": "https://github.com/example-org/demo-api/issues/17"}],
                    "runs": [{"id": 3, "name": "CI", "branch": "main", "event": "push", "status": "completed", "conclusion": "success", "created_at": now.isoformat(), "url": "https://github.com/example-org/demo-api/actions"},
                             {"id": 2, "name": "CI", "branch": "feat/refunds", "event": "pull_request", "status": "completed", "conclusion": "success", "created_at": (now - timedelta(hours=1)).isoformat(), "url": "https://github.com/example-org/demo-api/actions"}]}
        observe(f"github.{project_ids['demo-api']}", snapshot, "github")
        pending = uuid.uuid4().hex[:12]
        s.add(PublishRequest(id=pending, action="publish", host="preview.example.com", port=8010, access="internal",
                             project_id=project_ids["demo-web"], requested_by="session ui-polish", status="pending_approval",
                             reason="Let the team review the new checkout flow", zone_name="example.com"))
        s.add(PublishRequest(id=uuid.uuid4().hex[:12], action="publish", host="docs.example.com", port=8020, access="public",
                             project_id=project_ids["handbook"], requested_by="alex", status="applied", decided_by="alex",
                             zone_name="example.com", detail="HTTPS answered 200", decided_at=now - timedelta(days=2),
                             completed_at=now - timedelta(days=2)))
        s.add(AttentionItem(dedupe_key=f"publish_request:{pending}", kind="publish_request", severity="warn", source="agent",
                            verified=True, title="ui-polish asks to publish preview.example.com",
                            detail="Internal access to port 8010: let the team review the new checkout flow"))
        s.merge(Setting(key="guardrails", updated_by="alex", value={
            "rules": {"force_push": True, "push_default": True, "rm_outside": True, "secrets": True},
            "custom": [{"pattern": r"\bterraform\s+destroy\b", "reason": "no destroying infrastructure from an agent"}]}))
        s.merge(Setting(key="budget.alex", updated_by="alex", value={"rules": {  # alex chose these; nobody starts with any
            "claude.seven_day": {"mode": "pace", "limit": 100.0, "action": "notify"},
            "codex.primary": {"mode": "cap", "limit": 80.0, "action": "pause"}}}))
        for action, target in [("project.trust", "demo-api"), ("session.create", "api-refactor"), ("publish.approve", "docs.example.com"),
                               ("catalog.add_mcp", "shadcn"), ("session.stop", "docs-pass")]:
            s.add(AuditLog(actor="alex", action=action, target=target))
        s.commit()


def host_snapshot(path: Path) -> None:
    containers = [("demo-api", "api-1"), ("demo-api", "db-1"), ("demo-api", "worker-1"), ("demo-web", "web-1")]
    path.write_text(json.dumps({
        "collected_at": time.time(), "hostname": "acc-host", "kernel": "6.8.0-generic", "uptime_seconds": 9 * 86400 + 4 * 3600,
        "reboot_required": False,
        "disks": [{"mount": "/", "device": "/dev/nvme0n1p2", "total": 500e9, "used": 140e9, "free": 360e9, "percent": 28.0},
                  {"mount": "/srv", "device": "/dev/nvme1n1p1", "total": 2000e9, "used": 310e9, "free": 1690e9, "percent": 15.5}],
        "smart": [{"device": "nvme0n1", "model": "Example NVMe 500G", "size": "500G", "passed": True, "temperature": 38,
                   "power_on_hours": 4100, "percentage_used": 6, "media_errors": 0},
                  {"device": "nvme1n1", "model": "Example NVMe 2T", "size": "2T", "passed": True, "temperature": 35,
                   "power_on_hours": 2600, "percentage_used": 3, "media_errors": 0}],
        "gpu": [{"name": "Example GPU 8GB", "utilization": 4, "memory_used_mb": 512, "memory_total_mb": 8192, "temperature": 41, "power_w": 18}],
        "services": {"units": {"caddy": "active", "accd": "active", "acc-publish.path": "active", "ssh": "active",
                               "alex:acc-broker": "active", "alex:docker": "active"}, "failed": []},
        "containers": [{"name": f"{p}-{n}", "image": f"{p}:latest", "state": "running", "status": "Up 2 days", "project": p} for p, n in containers],
        "tailscale": {"state": "Running", "ips": ["192.0.2.10"], "online": True},
        "accounts": [{"account": a, "role": role, "active": "active", "cpu_percent": cpu, "cpu_quota_percent": None,
                      "memory_bytes": mem, "memory_max": None, "tasks": tasks, "tasks_max": None, "disk_bytes": disk,
                      "workspace_root": root}
                     for a, role, cpu, mem, tasks, disk, root in [
                         ("alex-agent", "agents", 14, 2.1e9, 38, 18.4e9, "/srv/acc/workspaces"),
                         ("sam-agent", "agents", 3, 0.6e9, 11, 2.2e9, "/srv/acc/workspaces-sam-agent"),
                         ("alex", "services", 6, 1.4e9, 64, None, None)]],
    }))


def demo_ca(state: Path) -> Path:
    """A throwaway private root CA where deploy/install.sh puts Caddy's (data/node-ca.pem), so Add machine shows the
    private-CA commands a LAN install uses and Settings → Certificate shows Caddy's own CA."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "Caddy Local Authority - 2026 ECC Root")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    path = state / "data" / "node-ca.pem"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


async def main(base: Path, port: int) -> None:
    state = base  # its path shows in screenshots (workspace paths), so pick a neutral one
    if state.exists() and any(state.iterdir()):
        raise SystemExit(f"{state} is not empty; use a new directory")
    home = state / "home" / "alex"
    home.mkdir(parents=True)
    os.environ["HOME"] = str(home)
    for key, value in {"GIT_AUTHOR_NAME": "Alex Example", "GIT_AUTHOR_EMAIL": "alex@example.com",
                       "GIT_COMMITTER_NAME": "Alex Example", "GIT_COMMITTER_EMAIL": "alex@example.com"}.items():
        os.environ[key] = value
    bin_dir = state / "bin"
    bin_dir.mkdir()
    for name in ("fake-claude", "fake-codex", "fake-copilot", "fake-antigravity", "fake-cursor", "fake-opencode", "fake-gh"):
        (bin_dir / name).symlink_to(REPO / "tests/fake_cli.py")
    for name in ("acc-hook", "acc-guard", "acc-statusline"):  # copies, so Launch spec shows the demo's path, not this checkout's
        shutil.copy2(REPO / "bin" / name, bin_dir / name)
    hook = str(bin_dir / "acc-hook")
    os.environ["FAKE_HOOK"] = hook
    workspaces, run_dir = state / "workspaces", state / "run"
    run_dir.mkdir()
    for slug, name, *_ in PROJECTS:
        (workspaces / slug).mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(workspaces / slug)], check=True)
        (workspaces / slug / "README.md").write_text(f"# {name}\n")
    for rel, text in WIKI.items():
        target = workspaces / "demo-api" / "docs" / "wiki" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for slug, *_ in PROJECTS:  # a first commit: Changes and worktrees start from it
        subprocess.run(["git", "-C", str(workspaces / slug), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(workspaces / slug), "commit", "-q", "-m", "Start"], check=True)
    host_snapshot(state / "hoststate.json")
    host.live = demo_live  # in place of psutil (the server reads host.live() on every refresh)
    guide = state / "guide"  # the Guide tab: ACC's own wiki, in a copy whose history is the demo user's
    shutil.copytree(REPO / "docs" / "wiki", guide / "docs" / "wiki")
    subprocess.run(["git", "init", "-q", "-b", "main", str(guide)], check=True)
    subprocess.run(["git", "-C", str(guide), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(guide), "commit", "-q", "-m", "Guide"], check=True)
    socket_name = f"acc-demo-{uuid.uuid4().hex[:6]}"
    broker = BrokerConfig(socket_path=state / "broker.sock", run_dir=run_dir, tmux_socket=socket_name,
                          containers_socket=state / "containers.sock",
                          containers_attach_root=state / "containers-attach",
                          hook_path=hook, catalog_dir=REPO / "catalog",
                          allowed_roots=(workspaces,), allowed_uids=frozenset({os.getuid()}),
                          providers={"claude": str(bin_dir / "fake-claude"), "codex": str(bin_dir / "fake-codex"),
                                     "copilot": str(bin_dir / "fake-copilot"),
                   "antigravity": str(bin_dir / "fake-antigravity"),
                   "cursor": str(bin_dir / "fake-cursor"), "opencode": str(bin_dir / "fake-opencode"),
                   "gh": str(bin_dir / "fake-gh")},
                          catalog_local_dir=state / "catalog-local", git_remote_base=str(state / "remotes"))
    origin = f"http://127.0.0.1:{port}"
    never = 1e12
    settings = Settings(data_dir=state / "data", run_dir=run_dir, broker_socket=broker.socket_path,
                        root_run_dir=state / "run-root", broker_env_dir=state / "broker-env",
                        accounts_dir=state / "accounts", updates_dir=state / "updates", publish_dir=state / "publish",
                        containers_dir=state / "containers",
                        hook_path=hook, statusline_path=str(bin_dir / "acc-statusline"),
                        ingest_interval=0.2, reconcile_interval=0.5, claude_verify_timeout=5.0, codex_settle_seconds=1.0,
                        changes_interval=2.0, history_interval=1.0,
                        allowed_origins=(origin, "https://acc.example.lan"), node_ca_file=demo_ca(state), cookie_secure=False, catalog_local_dir=state / "catalog-local",
                        workspaces_root=workspaces, hoststate_path=state / "hoststate.json",
                        host_label="acc-host", agent_home=home, wildcard_zone="example.com",
                        guide_dir=guide, github_interval=never, auth_interval=never, provider_interval=never, models_interval=never,
                        packages_dir=state / "packages",  # this demo's own "helper" folder, never the host's
                        git_dir=state / "git", backup_url=origin)  # git backups push to the demo itself
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "updates-config.json").write_text(json.dumps({"repository": ""}))
    (state / "packages" / "requests").mkdir(parents=True, exist_ok=True)
    (state / "packages" / "state.json").write_text(json.dumps({"daily_at": NOW - 5400, "packages": {
        "claude": {"label": "Claude Code", "method": "apt", "installed": "2.1.300", "latest": "2.1.300", "auto_update": True},
        "codex": {"label": "Codex", "method": "npm", "installed": "0.170.0", "latest": "0.171.0", "auto_update": True},
        "copilot": {"label": "GitHub Copilot CLI", "method": "npm", "installed": "1.0.103", "latest": "1.0.103", "auto_update": True}}}))
    broker_task = asyncio.create_task(serve(broker))
    while not broker.socket_path.exists():
        await asyncio.sleep(0.05)
    app = create_app(settings)
    db = app.state.db
    with db() as s:  # the demo user, already signed in
        user = User(username="alex", role="owner", password_hash=auth.hash_password("demo-password-not-real"),
                    totp_secret=auth.new_totp_secret(), totp_last_step=None)
        s.add(user)
        s.flush()
        cookie, csrf = auth.create_session(s, user, "demo")
        member = User(username="sam", role="member", password_hash=auth.hash_password("demo-password-not-real"),
                      totp_secret=auth.new_totp_secret(), totp_last_step=None)  # a member: sees only their own work
        s.add(member)
        s.flush()
        member_cookie, member_csrf = auth.create_session(s, member, "demo")
        s.commit()
    settings.setup_token_path.unlink(missing_ok=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())

    async def keep_snapshot_fresh() -> None:  # acc-hoststate.timer's job: otherwise "snapshot is stale" after 5 minutes
        while True:
            await asyncio.sleep(60)
            host_snapshot(state / "hoststate.json")
    fresh = asyncio.create_task(keep_snapshot_fresh())
    async with httpx.AsyncClient(base_url=origin, headers={"Authorization": f"Bearer {settings.api_token()}"}, timeout=30) as api:
        while True:
            try:
                if (await api.get("/api/health")).status_code == 200:
                    break
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
        ids = {}
        for slug, name, repo, notes in PROJECTS:
            project = (await api.post("/api/projects", json={"slug": slug, "name": name, "workspace_path": str(workspaces / slug),
                                                              "repo": repo, "notes": notes})).json()
            await api.post(f"/api/projects/{project['id']}/trust")
            ids[slug] = project["id"]

        # Keys in the vault (artificial values), added by Alex and given to projects before the sessions start
        async with httpx.AsyncClient(base_url=origin, cookies={"acc_session": cookie, "acc_csrf": csrf},
                                     headers={"x-acc-csrf": csrf, "origin": origin}, timeout=30) as alex:
            for name, variable, shared, remote, note, given in DEMO_KEYS:
                key = (await alex.post("/api/vault/keys", json={
                    "name": name, "variable": variable, "value": f"demo-not-a-real-key-{variable.lower()}-0000",
                    "shared": shared, "remote": remote, "note": note})).json()
                for slug in given:
                    (await alex.post(f"/api/vault/keys/{key['id']}/give", json={"project_id": ids[slug]})).raise_for_status()

        async def launch(**body):
            response = await api.post("/api/sessions", json={"remote_control": False, **body})
            response.raise_for_status()
            return response.json()
        api_refactor = await launch(project_id=ids["demo-api"], provider="claude", name="api-refactor", profile="fullstack")
        ui_polish = await launch(project_id=ids["demo-web"], provider="codex", name="ui-polish", profile="frontend")
        checkout = await launch(project_id=ids["demo-web"], provider="claude", name="checkout-flow", profile="frontend",
                                worktree=True)  # its own worktree and branch, next to ui-polish in the same project
        docs = await launch(project_id=ids["handbook"], provider="claude", name="docs-pass", profile="researcher")
        research = await launch(provider="claude", name="market-scan", profile="researcher", topic="Tournament scheduling apps")
        folder = research["workdir"].rsplit("/", 1)[1]
        await launch(provider="claude", name="go-no-go", profile="council", workspace=folder)
        report = workspaces / "local" / folder / "docs" / "research" / "2026-01-15-market-scan.md"
        report.write_text("# Tournament scheduling apps: market scan\n\nThree incumbents; none handle double elimination well.\n")
        (workspaces / "sam-notes").mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(workspaces / "sam-notes")], check=True)
        async with httpx.AsyncClient(base_url=origin, cookies={"acc_session": member_cookie, "acc_csrf": member_csrf},
                                     headers={"x-acc-csrf": member_csrf, "origin": origin}, timeout=30) as sam:
            notes = (await sam.post("/api/projects", json={"slug": "sam-notes", "name": "Sam's Notes", "workspace_path": str(workspaces / "sam-notes"),
                                                            "notes": "Sam's own project: only Sam and owners see it."})).json()
            (await sam.post(f"/api/projects/{notes['id']}/trust")).raise_for_status()
            (await sam.post("/api/sessions", json={"project_id": notes["id"], "provider": "claude", "name": "notes-cleanup",
                                                   "remote_control": False})).raise_for_status()
            (await sam.post(f"/api/projects/{notes['id']}/ports", json={"range": "dev"})).raise_for_status()
        with db() as s:  # Sam's own agent account (the demo has a single broker, so set it after Sam's session started)
            s.scalar(select(User).where(User.username == "sam")).account = "sam-agent"
            s.commit()
        for slug, body in (("demo-api", {"range": "dev"}), ("demo-api", {"range": "prod"}), ("demo-web", {"port": 8010}), ("handbook", {"port": 8020})):
            (await api.post(f"/api/projects/{ids[slug]}/ports", json=body)).raise_for_status()
        (await api.post("/api/ports", json={"port": 8150, "label": "Hosted game server"})).raise_for_status()
        for branch in ("feat/refunds", "fix/inventory-counts"):  # the Backup card: a few branches and a tag
            subprocess.run(["git", "-C", str(workspaces / "demo-api"), "branch", branch], check=True)
        subprocess.run(["git", "-C", str(workspaces / "demo-api"), "tag", "v1.4.0"], check=True)
        (await api.post(f"/api/projects/{ids['demo-api']}/backup/run")).raise_for_status()
        await asyncio.sleep(3)
        ctl = lambda session, command: (run_dir / session["id"] / "fake.ctl").write_text(command)
        # Conversations to search, what each is doing now, work to show under Changes, a guardrail at work
        for session, said in ((api_refactor, "Add a refunds endpoint with partial refunds and an idempotency key"),
                              (research, "Compare double elimination support in the three incumbents"),
                              (checkout, "Move the checkout flow to a single page and keep the cart on refresh")):
            ctl(session, f"say {said}")
        await asyncio.sleep(1.5)
        (workspaces / "demo-api" / "src").mkdir()
        (workspaces / "demo-api" / "src" / "refunds.py").write_text(
            'def refund(order, amount, key):\n    """Partial refunds; the idempotency key makes retries safe."""\n'
            '    if amount > order.total:\n        raise ValueError("more than was paid")\n    return order.refund(amount, key=key)\n')
        (workspaces / "demo-api" / "README.md").write_text("# Demo API\n\nOrders, inventory and refunds.\n")
        (Path(checkout["worktree"]) / "checkout.html").write_text("<main>One-page checkout</main>\n")
        ctl(checkout, "guarded git push --force origin main")
        await asyncio.sleep(1)
        ctl(ui_polish, "tool")  # "Running npm test"
        (run_dir / api_refactor["id"] / "fake.screen").write_text(SCREEN)
        (run_dir / api_refactor["id"] / "fake.screen.desktop").write_text(SCREEN)
        (run_dir / api_refactor["id"] / "fake.screen.phone").write_text(PHONE_SCREEN)
        (run_dir / api_refactor["id"] / "fake.ctl").write_text("screen")
        await asyncio.sleep(1)
        (run_dir / api_refactor["id"] / "fake.ctl").write_text("permission")  # a waiting permission prompt
        await api.post(f"/api/sessions/{docs['id']}/stop")
        # A second machine, connected: its link relays to this demo's broker and plays its acc-packages helper
        # (tests/demo_machine.py); Providers and Host show its CLIs and sign-ins.
        attach(app, "build-box", {"os": "linux", "arch": "x86_64", "hostname": "build-box"}, ["alex-agent"],
               broker.socket_path, {"claude": "2.1.300", "codex": "0.170.0", "ollama": "0.40.7"})
        await api.get("/api/accounts", params={"machine": "build-box", "refresh": 1})
        # A Mac still on an older ACC: the Machines card flags the version
        added = (await api.post("/api/nodes", json={"name": "alex-mac"})).json()
        public = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        (await api.post("/api/nodes/enroll", json={"name": "alex-mac", "code": added["code"], "public_key": public,
                                                   "info": {"os": "darwin", "arch": "arm64", "hostname": "alex-mac",
                                                            "version": "0.10.0"}})).raise_for_status()
        await api.post("/api/nodes", json={"name": "alex-pc"})  # added, waiting to be enrolled
        seed_db(db, ids)
    (state / "harness.json").write_text(json.dumps({"artificial": True, "source": "scripts/demo_instance.py",
                                                        "origin": origin, "cookie": cookie, "csrf": csrf,
                                                       "member_cookie": member_cookie, "member_csrf": member_csrf,
                                                       "run_dir": str(run_dir)}))
    print(f"demo ACC at {origin} (user alex; artificial data only)", flush=True)
    try:
        await serving
    finally:
        fresh.cancel()
        broker_task.cancel()
        subprocess.run(["tmux", "-L", socket_name, "kill-server"], capture_output=True)


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve(), int(sys.argv[2]) if len(sys.argv) > 2 else 18761))
