"""ACC is licensed under the MIT License; THIRD_PARTY_NOTICES.md lists what it builds on and must match the lock files."""
from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_mit_license_everywhere():
    assert (ROOT / "LICENSE").read_text().startswith("MIT License")
    assert tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["license"] == "MIT"
    assert '"license": "MIT"' in (ROOT / "web" / "package.json").read_text()
    assert "AGPL" not in (ROOT / "THIRD_PARTY_NOTICES.md").read_text()


def require_local_node_modules(root):
    modules = root / "web" / "node_modules"
    # Inspect the directory entry before any traversal. Never allow-list the
    # installed app just to reuse its dependencies in a root gate.
    if modules.is_symlink() or not modules.is_dir():
        pytest.fail("license integration requires worktree-local dependencies: run npm ci --prefix web")


def test_license_integration_refuses_shared_node_modules(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web/node_modules").symlink_to(tmp_path / "uninstalled", target_is_directory=True)
    with pytest.raises(pytest.fail.Exception, match="worktree-local"):
        require_local_node_modules(tmp_path)


def test_third_party_notices_are_current():
    require_local_node_modules(ROOT)
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "third_party_notices.py"), "--check"],
                            capture_output=True, text=True, env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert result.returncode == 0, result.stderr


async def test_signed_in_people_get_a_link_to_the_source(api, env, monkeypatch):
    """People using ACC over the network can get its source; a fork points ACC_SOURCE_URL at its own."""
    from test_auth import do_setup
    await do_setup(api, env)
    instance = (await api.get("/api/auth/state")).json()["instance"]
    assert instance["source_url"].startswith("https://")
    from acc.server.config import Settings
    monkeypatch.setenv("ACC_SOURCE_URL", "https://git.example.com/fork/acc")
    assert Settings.from_env().source_url == "https://git.example.com/fork/acc"


def test_notices_include_vendored_catalog_and_mcp_dependencies():
    import importlib.util
    spec = importlib.util.spec_from_file_location('notices', ROOT / 'scripts/third_party_notices.py')
    notices = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notices)
    rendered = "\n".join(notices.supplemental_notices())
    assert '## Vendored catalog' in rendered
    assert 'frontend-design' in rendered and 'catalog/skills/frontend-design/LICENSE.txt' in rendered
    assert '## MCP packages' in rendered
    assert '@playwright/mcp | 0.0.83 | Apache-2.0' in rendered


def notices_module():
    import runpy
    from types import SimpleNamespace
    return SimpleNamespace(**runpy.run_path(str(ROOT / 'scripts/third_party_notices.py')))


@pytest.mark.parametrize('installed', [None, '0.0.wrong'])
def test_python_notices_refuse_missing_or_mismatched_metadata(monkeypatch, installed):
    notices = notices_module()
    from importlib import metadata
    original = metadata.metadata
    expected = metadata.version("fastapi")

    def altered(name):
        if name == 'fastapi':
            if installed is None:
                raise metadata.PackageNotFoundError(name)
            result = original(name)
            result.replace_header('Version', installed)
            return result
        return original(name)

    monkeypatch.setattr(metadata, 'metadata', altered)
    with pytest.raises(ValueError, match='fastapi'):
        notices.python_license("fastapi", expected)


@pytest.mark.parametrize('problem', ['missing', 'version', 'license'])
def test_web_notices_refuse_incomplete_install(tmp_path, monkeypatch, problem):
    import json
    import runpy

    notices = runpy.run_path(str(ROOT / 'scripts/third_party_notices.py'))
    function = notices['npm_packages']
    monkeypatch.setitem(function.__globals__, 'ROOT', tmp_path)
    web = tmp_path / 'web'
    web.mkdir()
    (web / 'package-lock.json').write_text(json.dumps({'packages': {
        'node_modules/example': {'version': '1.2.3', 'license': 'MIT'}}}))
    if problem != 'missing':
        package = web / 'node_modules/example'
        package.mkdir(parents=True)
        (package / 'package.json').write_text(json.dumps({'version': 'wrong' if problem == 'version' else '1.2.3'}))
        if problem != 'license':
            (package / 'LICENSE').write_text('Example license')
    with pytest.raises(ValueError, match='example'):
        function()
