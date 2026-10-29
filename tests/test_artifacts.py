"""Session artifacts: files an agent leaves in .acc/artifacts for its reviewer (screenshots, videos, logs, reports).

Read like the wiki (test_confined_reads): through a descriptor of the workspace, no link followed at any level, no
blocking on a FIFO, regular files only, bounded by kind."""
from __future__ import annotations

import base64
import os

import pytest
from helpers import wait_for
from isolated_settings import isolated_broker
from test_ownership import two  # noqa: F401 (fixture)
from test_supervisor import state_is

from acc.broker import ops

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture
def artifacts(env):
    folder = env["workspaces"] / "demo" / ".acc" / "artifacts"
    folder.mkdir(parents=True)
    (folder / "home.png").write_bytes(PNG)
    (folder / "tests.log").write_text("12 passed\n")
    (folder / "e2e").mkdir()
    (folder / "e2e" / "run.webm").write_bytes(b"\x1aE\xdf\xa3")
    return folder


def names(env) -> list[str]:
    return [f["name"] for f in ops.list_artifacts(env["broker"], str(env["workspaces"] / "demo"))["files"]]


def test_lists_and_reads_files_and_one_level_of_folders(env, artifacts):
    path = str(env["workspaces"] / "demo")
    (artifacts / "e2e" / "deeper").mkdir()
    (artifacts / "e2e" / "deeper" / "x.png").write_bytes(PNG)  # two levels down: not listed
    (artifacts / ".hidden.png").write_bytes(PNG)
    listed = ops.list_artifacts(env["broker"], path)
    assert [(f["name"], f["kind"], f["size"]) for f in listed["files"]] == [
        ("e2e/run.webm", "video", 4), ("home.png", "image", len(PNG)), ("tests.log", "text", 10)]
    assert not listed["truncated"] and all(isinstance(f["mtime"], int) and not f["too_large"] for f in listed["files"])
    read = ops.read_artifact(env["broker"], path, "home.png")
    assert read["kind"] == "image" and read["size"] == len(PNG) and base64.b64decode(read["data"]) == PNG
    assert base64.b64decode(ops.read_artifact(env["broker"], path, "e2e/run.webm")["data"]) == b"\x1aE\xdf\xa3"
    for bad in ("../../../etc/passwd.txt", "e2e/deeper/x.png", ".hidden.png", "e2e/../home.png", ""):
        with pytest.raises(ops.OpError, match="no such artifact"):
            ops.read_artifact(env["broker"], path, bad)


def test_no_folder_is_an_empty_list(env):
    assert ops.list_artifacts(env["broker"], str(env["workspaces"] / "demo")) == {"files": [], "truncated": False}
    with pytest.raises(ops.OpError):
        ops.list_artifacts(env["broker"], "/etc")  # outside the workspace roots


def test_symlinked_folders_are_not_followed(env, artifacts, tmp_path):
    path = str(env["workspaces"] / "demo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    (artifacts / "linked").symlink_to(outside)
    assert "linked/secret.txt" not in names(env)
    with pytest.raises(ops.OpError, match="no such artifact"):
        ops.read_artifact(env["broker"], path, "linked/secret.txt")
    # the artifacts folder itself, or .acc, replaced by a link
    other = env["workspaces"] / "other"
    (other / ".acc" / "artifacts").mkdir(parents=True)
    (other / ".acc" / "artifacts" / "a.txt").write_text("a")
    (env["workspaces"] / "linked-acc").mkdir()
    (env["workspaces"] / "linked-acc" / ".acc").symlink_to(other / ".acc")
    assert ops.list_artifacts(env["broker"], str(env["workspaces"] / "linked-acc"))["files"] == []
    with pytest.raises(ops.OpError, match="no such artifact"):
        ops.read_artifact(env["broker"], str(env["workspaces"] / "linked-acc"), "a.txt")


def test_symlinked_files_fifos_and_other_kinds_are_skipped(env, artifacts, tmp_path):
    path = str(env["workspaces"] / "demo")
    (tmp_path / "passwd.txt").write_text("root:x:0:0")
    (artifacts / "passwd.txt").symlink_to(tmp_path / "passwd.txt")
    os.mkfifo(artifacts / "pipe.log")
    (artifacts / "logo.svg").write_text("<svg onload='alert(1)'/>")
    (artifacts / "tool.sh").write_text("#!/bin/sh")
    (artifacts / "noext").write_text("x")
    assert names(env) == ["e2e/run.webm", "home.png", "tests.log"]
    for name in ("passwd.txt", "pipe.log", "logo.svg", "tool.sh", "noext"):  # a FIFO would block a plain open
        with pytest.raises(ops.OpError, match="no such artifact"):
            ops.read_artifact(env["broker"], path, name)


def test_oversized_files_are_listed_but_refused(env, artifacts):
    path = str(env["workspaces"] / "demo")
    with open(artifacts / "big.log", "wb") as f:
        f.truncate(ops.ARTIFACT_LIMITS["text"] + 1)
    with open(artifacts / "fine.mp4", "wb") as f:
        f.truncate(ops.ARTIFACT_LIMITS["text"] + 1)  # the video limit is higher
    big = next(f for f in ops.list_artifacts(env["broker"], path)["files"] if f["name"] == "big.log")
    assert big["too_large"]
    with pytest.raises(ops.OpError, match="too large"):
        ops.read_artifact(env["broker"], path, "big.log")
    read = ops.read_artifact(env["broker"], path, "fine.mp4")
    assert read["size"] == ops.ARTIFACT_LIMITS["text"] + 1 and len(base64.b64decode(read["data"])) == read["size"]


def test_at_most_200_entries(env, artifacts):
    for i in range(ops.MAX_ARTIFACTS + 20):
        (artifacts / f"shot-{i:03}.png").write_bytes(PNG)
    listed = ops.list_artifacts(env["broker"], str(env["workspaces"] / "demo"))
    assert len(listed["files"]) == ops.MAX_ARTIFACTS and listed["truncated"]


def test_large_files_come_in_chunks(env, artifacts):
    path = str(env["workspaces"] / "demo")
    data = os.urandom(ops.ARTIFACT_CHUNK + 1000)
    (artifacts / "clip.mp4").write_bytes(data)
    first = ops.read_artifact(env["broker"], path, "clip.mp4")
    rest = ops.read_artifact(env["broker"], path, "clip.mp4", offset=ops.ARTIFACT_CHUNK)
    assert base64.b64decode(first["data"]) + base64.b64decode(rest["data"]) == data
    assert first["mtime_ns"] == rest["mtime_ns"] and first["size"] == len(data)


async def _session(api, project):
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "art"})).json()
    await wait_for(state_is(api, session["id"], "idle"))
    return session["id"]


