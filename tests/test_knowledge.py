"""Knowledge index, doc reading and the audit view (M10)."""
from __future__ import annotations

import subprocess

import pytest

from acc.broker import ops


@pytest.fixture
def repo(env):
    workspace = env["workspaces"] / "demo"
    run = lambda *a: subprocess.run(["git", "-C", str(workspace), *a], check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (workspace / "docs/handoffs").mkdir(parents=True)
    (workspace / "docs/decisions").mkdir(parents=True)
    (workspace / "docs/handoffs/2026-10-18-abc123def456-api.md").write_text("# Handoff\\nDone.")
    (workspace / "docs/decisions/0001-sqlite.md").write_text("# ADR 1")
    (workspace / "secret.md").write_text("not under docs")
    run("add", "-A")
    run("commit", "-qm", "Add docs")
    return workspace


def test_scan_docs_lists_pointers_with_commits(env, repo):
    docs = ops.scan_docs(env["broker"], str(repo))
    handoff = docs["handoffs"][0]
    assert handoff["path"] == "docs/handoffs/2026-10-18-abc123def456-api.md" and handoff["subject"] == "Add docs"
    assert docs["decisions"][0]["commit"] and docs["council"] == []


def test_read_doc_is_limited_to_markdown_under_docs(env, repo):
    assert ops.read_doc(env["broker"], str(repo), "docs/decisions/0001-sqlite.md")["content"] == "# ADR 1"
    for bad in ("secret.md", "docs/../secret.md", "../../etc/passwd", "docs/decisions"):
        with pytest.raises(ops.OpError):
            ops.read_doc(env["broker"], str(repo), bad)


async def test_knowledge_and_audit_endpoints(api, project, repo):
    view = (await api.get(f"/api/projects/{project['id']}/knowledge")).json()
    assert view["docs"]["handoffs"][0]["path"].endswith("abc123def456-api.md")
    doc = (await api.get(f"/api/projects/{project['id']}/knowledge/doc", params={"path": "docs/handoffs/2026-10-18-abc123def456-api.md"})).json()
    assert doc["content"].startswith("# Handoff")
    refused = await api.get(f"/api/projects/{project['id']}/knowledge/doc", params={"path": "secret.md"})
    assert refused.status_code == 502
    audit = (await api.get("/api/audit", params={"action": "project."})).json()
    assert {"project.create", "project.trust"} <= {a["action"] for a in audit}


async def test_session_knowledge_reads_the_sessions_own_folder(api, project, env):
    """Handoffs are read from where the session works: its worktree, or the shared checkout (UX review SE-2)."""
    from pathlib import Path

    from helpers import wait_for
    from test_changes import repo as make_repo
    from test_supervisor import state_is

    make_repo(env["workspaces"] / "demo")
    tree = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "Tree",
                                                  "worktree": True})).json()
    shared = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "Shared"})).json()
    await wait_for(state_is(api, tree["id"], "idle"))
    note = f"docs/handoffs/2026-10-22-{tree['id']}-login.md"
    (Path(tree["worktree"]) / "docs/handoffs").mkdir(parents=True)
    (Path(tree["worktree"]) / note).write_text("# Handoff\nWritten in the worktree.")
    shared_note = f"docs/handoffs/2026-10-22-{shared['id']}-notes.md"
    (env["workspaces"] / "demo/docs/handoffs").mkdir(parents=True, exist_ok=True)
    (env["workspaces"] / "demo" / shared_note).write_text("# Handoff\nWritten in the shared checkout.")

    view = (await api.get(f"/api/sessions/{tree['id']}/knowledge")).json()
    assert note in [d["path"] for d in view["docs"]["handoffs"]] and view["folder"] == "worktree"
    doc = (await api.get(f"/api/sessions/{tree['id']}/knowledge/doc", params={"path": note})).json()
    assert "Written in the worktree" in doc["content"]
    view = (await api.get(f"/api/sessions/{shared['id']}/knowledge")).json()
    assert shared_note in [d["path"] for d in view["docs"]["handoffs"]] and view["folder"] == "project"

    # Once the worktree folder is cleaned up, its merged notes are still found in the project's checkout.
    import shutil
    shutil.rmtree(tree["worktree"])
    view = (await api.get(f"/api/sessions/{tree['id']}/knowledge")).json()
    assert view["folder"] == "project" and shared_note in [d["path"] for d in view["docs"]["handoffs"]]
    assert (await api.get(f"/api/sessions/{tree['id']}/knowledge/doc", params={"path": shared_note})).status_code == 200


async def test_team_helper_handoffs_come_from_its_worktree(api, project, env):
    from pathlib import Path

    from test_team import lead, request

    x = await lead(api, project, env)
    helper = (await request(api, env, x, 'start', name='A', agent='claude', task='write notes'))['result']
    note = f"docs/handoffs/2026-10-22-{helper['id']}-notes.md"
    (Path(helper["worktree"]) / "docs/handoffs").mkdir(parents=True)
    (Path(helper["worktree"]) / note).write_text("# Helper handoff")
    view = (await api.get(f"/api/sessions/{helper['id']}/knowledge")).json()
    assert view["folder"] == "worktree" and note in [d["path"] for d in view["docs"]["handoffs"]]
    lead_view = (await api.get(f"/api/sessions/{x['id']}/knowledge")).json()
    assert note not in [d["path"] for d in lead_view["docs"]["handoffs"]]  # each session reads its own folder
