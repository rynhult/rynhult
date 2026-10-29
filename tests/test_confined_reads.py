"""Wiki and knowledge reads go through a descriptor of the workspace: no link followed at any level, no blocking on
a FIFO, regular files only, bounded reads (security review 2026-10-22, #3)."""
from __future__ import annotations

import os

import pytest

from acc.broker import ops

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def workspace(env):
    workspace = env["workspaces"] / "demo"
    wiki = workspace / "docs/wiki"
    (wiki / ".attachments").mkdir(parents=True)
    (wiki / "Guide").mkdir()
    (wiki / "Home.md").write_text("# Home")
    (wiki / "Guide/Start.md").write_text("# Start")
    (wiki / ".attachments/a.png").write_bytes(PNG)
    (workspace / "docs/decisions").mkdir()
    (workspace / "docs/decisions/0001.md").write_text("# ADR")
    return workspace


def test_normal_files_still_read(env, workspace):
    broker, path = env["broker"], str(workspace)
    assert ops.wiki_page(broker, path, "Guide/Start.md")["content"] == "# Start"
    assert ops.wiki_attachment(broker, path, "/.attachments/a.png")["content_type"] == "image/png"
    assert ops.read_doc(broker, path, "docs/decisions/0001.md")["content"] == "# ADR"
    assert [e["path"] for e in ops.scan_docs(broker, path)["decisions"]] == ["docs/decisions/0001.md"]
    tree = ops.wiki_tree(broker, path)["pages"]
    assert [(n["name"], n["path"]) for n in tree] == [("Guide", None), ("Home", "Home.md")]
    assert tree[0]["children"][0]["path"] == "Guide/Start.md"


def test_symlinked_folder_inside_the_workspace_is_not_followed(env, workspace):
    """A link that stays inside the workspace is refused too: it could be swapped for one that leads out."""
    broker, path = env["broker"], str(workspace)
    real = workspace / "elsewhere"
    (real / ".attachments").mkdir(parents=True)
    (real / "Page.md").write_text("# via link")
    (real / ".attachments/b.png").write_bytes(PNG)
    (real / "decisions").mkdir()
    (real / "decisions/0002.md").write_text("# via link")
    (workspace / "docs/wiki/Linked").symlink_to(real)
    (workspace / "docs/wiki/.attachments/more").symlink_to(real / ".attachments")
    (workspace / "docs/research").symlink_to(real / "decisions")
    with pytest.raises(ops.OpError, match="no such wiki page"):
        ops.wiki_page(broker, path, "Linked/Page.md")
    with pytest.raises(ops.OpError, match="only images"):
        ops.wiki_attachment(broker, path, ".attachments/more/b.png")
    with pytest.raises(ops.OpError, match="only Markdown files under docs/"):
        ops.read_doc(broker, path, "docs/research/0002.md")
    assert ops.scan_docs(broker, path)["research"] == []
    assert "Linked" not in [n["name"] for n in ops.wiki_tree(broker, path)["pages"]]

    # the whole docs folder replaced by a link to a copy inside the workspace
    os.rename(workspace / "docs", workspace / "docs-real")
    (workspace / "docs").symlink_to(workspace / "docs-real")
    assert ops.wiki_tree(broker, path) == {"exists": False, "pages": []}
    with pytest.raises(ops.OpError):
        ops.wiki_page(broker, path, "Home.md")
    with pytest.raises(ops.OpError):
        ops.read_doc(broker, path, "docs/decisions/0001.md")
    assert all(entries == [] for entries in ops.scan_docs(broker, path).values())


def test_symlinked_leaf_is_not_followed(env, workspace):
    broker, path = env["broker"], str(workspace)
    (workspace / "docs/wiki/Alias.md").symlink_to(workspace / "docs/wiki/Home.md")
    (workspace / "docs/decisions/0003.md").symlink_to(workspace / "docs/decisions/0001.md")
    with pytest.raises(ops.OpError, match="no such wiki page"):
        ops.wiki_page(broker, path, "Alias.md")
    with pytest.raises(ops.OpError):
        ops.read_doc(broker, path, "docs/decisions/0003.md")
    assert [e["path"] for e in ops.scan_docs(broker, path)["decisions"]] == ["docs/decisions/0001.md"]


def test_fifo_does_not_block(env, workspace):
    """A FIFO with no writer would block a plain open forever; it is refused at once instead."""
    broker, path = env["broker"], str(workspace)
    os.mkfifo(workspace / "docs/wiki/Pipe.md")
    os.mkfifo(workspace / "docs/wiki/.attachments/pipe.png")
    os.mkfifo(workspace / "docs/decisions/pipe.md")
    os.mkfifo(workspace / "docs/wiki/.order")
    with pytest.raises(ops.OpError, match="no such wiki page"):
        ops.wiki_page(broker, path, "Pipe.md")
    with pytest.raises(ops.OpError, match="only images"):
        ops.wiki_attachment(broker, path, ".attachments/pipe.png")
    with pytest.raises(ops.OpError, match="only Markdown files under docs/"):
        ops.read_doc(broker, path, "docs/decisions/pipe.md")
    assert [e["path"] for e in ops.scan_docs(broker, path)["decisions"]] == ["docs/decisions/0001.md"]
    assert "Home" in [n["name"] for n in ops.wiki_tree(broker, path)["pages"]]  # the FIFO .order is ignored


def test_files_over_the_limit_are_refused(env, workspace):
    broker, path = env["broker"], str(workspace)
    (workspace / "docs/wiki/Big.md").write_bytes(b"x" * (ops.MAX_DOC_BYTES + 1))
    (workspace / "docs/wiki/Exact.md").write_bytes(b"x" * ops.MAX_DOC_BYTES)
    (workspace / "docs/decisions/big.md").write_bytes(b"x" * (ops.MAX_DOC_BYTES + 1))
    (workspace / "docs/wiki/.attachments/big.png").write_bytes(b"x" * (ops.MAX_ATTACHMENT_BYTES + 1))
    with pytest.raises(ops.OpError, match="page too large to show"):
        ops.wiki_page(broker, path, "Big.md")
    assert len(ops.wiki_page(broker, path, "Exact.md")["content"]) == ops.MAX_DOC_BYTES
    with pytest.raises(ops.OpError, match="file too large to show"):
        ops.read_doc(broker, path, "docs/decisions/big.md")
    with pytest.raises(ops.OpError, match="attachment too large"):
        ops.wiki_attachment(broker, path, ".attachments/big.png")


def test_oversized_order_file_is_ignored(env, workspace):
    (workspace / "docs/wiki/.order").write_text("Home\n" + "x" * ops.WIKI_ORDER_BYTES)
    names = [n["name"] for n in ops.wiki_tree(env["broker"], str(workspace))["pages"]]
    assert names == ["Guide", "Home"]  # alphabetical: the order file was too big to trust


def test_dotdot_components_are_refused(env, workspace):
    broker, path = env["broker"], str(workspace)
    with pytest.raises(ops.OpError):
        ops.wiki_page(broker, path, "Guide/../Home.md")
    with pytest.raises(ops.OpError):
        ops.read_doc(broker, path, "docs/../docs/decisions/0001.md")
    with pytest.raises(ops.OpError):
        ops.wiki_attachment(broker, path, ".attachments/../.attachments/a.png")
