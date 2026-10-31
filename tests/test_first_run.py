"""A first project without a server shell (UX review 2, beta item 1): an empty folder or a clone, made in the
caller's own workspace root through their broker and trusted in the same step."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_member_views import member  # noqa: F401  (fixture)
from test_users import csrf

from acc.broker import ops


async def test_empty_project_is_made_trusted_and_registered(api, env):
    made = await api.post("/api/projects/new", json={"name": "Hello app"})
    assert made.status_code == 201, made.text
    project = made.json()
    folder = env["workspaces"] / "hello-app"
    assert project["slug"] == "hello-app" and project["workspace_path"] == str(folder) and project["trusted"] is True
    assert (folder / ".git").is_dir() and (folder / "docs/handoffs").is_dir()
    assert not (folder / "docs/research").exists() and (folder / "README.md").read_text().startswith("# Hello app")
    claude = json.loads((env["home"] / ".claude.json").read_text())
    assert claude["projects"][str(folder)]["hasTrustDialogAccepted"] is True
    audit = (await api.get("/api/audit", params={"action": "project.create"})).json()
    assert audit[0]["target"] == "hello-app" and audit[0]["detail"]["trusted"] is True

    again = await api.post("/api/projects/new", json={"name": "Hello app"})
    assert again.status_code == 409 and "already exists" in again.json()["detail"]


async def test_trust_can_be_left_for_later_and_slugs_are_checked(api, env):
    made = (await api.post("/api/projects/new", json={"name": "Later", "trust": False})).json()
    assert made["trusted"] is False and (env["workspaces"] / "later").is_dir()
    for slug in ("local", "local-x", "../up"):
        refused = await api.post("/api/projects/new", json={"name": "X", "slug": slug})
        assert refused.status_code == 422, slug
    assert (await api.post("/api/projects/new", json={"name": "!!!"})).status_code == 422


@pytest.mark.parametrize("url", ["file:///etc", "/srv/acc/data", "ext::sh -c id", "https://user:pw@example.com/a/b.git",
                                 "https://example.com/a/b?x=1", "http://example.com/a/b.git", ""])
async def test_clone_refuses_urls_that_are_not_https_or_ssh(api, env, url):
    refused = await api.post("/api/projects/new", json={"name": "Clone", "source": "clone", "url": url})
    assert refused.status_code == 422, refused.text
    assert not (env["workspaces"] / "clone").exists()


async def test_clone_by_url_and_by_github_name(api, env, monkeypatch):
    cloned = []

    def fake_url(config, url, dest):  # stands in for `git clone` (no network in tests)
        Path(dest).mkdir(parents=True)
        cloned.append(("url", url, dest))
        return {"path": dest}

    def fake_repo(config, repo, dest):
        Path(dest).mkdir(parents=True)
        cloned.append(("gh", repo, dest))
        return {"path": dest}
    monkeypatch.setattr(ops, "clone_url", fake_url)
    monkeypatch.setattr(ops, "clone_repo", fake_repo)

    by_url = (await api.post("/api/projects/new", json={"name": "Shop", "source": "clone",
                                                         "url": "https://github.com/example-org/shop.git"})).json()
    assert by_url["repo"] == "example-org/shop" and by_url["remote_url"] == "https://github.com/example-org/shop.git"
    elsewhere = (await api.post("/api/projects/new", json={"name": "Wiki", "source": "clone",
                                                            "url": "git@git.example.com:team/wiki.git"})).json()
    assert elsewhere["repo"] is None
    by_name = (await api.post("/api/projects/new", json={"name": "Docs", "source": "clone", "url": "example-org/docs"})).json()
    assert by_name["repo"] == "example-org/docs"
    # a clone runs someone else's config and hooks: it starts untrusted unless the caller says otherwise (0008 follow-up)
    assert [p["trusted"] for p in (by_url, elsewhere, by_name)] == [False, False, False]
    trusted = (await api.post("/api/projects/new", json={"name": "Mine", "source": "clone", "trust": True,
                                                          "url": "https://git.example.com/team/mine.git"})).json()
    assert trusted["trusted"] is True
    assert cloned == [("url", "https://github.com/example-org/shop.git", str(env["workspaces"] / "shop")),
                      ("url", "git@git.example.com:team/wiki.git", str(env["workspaces"] / "wiki")),
                      ("gh", "example-org/docs", str(env["workspaces"] / "docs")),
                      ("url", "https://git.example.com/team/mine.git", str(env["workspaces"] / "mine"))]


async def test_members_get_their_own_folder_and_cannot_pick_a_path(api, env, member):  # noqa: F811
    made = await member.post("/api/projects/new", headers=csrf(member), json={
        "name": "Mine", "workspace_path": "/srv/elsewhere"})  # an extra field is ignored, never used as the path
    assert made.status_code == 201, made.text
    assert made.json()["workspace_path"] == str(env["workspaces"] / "mine") and made.json()["account"] == "dana-agent"
    state = (await member.get("/api/auth/state")).json()["instance"]
    assert state["workspaces_root"] == str(env["workspaces"]) and state["github"] is False


def test_clone_url_validation(env):
    root = env["workspaces"]
    for url in ("file:///etc", "ext::sh", "/srv/x", "https://u:p@example.com/a/b", "https://example.com/a/../b",
                "https://example.com/a b", "ssh://root@example.com/a", "--upload-pack=x"):
        with pytest.raises(ops.OpError, match="HTTPS or SSH"):
            ops.clone_url(env["broker"], url, str(root / "x"))
    with pytest.raises(ops.OpError, match="outside"):
        ops.clone_url(env["broker"], "https://example.com/a/b.git", "/tmp/elsewhere")
    (root / "taken").mkdir()
    with pytest.raises(ops.OpError, match="already exists"):
        ops.clone_url(env["broker"], "https://example.com/a/b.git", str(root / "taken"))


def test_failed_clone_leaves_no_folder(env):
    with pytest.raises(ops.OpError, match="clone failed"):  # nothing listens on port 9 of localhost
        ops.clone_url(env["broker"], "https://127.0.0.1:9/a/b.git", str(env["workspaces"] / "never"))
    assert not (env["workspaces"] / "never").exists()


@pytest.mark.parametrize(('identity', 'expected'), [
    ('', 'Rynhult <acc@example.com>'),
    ('\tname = Alex Example\n', 'Alex Example <acc@example.com>'),
    ('\temail = alex@example.com\n', 'Rynhult <alex@example.com>'),
    ('\tname = Alex Example\n\temail = alex@example.com\n', 'Alex Example <alex@example.com>'),
])
async def test_empty_project_has_initial_commit_and_immediately_supports_worktrees(api, env, identity, expected):
    import subprocess
    from configparser import ConfigParser

    config = '[commit]\n\tgpgSign = true\n[gpg]\n\tprogram = /no-such-signing-program\n'
    if identity:
        config += '[user]\n' + identity
    (env['home'] / '.gitconfig').write_text(config)
    response = await api.post('/api/projects/new', json={'name': 'First checkout'})
    assert response.status_code == 201, response.text
    project = response.json()
    folder = Path(project['workspace_path'])
    def git(*args):
        result = subprocess.run(['git', '-C', str(folder), *args], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()
    assert git('rev-list', '--count', 'HEAD') == '1'
    assert git('ls-tree', '-r', '--name-only', 'HEAD') == 'README.md'
    assert git('show', 'HEAD:README.md') == (folder / 'README.md').read_text().strip()
    assert git('show', '-s', '--format=%an <%ae>') == expected
    assert git('show', '-s', '--format=%cn <%ce>') == expected
    assert git('status', '--porcelain') == ''
    assert git('remote') == ''
    # Initial-commit fallbacks are command-local. The project's subsequent
    # identity setup fills only missing fields, without shadowing global ones.
    configured = ConfigParser()
    configured.read_string(config)
    configured = dict(configured['user']) if configured.has_section('user') else {}
    defaults = {'name': 'Rynhult', 'email': 'rynhult@noreply.rynhult.invalid'}
    for key, fallback in defaults.items():
        local = subprocess.run(['git', '-C', str(folder), 'config', '--local', '--get', 'user.' + key],
                               capture_output=True, text=True)
        if key in configured:
            assert local.returncode == 1
            assert git('config', '--get', 'user.' + key) == configured[key]
        else:
            assert local.returncode == 0 and local.stdout.strip() == fallback
    local_config = (folder / '.git/config').read_text()
    launched = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                   'name': 'First own checkout', 'worktree': True})
    assert launched.status_code == 201, launched.text
    assert launched.json()['worktree']
    assert (env['home'] / '.gitconfig').read_text() == config
    assert (folder / '.git/config').read_text() == local_config


async def test_imported_unborn_repository_launch_has_plain_error_without_class_prefix(api, env, project):
    import subprocess

    folder = env['workspaces'] / 'demo'
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(folder)], check=True)
    response = await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude',
                                                   'name': 'Unborn import', 'worktree': True})
    assert response.status_code == 409, response.text
    detail = response.json()['detail']
    assert "Make the first commit" in detail and "project page" in detail
    assert 'OpError:' not in detail
