"""The root helper's validation and templates (no Caddy, no systemd)."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "deploy/publish/acc-publish"


@pytest.fixture
def helper(tmp_path, monkeypatch):
    monkeypatch.setenv("ACC_PUBLISH_ROOT", str(tmp_path / "publish"))
    monkeypatch.setenv("ACC_CADDY_DIR", str(tmp_path / "acc.d"))
    monkeypatch.setenv("ACC_WILDCARD_ZONE", "example.com")
    monkeypatch.setenv("ACC_PUBLISH_ZONES", "example.com, example.lan")
    monkeypatch.setenv("ACC_ALLOWED_ORIGINS", "https://acc.example.com,http://127.0.0.1:8740")
    loader = importlib.machinery.SourceFileLoader("acc_publish", str(HELPER))
    spec = importlib.util.spec_from_loader("acc_publish", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    calls = []
    module.real_validate = module.validate
    module.validate = lambda: calls.append("validate")
    module.reload = lambda: calls.append("reload")
    (tmp_path / "publish/requests").mkdir(parents=True)
    (tmp_path / "acc.d").mkdir()
    module.calls = calls
    module.tmp = tmp_path
    return module


def submit(helper, **request):
    path = helper.ROOT / "requests" / f"{request['id']}.json"
    path.write_text(json.dumps(request))
    return helper.process(path)


@pytest.mark.parametrize("request_, message", [
    ({"action": "apply", "host": "preview-1-8000.example.com", "port": 8000, "access": "public"}, "not allowed"),
    ({"action": "apply", "host": "acc.example.com", "port": 8004, "access": "public"}, "not allowed"),
    ({"action": "apply", "host": "*.example.com", "port": 8004, "access": "public"}, "not allowed"),
    ({"action": "apply", "host": "Bad_Host.example.com", "port": 8004, "access": "public"}, "not allowed"),
    ({"action": "apply", "host": "x.example.com\nfile_server", "port": 8004, "access": "public"}, "not allowed"),
    ({"action": "apply", "host": "x.example.com", "port": 22, "access": "public"}, "port must be"),
    ({"action": "apply", "host": "x.example.com", "port": "8004", "access": "public"}, "port must be"),
    ({"action": "apply", "host": "x.example.com", "port": 8004, "access": "open"}, "access must be"),
    ({"action": "delete-everything", "host": "x.example.com"}, "unknown action"),
    ({"action": "apply", "host": "x.example.com\n", "port": 8004, "access": "public"}, "not allowed"),  # `$` before a newline
    ({"action": "apply", "host": "example.com", "port": 8004, "access": "public"}, "not allowed"),  # zone apex
    ({"action": "apply", "host": "www.example.lan", "port": 8004, "access": "public"}, "not allowed"),
    ({"action": "apply", "host": "x.attacker.example", "port": 8004, "access": "public"}, "not in a zone"),
])
def test_rejects_bad_requests(helper, request_, message):
    result = submit(helper, id="req-0001", **request_)
    assert not result["ok"] and message in result["detail"]
    assert helper.calls == []


def test_example_subdomain_becomes_wildcard_route(helper):
    result = submit(helper, id="req-0002", action="apply", host="demo.example.com", port=8004, access="internal", actor="casey")
    assert result["ok"], result
    content = (helper.tmp / "acc.d/routes-example.com/demo.example.com.caddy").read_text()
    assert content.startswith("# acc-publish managed: request req-0002, approved by casey")
    assert "@demo_example_com host demo.example.com" in content and "reverse_proxy localhost:8004" in content
    assert "remote_ip 10.0.0.0/8" in content and 'respond "Forbidden" 403' in content
    assert helper.calls == ["validate", "reload"]
    assert json.loads((helper.ROOT / "results/req-0002.json").read_text())["ok"]
    assert not (helper.ROOT / "requests/req-0002.json").exists()


def test_other_zone_and_deep_names_are_standalone_sites(helper):
    assert submit(helper, id="req-0003", action="apply", host="app.example.lan", port=8100, access="public")["ok"]
    site = (helper.tmp / "acc.d/app.example.lan.caddy").read_text()
    assert "app.example.lan {" in site and "remote_ip" not in site
    assert submit(helper, id="req-0004", action="apply", host="a.b.example.com", port=8100, access="public")["ok"]
    assert (helper.tmp / "acc.d/a.b.example.com.caddy").exists()  # the wildcard only covers one label


def test_never_touches_files_it_did_not_create(helper):
    manual = helper.tmp / "acc.d/roost.example.com.caddy"
    manual.write_text("roost.example.com {\n\treverse_proxy localhost:8003\n}\n")
    shadowed = submit(helper, id="req-0005", action="apply", host="roost.example.com", port=8003, access="public")
    assert not shadowed["ok"] and "not managed by acc-publish" in shadowed["detail"]
    assert manual.read_text().startswith("roost.example.com {")
    route = helper.tmp / "acc.d/routes-example.com/other.example.com.caddy"
    route.parent.mkdir()
    route.write_text("hand-written")
    assert not submit(helper, id="req-0006", action="apply", host="other.example.com", port=8003, access="public")["ok"]
    assert not submit(helper, id="req-0010", action="remove", host="other.example.com")["ok"]
    assert route.read_text() == "hand-written"


def test_failed_validation_restores_previous_state(helper):
    def boom():
        raise helper.Refused("caddy validate failed: test")
    helper.validate = boom
    result = submit(helper, id="req-0007", action="apply", host="new.example.com", port=8005, access="public")
    assert not result["ok"] and not (helper.tmp / "acc.d/routes-example.com/new.example.com.caddy").exists()


def test_remove_round_trip(helper):
    submit(helper, id="req-0008", action="apply", host="gone.example.com", port=8006, access="public")
    assert submit(helper, id="req-0009", action="remove", host="gone.example.com")["ok"]
    assert not (helper.tmp / "acc.d/routes-example.com/gone.example.com.caddy").exists()


def test_approver_label_cannot_inject_caddy_config(helper):
    actor = "x\n:9999 {\n\trespond {env.CF_API_TOKEN}\n}\n#"
    assert submit(helper, id="req-0011", action="apply", host="lab.example.com", port=8007, access="public", actor=actor)["ok"]
    content = (helper.tmp / "acc.d/routes-example.com/lab.example.com.caddy").read_text()
    assert content.splitlines()[0] == "# acc-publish managed: request req-0011, approved by x9999respondenv.CF_API_TOKEN"
    assert ":9999" not in content and "{env." not in content


def test_managed_certificate_environment_supersedes_legacy(helper, monkeypatch):
    parent = helper.tmp / 'managed'
    parent.mkdir(mode=0o700)
    managed = parent / 'current.env'
    managed.write_text('CF_API_TOKEN=managed-canary-123456789\n')
    managed.chmod(0o600)
    legacy = helper.tmp / 'cloudflare.env'
    legacy.write_text('CF_API_TOKEN=legacy-canary-987654321\n') # gitleaks:allow -- synthetic fixture
    monkeypatch.setattr(helper, 'MANAGED_CADDY_ENV', managed, raising=False)
    monkeypatch.setattr(helper, 'CADDY_ENV', legacy)
    env = helper.caddy_env()
    assert env['CF_API_TOKEN'] == 'managed-canary-123456789'
    managed.unlink()
    managed.symlink_to(legacy)
    with pytest.raises(helper.Refused):
        helper.caddy_env()


def test_managed_multicloud_environment_is_used_for_publishing_validation(helper, tmp_path, monkeypatch):
    from pathlib import Path

    from test_dns_providers_p2 import AZURE_FIELDS

    from acc.dns_helper import DNSHelper
    directory = tmp_path / 'managed'
    directory.mkdir(mode=0o700)
    credential = directory / 'current.env'
    credential.write_text(DNSHelper.environment({'provider': 'azure', 'fields': AZURE_FIELDS}))
    credential.chmod(0o600)
    monkeypatch.setattr(helper, 'MANAGED_CADDY_ENV', credential)
    parser = tmp_path / 'credentials.py'
    parser.write_text((Path(__file__).resolve().parents[1] / 'src/acc/dns_credentials.py').read_text())
    parser.chmod(0o600)
    monkeypatch.setattr(helper, 'CREDENTIAL_MODULE', parser)
    env = helper.caddy_env()
    assert 'dns azure {' in env['ACC_DNS_CONFIG']
    assert 'example-secret' in env['ACC_DNS_CONFIG']
    assert 'CF_API_TOKEN' not in env


def test_validation_sandbox_isolates_caddy_logs_and_installer_refreshes_unit():
    root = HELPER.parents[2]
    unit = (HELPER.parent / 'acc-publish.service').read_text()
    assert 'ProtectSystem=strict' in unit
    assert 'TemporaryFileSystem=/var/lib/caddy:rw,nosuid,nodev,noexec' in unit
    assert 'XDG_DATA_HOME=/var/lib/acc-publish' in unit
    assert not any('/var/lib/caddy' in line for line in unit.splitlines() if line.startswith('ReadWritePaths='))
    installer = (root / 'deploy/install.sh').read_text()
    install = installer.index('"$REPO/deploy/publish/acc-publish.service"')
    assert '/etc/systemd/system/' in installer[install:install+500]
    assert installer.index('systemctl daemon-reload', install) > install


def test_validate_failure_journals_bounded_redacted_reason_not_api(helper, monkeypatch, capsys):
    from types import SimpleNamespace
    secret = 'synthetic-secret-1234567890'
    nested = 'short-value'
    monkeypatch.setattr(helper, 'caddy_env', lambda: {
        'CF_API_TOKEN': secret, 'ACC_DNS_CONFIG': 'dns azure { client_secret "' + nested + '" }'})
    stderr = ('setting up custom log: opening log writer: permission denied '
              + secret + ' client_secret="' + nested + '" Bearer unknown-token-value '
              + 'api_key=other-secret-value ' + 'X'*10000 + '\nforged journal line\x1b[31m')
    monkeypatch.setattr(helper.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stderr=stderr))
    helper.validate = helper.real_validate
    result = submit(helper, id='req-0012', action='apply', host='new.example.com', port=8005, access='public')
    assert not result['ok']
    assert result['detail'] == 'caddy validate failed; check the private configuration and credential generation'
    diagnostic = capsys.readouterr().err
    assert 'opening log writer: permission denied' in diagnostic
    assert 'redacted' in diagnostic
    for value in (secret, nested, 'unknown-token-value', 'other-secret-value'):
        assert value not in diagnostic
    assert len(diagnostic) <= 2100
    assert len(diagnostic.splitlines()) == 1
    assert '\x1b' not in diagnostic
    assert not (helper.tmp / 'acc.d/routes-example.com/new.example.com.caddy').exists()


def test_validation_diagnostic_truncates_and_handles_timeout_bytes(helper, monkeypatch, capsys):
    assert len(helper.validation_diagnostic('short words ' * 1000, {})) == 2048
    monkeypatch.setattr(helper, 'caddy_env', lambda: {'CF_API_TOKEN': 'private-fixture-value'})
    def timeout(*args, **kwargs):
        raise helper.subprocess.TimeoutExpired('fixture-caddy', 60, stderr=b'opening log writer private-fixture-value')
    monkeypatch.setattr(helper.subprocess, 'run', timeout)
    with pytest.raises(helper.Refused, match='^caddy validate timed out$'):
        helper.real_validate()
    diagnostic = capsys.readouterr().err
    assert 'opening log writer' in diagnostic and 'private-fixture-value' not in diagnostic


def test_real_caddy_validate_provisions_log_writer_in_private_scratch(tmp_path):
    """Exercise installed Caddy, without systemd, listeners, TLS, or real Caddy storage."""
    import shutil
    import subprocess
    caddy = shutil.which('caddy')
    if not caddy:
        pytest.skip('Caddy unavailable; validation must be qualified on deployment VM')
    config = tmp_path / 'Caddyfile'
    log = tmp_path / 'private-logs/access.json'
    config.write_text('{\n admin off\n auto_https off\n}\nhttp://localhost:19876 {\n'
                      f' log {{\n output file {log}\n }}\n respond "test"\n}}\n')
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'XDG_DATA_HOME': str(tmp_path / 'data'),
           'XDG_CONFIG_HOME': str(tmp_path / 'config')}
    result = subprocess.run([caddy, 'validate', '--config', str(config), '--adapter', 'caddyfile'],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert log.is_file(), 'Validation must provision the logger even without starting Caddy'
