"""MCP environment values may be secrets: never in the API, the stored launch spec or any command line."""
from __future__ import annotations

import json
import stat
from pathlib import Path

from helpers import wait_for

from acc.broker import ops

SECRET = "sk-test-0123456789"  # gitleaks:allow -- synthetic redaction canary, never a credential


async def add_secret_mcp(api):
    added = await api.post("/api/catalog/mcps", json={"id": "vault", "command": "true", "args": ["--serve"],
                                                       "env": {"VAULT_TOKEN": SECRET}, "profiles": []})
    assert added.status_code in (200, 201), added.text


async def test_preview_and_stored_spec_never_hold_the_value(api, project):
    await add_secret_mcp(api)
    for provider in ("claude", "codex"):
        body = {"project_id": project["id"], "provider": provider, "name": f"s-{provider}", "mcps": ["vault"], "skills": [], "agents": []}
        preview = await api.post("/api/sessions/preview", json=body)
        assert preview.status_code == 200, preview.text
        assert SECRET not in preview.text
        created = (await api.post("/api/sessions", json=body)).json()
        detail = await api.get(f"/api/sessions/{created['id']}")
        assert SECRET not in detail.text
        events = await api.get(f"/api/sessions/{created['id']}/events")
        assert SECRET not in events.text  # the launch event records the command line
    codex = json.loads(preview.json()["files"]["codex-overrides.json"])
    assert 'mcp_servers.vault.env_vars=["VAULT_TOKEN"]' in codex


async def test_claude_run_folder_gets_the_real_value(api, project, env):
    await add_secret_mcp(api)
    created = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "real",
                                                    "mcps": ["vault"], "skills": [], "agents": []})).json()
    written = env["run_dir"] / created["id"] / "mcp.json"
    assert await wait_for(lambda: _exists(written))
    assert json.loads(written.read_text())["mcpServers"]["vault"]["env"] == {"VAULT_TOKEN": SECRET}


async def _exists(path):
    return path.exists()


def test_broker_passes_env_through_a_private_file(env):
    params = {"acc_id": "abcdef654321", "tmux_name": "acc-s-abcdef65", "provider": "codex", "args": ["--sandbox", "read-only"],
              "cwd": str(env["workspaces"] / "demo"), "files": {}, "env": {"VAULT_TOKEN": SECRET + "'; touch /tmp/x; '"}}
    (env["workspaces"] / "demo").mkdir(exist_ok=True)
    result = ops.launch(env["broker"], params)
    assert SECRET not in result["command"]
    env_file = env["run_dir"] / "abcdef654321" / "session.env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "export VAULT_TOKEN=" in env_file.read_text()
    import time
    for _ in range(50):  # the CLI itself (after sh's exec) has the value, with the quote characters intact
        environ = Path(f"/proc/{result['pid']}/environ").read_bytes() if result.get("pid") else b""
        if b"VAULT_TOKEN=" in environ:
            break
        time.sleep(0.1)
    assert ("VAULT_TOKEN=" + SECRET + "'; touch /tmp/x; '").encode() in environ.split(b"\0")
    ops.kill(env["broker"], "acc-s-abcdef65")