async def test_api_lists_and_serves_with_strict_headers(api, env, project, artifacts):
    acc_id = await _session(api, project)
    (artifacts / "report.html").write_text("<script>alert(1)</script>")
    big = os.urandom(ops.ARTIFACT_CHUNK * 2 + 10)  # three broker replies
    (artifacts / "e2e" / "long.mp4").write_bytes(big)
    listed = (await api.get(f"/api/sessions/{acc_id}/artifacts")).json()
    assert [f["name"] for f in listed["files"]] == ["e2e/long.mp4", "e2e/run.webm", "home.png", "report.html", "tests.log"]

    png = await api.get(f"/api/sessions/{acc_id}/artifacts/home.png")
    assert png.status_code == 200 and png.content == PNG
    assert png.headers["content-type"] == "image/png"
    assert png.headers["x-content-type-options"] == "nosniff"
    assert png.headers["content-disposition"] == "inline; filename*=UTF-8''home.png"
    assert png.headers["content-security-policy"] == "default-src 'none'; img-src 'self'; media-src 'self'; sandbox"

    video = await api.get(f"/api/sessions/{acc_id}/artifacts/e2e/long.mp4")
    assert video.content == big and video.headers["content-type"] == "video/mp4"
    assert video.headers["content-disposition"].startswith("inline;")

    html = await api.get(f"/api/sessions/{acc_id}/artifacts/report.html")  # text, never a page
    assert html.headers["content-type"] == "text/plain; charset=utf-8"
    assert html.headers["content-disposition"] == "attachment; filename*=UTF-8''report.html"
    assert html.headers["x-content-type-options"] == "nosniff" and "sandbox" in html.headers["content-security-policy"]

    assert (await api.get(f"/api/sessions/{acc_id}/artifacts/tests.log")).text == "12 passed\n"
    (artifacts / "logo.svg").write_text("<svg/>")
    assert (await api.get(f"/api/sessions/{acc_id}/artifacts/logo.svg")).status_code == 404
    assert (await api.get(f"/api/sessions/{acc_id}/artifacts/missing.png")).status_code == 404
    with open(artifacts / "huge.png", "wb") as f:
        f.truncate(ops.ARTIFACT_LIMITS["image"] + 1)
    assert (await api.get(f"/api/sessions/{acc_id}/artifacts/huge.png")).status_code == 413


async def test_a_member_cannot_see_another_persons_artifacts(two, env, artifacts):  # noqa: F811
    acc_id = two["session"]["id"]
    assert (await two["owner"].get(f"/api/sessions/{acc_id}/artifacts")).json()["files"]
    for url in (f"/api/sessions/{acc_id}/artifacts", f"/api/sessions/{acc_id}/artifacts/home.png"):
        refused = await two["member"].get(url)
        assert refused.status_code == 404 and PNG not in refused.content


