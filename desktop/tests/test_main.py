"""Desktop-only boundaries; removing the outer guard must fail these tests."""
import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient

SOURCE = Path(__file__).parents[1] / 'main.py'


def load():
    assert SOURCE.exists(), 'desktop main entry point has not been implemented'
    spec = importlib.util.spec_from_file_location('desktop_main', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('path', ['/', '/api/health', '/api/events', '/assets/app.js'])
def test_capability_required_for_every_http_path(path):
    module = load()
    inner = FastAPI()

    @inner.get('/{path:path}')
    def ok(path):
        return {'ok': True}

    client = TestClient(module.CapabilityGuard(inner, 'a' * 64, 'http://127.0.0.1:12345'), base_url='http://127.0.0.1:12345')
    assert client.get(path).status_code == 401
    assert client.get(path, cookies={'rynhult_desktop': 'wrong'}).status_code == 401
    assert client.get(path, cookies={'rynhult_desktop': 'a' * 64}).status_code == 200
    assert client.get(path, cookies={'rynhult_desktop': 'a' * 64},
                      headers={'Origin': 'http://evil.example.com'}).status_code == 403
    assert client.get(path, cookies={'rynhult_desktop': 'a' * 64},
                      headers={'Host': 'evil.example.com'}).status_code == 403


def test_websocket_guard_precedes_accept():
    from starlette.websockets import WebSocketDisconnect
    module = load()
    inner = FastAPI()

    @inner.websocket('/ws')
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_text('accepted')
        await socket.close()

    client = TestClient(module.CapabilityGuard(inner, 'b' * 64, 'http://127.0.0.1:12345'), base_url='http://127.0.0.1:12345')
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('ws://127.0.0.1:12345/ws'):
            pass
    with client.websocket_connect('ws://127.0.0.1:12345/ws', headers={'Cookie': 'rynhult_desktop=' + 'b' * 64}) as connection:
        assert connection.receive_text() == 'accepted'


def test_settings_never_use_deployment_paths(tmp_path, monkeypatch):
    # Server organizations must remain opt-in outside the standalone profile.
    monkeypatch.setenv("ACC_ORGANIZATIONS", "1")
    monkeypatch.setenv("RYNHULT_ORGANIZATIONS", "1")
    module = load()
    settings = module.settings_for(tmp_path / 'profile', tmp_path / 'ui', 'http://127.0.0.1:12345')
    for name in settings.__dataclass_fields__:
        value = getattr(settings, name)
        if isinstance(value, Path) and name not in {'static_dir', 'catalog_dir', 'guide_dir', 'ruleset_path', 'update_source'}:
            assert value.is_relative_to(tmp_path / 'profile'), (name, value)
    assert settings.database_url == ''
    assert settings.organizations_enabled is False
    assert not settings.cookie_secure
    assert settings.allowed_origins == ('http://127.0.0.1:12345',)


@pytest.mark.parametrize("platform, label", [("darwin", "macOS"), ("linux", "Linux")])
def test_standalone_profile_identifies_its_native_platform(tmp_path, monkeypatch, platform, label):
    module = load()
    from types import SimpleNamespace
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(module, "subprocess", SimpleNamespace(run=lambda *a, **kw: SimpleNamespace(stdout="")))
    profile = tmp_path / "profile"
    profile.mkdir(mode=0o700)
    settings = module.settings_for(profile, tmp_path / "static", "http://127.0.0.1:12345")
    assert settings.host_label == f"This computer · {label} · Standalone"

def test_native_document_bootstrap_sets_normal_session_cookies():
    module = load()
    inner = FastAPI()

    @inner.get('/')
    def ok():
        return {'ok': True}

    guard = module.CapabilityGuard(inner, 'a' * 64, 'http://127.0.0.1:12345', login=('session-secret', 'csrf-secret'))
    client = TestClient(guard, base_url='http://127.0.0.1:12345')
    assert not client.get('/').cookies  # no capability, no owner credentials
    response = client.get('/', cookies={'rynhult_desktop': 'a' * 64})
    assert response.cookies['acc_session'] == 'session-secret'
    assert response.cookies['acc_csrf'] == 'csrf-secret'
    assert 'httponly' in response.headers.get_list('set-cookie')[0].lower()


@pytest.mark.asyncio
async def test_desktop_refuses_elevated_main_before_opening_listener(monkeypatch):
    module = load()
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    with pytest.raises(ValueError, match='never run elevated'):
        await module.serve({})


def test_profile_lease_releases_on_close(tmp_path):
    module = load()
    lease = module.profile_lock(tmp_path / 'main.lock')
    try:
        with pytest.raises(OSError):
            module.profile_lock(tmp_path / 'main.lock')
    finally:
        lease.close()
    module.profile_lock(tmp_path / 'main.lock').close()


def test_mac_profile_rejects_acl_grants_even_with_mode_0700(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module = load()
    monkeypatch.setattr(module,'sys',SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(module,'subprocess',SimpleNamespace(run=lambda *a,**kw:SimpleNamespace(
        stdout='drwx------ owner staff\n 0: user:other allow read,search\n')),raising=False)
    with pytest.raises(ValueError,match='ACL'):
        module.settings_for(tmp_path/'profile',tmp_path/'ui','http://127.0.0.1:12345')
    monkeypatch.setattr(module,'subprocess',SimpleNamespace(run=lambda *a,**kw:SimpleNamespace(
        stdout='drwx------ owner staff\n 0: group:everyone deny delete\n')))
    settings = module.settings_for(tmp_path/'private',tmp_path/'ui','http://127.0.0.1:12345')
    assert settings.data_dir == tmp_path/'private/data_dir'
