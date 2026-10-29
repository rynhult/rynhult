"""AppImage tool custody with fixture hashes/downloads; no network or installed tools."""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

DESKTOP = Path(__file__).resolve().parents[1]
NAMES = ('AppRun-x86_64', 'linuxdeploy-07333c6-x86_64.AppImage', 'linuxdeploy-plugin-appimage.AppImage')
RAW = ('f30140a43a0a59e46db21bdefdf749b9e9f2c6946e92afabbacf98b8ae73fb4f',
       '36a2d7e274d12e1050d0e9ecfe11d339ed54720b2bec464c286d53f8b07f5c62',
       '992d502a248e14ab185448ddf6f6e7d25558cb84d4623c354c3af350c25fccb3')


@pytest.fixture
def tool_cache(tmp_path):
    desktop = tmp_path / 'desktop'
    (desktop / 'appimage').mkdir(parents=True)
    tools = desktop / 'src-tauri/target/.tauri'
    tools.mkdir(parents=True)
    script = (DESKTOP / 'appimage/fetch-tools.sh').read_text()
    manifest = []
    for name, digest in zip(NAMES, RAW):
        raw = ('fixture-' + name + '-end').encode()
        script = script.replace(digest, hashlib.sha256(raw).hexdigest())
        normalized = raw[:8] + b'\0\0\0' + raw[11:] if name == NAMES[1] else raw
        manifest.append(f'{hashlib.sha256(normalized).hexdigest()}  {name}')
        (tools / name).write_bytes(raw)
    (desktop / 'appimage/tools.sha256').write_text('\n'.join(manifest) + '\n')
    path = desktop / 'appimage/fetch-tools.sh'
    path.write_text(script)
    commands = tmp_path / 'commands'
    commands.mkdir()
    curl = commands / 'curl'
    curl.write_text('#!/bin/sh\necho unexpected-download >&2\nexit 91\n')
    curl.chmod(0o755)
    return path, tools, {**os.environ, 'PATH': f'{commands}:{os.environ["PATH"]}'}


def test_cached_linuxdeploy_link_never_mutates_its_target(tool_cache, tmp_path):
    script, tools, env = tool_cache
    binary = tools / NAMES[1]
    target = tmp_path / 'unrelated-binary'
    binary.rename(target)
    original = target.read_bytes()
    target.chmod(0o600)
    binary.symlink_to(target)
    result = subprocess.run(['sh', str(script)], env=env, capture_output=True, text=True)
    assert result.returncode != 0, 'linked cache entry was accepted'
    assert target.read_bytes() == original
    assert target.stat().st_mode & 0o777 == 0o600


def test_valid_cached_tools_are_normalized_and_verified(tool_cache):
    script, tools, env = tool_cache
    result = subprocess.run(['sh', str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tools / NAMES[1]).read_bytes()[8:11] == b'\0\0\0'


def test_corrupt_cached_tool_is_not_chmodded_before_verification(tool_cache):
    script, tools, env = tool_cache
    binary = tools / NAMES[0]
    binary.write_text('corrupt')
    binary.chmod(0o600)
    result = subprocess.run(['sh', str(script)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert binary.read_text() == 'corrupt'
    assert binary.stat().st_mode & 0o777 == 0o600


def test_symlinked_cache_directory_is_refused(tool_cache, tmp_path):
    script, tools, env = tool_cache
    outside = tmp_path / 'outside'
    shutil.move(tools, outside)
    tools.symlink_to(outside, target_is_directory=True)
    original = (outside / NAMES[1]).read_bytes()
    result = subprocess.run(['sh', str(script)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert (outside / NAMES[1]).read_bytes() == original


def test_partial_download_link_is_refused_before_curl(tool_cache, tmp_path):
    script, tools, env = tool_cache
    (tools / NAMES[0]).unlink()
    target = tmp_path / 'unrelated'
    target.write_text('keep')
    (tools / (NAMES[0] + '.part')).symlink_to(target)
    result = subprocess.run(['sh', str(script)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert 'unexpected-download' not in result.stderr
    assert target.read_text() == 'keep'


def test_hardlinked_tool_is_not_normalized_in_place(tool_cache, tmp_path):
    script, tools, env = tool_cache
    target = tmp_path / 'original-tool'
    os.link(tools / NAMES[1], target)
    original = target.read_bytes()
    result = subprocess.run(['sh', str(script)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert target.read_bytes() == original