def test_launch_keeps_acc_folder_out_of_git(tmp_path):
    """Artifacts aren't changes: .acc/ goes into the repository's own info/exclude (never committed), once."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".acc" / "artifacts").mkdir(parents=True)
    (repo / ".acc" / "artifacts" / "shot.png").write_bytes(b"x")
    ops.exclude_artifacts(repo)
    ops.exclude_artifacts(repo)
    exclude = (repo / ".git" / "info" / "exclude").read_text()
    assert exclude.count(".acc/") == 1
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert ".acc" not in status
    ops.exclude_artifacts(tmp_path / "missing")  # not a repository (or not there): nothing happens


def _artifact_route(replies):
    """The real artifact route, with an authorized session on another machine whose broker sends `replies`."""
    import asyncio
    from contextlib import nullcontext
    from types import SimpleNamespace

    from acc.server.api.sessions import router
    from acc.server.tty import Writers

    class Broker:
        async def call(self, method, **kwargs):
            return replies(kwargs["offset"])
    session = SimpleNamespace(run_as="agent", project=SimpleNamespace(host="remote"), cwd="/workspace",
                              launch_spec=SimpleNamespace(files={}))
    ctx = SimpleNamespace(writers=Writers(), settings=None, db=lambda: nullcontext(None), supervisor=None, broker=Broker(),
                          actor=lambda: None, refused=lambda e: e, principal=lambda: None, owner=lambda: None,
                          session_for=lambda *a: session)
    endpoint = next(r.endpoint for r in router(ctx).routes if r.path.endswith("/artifacts/{name:path}"))
    return lambda name: asyncio.run(endpoint("session", name, who=object()))


def test_a_machine_cant_send_more_than_the_limits():
    """Release review #1: a compromised node's broker claims a size over the kind's limit, or keeps sending past what
    it claimed: refused at the primary, whatever it says."""
    from fastapi import HTTPException
    mib = base64.b64encode(b"x" * (1 << 20)).decode()
    oversized = _artifact_route(lambda offset: {"kind": "text", "size": 3 << 20, "mtime_ns": 1, "offset": offset, "data": mib})
    with pytest.raises(HTTPException) as refused:
        oversized("report.txt")
    assert refused.value.status_code == 502
    endless = _artifact_route(lambda offset: {"kind": "text", "size": 10, "mtime_ns": 1, "offset": offset, "data": mib})
    with pytest.raises(HTTPException):
        endless("report.txt")
    wrong_offset = _artifact_route(lambda offset: {"kind": "text", "size": 10, "mtime_ns": 1, "offset": 5, "data": ""})
    with pytest.raises(HTTPException):
        wrong_offset("report.txt")
    short = _artifact_route(lambda offset: {"kind": "text", "size": 10, "mtime_ns": 1, "offset": offset, "data": ""})
    with pytest.raises(HTTPException):
        short("report.txt")  # ends before its size
    ok = _artifact_route(lambda offset: {"kind": "text", "size": 5, "mtime_ns": 1, "offset": offset,
                                         "data": base64.b64encode(b"hello").decode()})
    assert ok("report.txt").body == b"hello"


def test_listing_looks_at_a_bounded_number_of_entries(tmp_path, monkeypatch):
    """Release review #3: a folder with very many entries (or subfolders) is read only up to a visit budget."""
    import stat as stat_module
    from contextlib import nullcontext
    from types import SimpleNamespace

    (tmp_path / ".acc/artifacts").mkdir(parents=True)
    consumed = []

    def entries():
        for i in range(100_000):
            consumed.append(i)
            yield SimpleNamespace(name=f"{i:06}.bin", is_dir=lambda **k: False,  # unsupported: never counted as files
                                  stat=lambda **k: SimpleNamespace(st_mode=stat_module.S_IFREG, st_size=0, st_mtime=0))
    monkeypatch.setattr(ops.os, "scandir", lambda fd: nullcontext(entries()))
    result = ops.list_artifacts(isolated_broker(tmp_path, allowed_roots=(tmp_path,)), str(tmp_path))
    assert result == {"files": [], "truncated": True}
    assert len(consumed) <= ops.MAX_ARTIFACT_VISITS + 1


def test_exclude_never_follows_a_link_or_blocks(tmp_path):
    """Release review #4: info/exclude that is a link (or a FIFO) is left alone; nothing outside is written."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    victim = tmp_path / "outside.txt"
    victim.write_text("original\n")
    exclude = repo / ".git" / "info" / "exclude"
    exclude.unlink()
    exclude.symlink_to(victim)
    ops.exclude_artifacts(repo)
    assert victim.read_text() == "original\n"
    exclude.unlink()
    os.mkfifo(exclude)
    ops.exclude_artifacts(repo)  # returns at once
    exclude.unlink()
    info = repo / ".git" / "info"
    info.rmdir()
    info.symlink_to(tmp_path)  # a linked folder on the way
    ops.exclude_artifacts(repo)
    assert not (tmp_path / "exclude").exists()
