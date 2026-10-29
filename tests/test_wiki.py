"""Wikis: Azure DevOps "publish code as wiki" layout in docs/wiki, read through the broker."""
from __future__ import annotations

import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def wiki(env):
    root = env["workspaces"] / "demo" / "docs" / "wiki"
    (root / "User-Guide").mkdir(parents=True)
    (root / "Internal" / "Architecture").mkdir(parents=True)  # a folder without a peer page still shows as a parent
    (root / ".attachments").mkdir()
    (root / "Home.md").write_text("# Welcome\n![shot](/.attachments/overview.png)")
    (root / "User-Guide.md").write_text("# User guide")
    (root / "User-Guide" / "Getting-Started.md").write_text("# Getting started")
    (root / "User-Guide" / "Sessions.md").write_text("# Sessions")
    (root / "User-Guide" / ".order").write_text("Sessions\nGetting-Started\nMissing-Page\n")
    (root / "Internal" / "Architecture" / "Overview.md").write_text("# Overview")
    (root / "Zeta.md").write_text("# Last")
    (root / ".order").write_text("Home\nUser-Guide\n")
    (root / ".attachments" / "overview.png").write_bytes(PNG)
    (root / ".attachments" / "notes.txt").write_text("not an image")
    return root


async def test_tree_follows_order_files_and_titles(api, project, wiki):
    tree = (await api.get(f"/api/wiki/project/{project['id']}")).json()
    assert tree["exists"] is True
    top = [(n["name"], n["title"], n["path"]) for n in tree["pages"]]
    # .order first (Home is the home page), then the rest alphabetically; a folder without a page has no path
    assert top == [("Home", "Home", "Home.md"), ("User-Guide", "User Guide", "User-Guide.md"),
                   ("Internal", "Internal", None), ("Zeta", "Zeta", "Zeta.md")]
    guide = tree["pages"][1]["children"]
    assert [n["name"] for n in guide] == ["Sessions", "Getting-Started"]  # unknown names in .order are ignored
    assert tree["pages"][2]["children"][0]["children"][0]["path"] == "Internal/Architecture/Overview.md"


async def test_pages_and_attachments(api, project, wiki):
    page = (await api.get(f"/api/wiki/project/{project['id']}/page", params={"path": "User-Guide/Sessions.md"})).json()
    assert page["content"] == "# Sessions"
    image = await api.get(f"/api/wiki/project/{project['id']}/attachment", params={"path": "/.attachments/overview.png"})
    assert image.status_code == 200 and image.content == PNG and image.headers["content-type"] == "image/png"
    assert "sandbox" in image.headers["content-security-policy"]
    for bad in ("../../../../etc/passwd", ".attachments/notes.txt", "Home.md"):
        refused = await api.get(f"/api/wiki/project/{project['id']}/attachment", params={"path": bad})
        assert refused.status_code == 502, bad
    traversal = await api.get(f"/api/wiki/project/{project['id']}/page", params={"path": "../../README.md"})
    assert traversal.status_code == 502


async def test_missing_wiki_and_unknown_scope(api, project, env):
    assert (await api.get(f"/api/wiki/project/{project['id']}")).json() == {"exists": False, "pages": []}
    assert (await api.get("/api/wiki/workspace/not-a-workspace-20260101")).status_code == 404
    assert (await api.get("/api/wiki/project/99999")).status_code == 404


async def test_workspace_wiki(api, env):
    session = (await api.post("/api/sessions", json={"provider": "claude", "name": "w", "profile": "researcher",
                                                     "topic": "Wiki test"})).json()
    folder = session["workdir"].rsplit("/", 1)[1]
    root = env["workspaces"] / "local" / folder / "docs" / "wiki"
    root.mkdir(parents=True)
    (root / "Findings.md").write_text("# Findings")
    tree = (await api.get(f"/api/wiki/workspace/{folder}")).json()
    assert [n["path"] for n in tree["pages"]] == ["Findings.md"]
