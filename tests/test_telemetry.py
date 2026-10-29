"""Privacy boundary tests use an injected transport; never the public collector."""

import json
from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from acc.server.telemetry import Scrubber, collector_url
from acc.telemetry_schema import Usage, encode


def usage():
    return dict(
        schema="usage/1",
        install="a" * 22,
        day=date(2026, 10, 22),
        acc="dev",
        os=["linux"],
        install_kind="git-checkout",
        machines="1",
        people="1",
        projects="0",
        sessions_running="0",
        vault_keys="0",
        providers_enabled=["codex"],
        features_enabled=[],
        used={"session.launch": "1"},
        by_provider={"codex": "1"},
    )


def test_usage_schema_rejects_unknown_fields_and_values():
    assert json.loads(encode(Usage(**usage())))["used"] == {"session.launch": "1"}
    for patch in (
        {"prompt": "private"},
        {"providers_enabled": ["private-model"]},
        {"used": {"secret": "1"}},
        {"people": "123"},
        {"os": ["host.example.com"]},
    ):
        with pytest.raises(ValidationError):
            Usage(**(usage() | patch))


@pytest.mark.parametrize("version", ["dev", "0.15.0", "0.15.0-rc.1", "1.2.0-beta.2", "2.0.0-alpha.10"])
def test_usage_schema_accepts_release_and_candidate_versions(version):
    assert Usage(**(usage() | {"acc": version})).acc == version


@pytest.mark.parametrize("version", ["0.15.0-rc", "0.15.0rc1", "0.15.0-private.1", "0.15.0-rc.1.alex", "0.15.0+abc", "1.0"])
def test_usage_schema_rejects_other_version_text(version):
    with pytest.raises(ValidationError):
        Usage(**(usage() | {"acc": version}))


@pytest.mark.parametrize("version, built, expected", [
    ("0.15.0-rc.1", True, ("0.15.0-rc.1", "release")),
    ("0.15.0", True, ("0.15.0", "release")),
    ("0.15.0-rc.1", False, ("dev", "git-checkout")),
    ("0.15.0-local.1", True, ("dev", "git-checkout")),
])
def test_release_candidate_installs_report_their_version(tmp_path, monkeypatch, version, built, expected):
    from types import SimpleNamespace

    import acc
    from acc.server.telemetry import Telemetry

    if built:
        (tmp_path / ".acc-build").write_text("b" * 64)
    monkeypatch.setattr(acc, "__version__", version)
    assert Telemetry.identity(SimpleNamespace(settings=SimpleNamespace(update_source=tmp_path))) == expected


@pytest.mark.parametrize(
    "text",
    [
        "password=canarypass token=canarytoken Bearer canarybearer12345",
        "ghp_" + "x" * 36,
        "sk-" + "x" * 40,
        "/home/alex/private /srv/workspaces/private C:\\Users\\alex\\private ~alex/private",
        "alex@example.com host.example.lan 192.0.2.123 2001:db8::123",
        "https://alex:pass@host.example.com/private?q=secret",
        "alex project-canary machine-canary known-secret-canary",
        "a" * 195 + "known-secret-canary" * 40,
    ],
)
def test_scrubber_canaries(text):
    scrub = Scrubber(["known-secret-canary"], ["alex", "project-canary", "machine-canary"])
    result = scrub.text(text)
    for canary in [
        "canarypass",
        "canarytoken",
        "canarybearer",
        "ghp_",
        "sk-",
        "/home/",
        "/srv/",
        "Users",
        "~alex",
        "alex",
        "example",
        "192.0.2.123",
        "2001:db8",
        "project-canary",
        "machine-canary",
        "known-secret-canary",
    ]:
        assert canary not in result
    assert len(result) <= 300


def test_https_only():
    assert collector_url("https://telemetry.example.com") == "https://telemetry.example.com"
    for url in [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com/?q=a",
        "https://example.com/#fragment",
        "file:///tmp/collector",
    ]:
        with pytest.raises(ValueError):
            collector_url(url)


@pytest.fixture
def telemetry(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.models import Base
    from acc.server.telemetry import Telemetry

    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("ACC_TEST", raising=False)
    monkeypatch.delenv("RYNHULT_TEST", raising=False)  # its alias wins (host isolation sets both)
    engine = make_engine("sqlite:///" + str(tmp_path / "test.db"))
    Base.metadata.create_all(engine)
    client = Telemetry(SimpleNamespace(data_dir=tmp_path), make_sessionmaker(engine))
    yield client
    engine.dispose()


async def test_off_never_sends_and_preview_equals_wire(telemetry):
    import httpx

    from acc.server.models import TelemetryItem

    calls = []

    def receive(request):
        calls.append(request)
        return httpx.Response(200)

    telemetry.transport = httpx.MockTransport(receive)
    with telemetry.db() as s:
        body = telemetry.usage(s)
        item = TelemetryItem(
            id="preview",
            kind="usage",
            key_id=json.loads(body)["install"],
            body=body,
            state="approved",
            created=0,
            due=0,
            attempts=0,
        )
        s.add(item)
        await telemetry.send(s, item)
        assert calls == []
        telemetry.configure(s, True, False, "https://collector.example.com", "owner")
        # configure purges old previews on controller change; use the exact new preview.
        body = telemetry.usage(s)
        item = TelemetryItem(
            id="new",
            kind="usage",
            key_id=json.loads(body)["install"],
            body=body,
            state="approved",
            created=0,
            due=0,
            attempts=0,
        )
        s.add(item)
        await telemetry.send(s, item)
        assert len(calls) == 1
        assert calls[0].content == body.encode()
        assert item.state == "sent"
        telemetry.configure(s, False, False, "https://collector.example.com", "owner")
        item.state = "approved"
        await telemetry.send(s, item)
        assert len(calls) == 1


async def test_review_ci_redirects_backoff_and_separate_rotation(telemetry, monkeypatch):
    import time

    import httpx

    from acc.server.models import TelemetryItem

    with telemetry.db() as s:
        telemetry.configure(s, False, True, "https://collector.example.com", "owner")
        a, b = telemetry.key(s, "usage"), telemetry.key(s, "reports")
        assert a["id"] != b["id"]
        assert telemetry.key(s, "usage", a["created"] + 91 * 86400)["id"] != a["id"]
        s.commit()
    telemetry.capture(RuntimeError("private /home/alex/path"))
    with telemetry.db() as s:
        item = s.query(TelemetryItem).filter_by(kind="reports").one()
        calls = []
        telemetry.transport = httpx.MockTransport(
            lambda r: (calls.append(r) or httpx.Response(302, headers={"location": "https://evil.example.com"}))
        )
        await telemetry.send(s, item)
        assert not calls  # ask each time
        item.state = "approved"
        monkeypatch.setenv("CI", "")
        await telemetry.send(s, item)
        assert not calls
        monkeypatch.delenv("CI")
        await telemetry.send(s, item)
        assert len(calls) == 1 and item.state == "retry"
        assert item.due > time.time()
        await telemetry.send(s, item)
        assert len(calls) == 1


def test_crashed_session_is_metadata_only(telemetry):
    from acc.server.models import TelemetryItem

    with telemetry.db() as s:
        telemetry.configure(s, False, True, "https://collector.example.com", "owner")
        s.commit()
    telemetry.capture(RuntimeError("transcript-canary"), "session_crash", "session", "codex", -9)
    with telemetry.db() as s:
        body = s.query(TelemetryItem).one().body
        report = json.loads(body)
        assert "transcript-canary" not in body
        assert report["message"] == "" and report["frames"] == []
        assert "build" not in report and "log" not in report


async def test_reference_collector_signatures_validation_delete_retention(tmp_path):
    import base64
    import time

    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from acc.telemetry_collector import create_collector
    from acc.telemetry_schema import key_id

    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    install = key_id(public)
    app = create_collector(tmp_path / "collector.db")

    def headers(path, body):
        stamp = str(int(time.time()))
        signature = private.sign(stamp.encode() + b"\n" + path.encode() + b"\n" + body)
        return {
            "X-ACC-Key": base64.b64encode(public).decode(),
            "X-ACC-Time": stamp,
            "X-ACC-Signature": base64.b64encode(signature).decode(),
        }

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://collector.example.com") as c:
        body = encode(Usage(**(usage() | {"install": install, "day": datetime.now(timezone.utc).date()})))
        assert (await c.post("/v1/usage", content=body)).status_code == 401
        assert (await c.post("/v1/usage", content=body, headers=headers("/v1/usage", body))).status_code == 202
        assert (await c.post("/v1/report", content=body, headers=headers("/v1/usage", body))).status_code == 401
        bad = body[:-1] + b',"prompt":"private"}'
        assert (await c.post("/v1/usage", content=bad, headers=headers("/v1/usage", bad))).status_code == 422
        deletion = encode({"install": install, "reports": []})
        response = await c.post("/v1/delete", content=deletion, headers=headers("/v1/delete", deletion))
        assert response.status_code == 200 and response.json()["receipt"]
    import sqlite3

    with sqlite3.connect(tmp_path / "collector.db") as db:
        assert db.execute("select count(*) from payload").fetchone()[0] == 0


def test_audit_counters_are_closed_and_off_by_default(telemetry):
    from acc.server.models import AuditLog, TelemetryCounter

    with telemetry.db() as s:
        s.add(AuditLog(actor="private-user", action="multirun.start", detail={"prompt": "private"}))
        s.commit()
        assert s.query(TelemetryCounter).count() == 0
        telemetry.configure(s, True, False, "https://collector.example.com", "owner")
        s.commit()
        s.add(AuditLog(actor="private-user", action="multirun.start", detail={"prompt": "private"}))
        s.commit()
        counter = s.query(TelemetryCounter).one()
        assert counter.event == "session.multirun" and counter.count == 1
        assert "private" not in telemetry.usage(s)


async def test_owner_api_preview_and_consent(api):
    response = await api.get("/api/telemetry")
    assert response.status_code == 200
    assert response.json()["consent"]["usage"] is False
    response = await api.get("/api/telemetry/preview")
    assert response.status_code == 200
    assert json.loads(response.json()["usage"])["schema"] == "usage/1"
    assert (
        await api.put("/api/telemetry", json={"usage": False, "reports": False, "url": "http://collector.example.com"})
    ).status_code == 422


def test_contract_files_and_counter_names():
    from pathlib import Path
    from typing import get_args

    from acc.server.telemetry import AUDIT_COUNTERS, ROUTE_COUNTERS
    from acc.telemetry_schema import Event, Report, wire_schema

    for name, model in [("usage-1", Usage), ("report-1", Report)]:
        assert json.loads(Path(f"schemas/telemetry/{name}.json").read_text()) == wire_schema(model)
    assert set(AUDIT_COUNTERS.values()) | set(ROUTE_COUNTERS.values()) <= set(get_args(Event))


def test_collector_retention_and_anonymous_monthly_totals(tmp_path):
    import sqlite3
    from datetime import datetime, timezone

    from acc.telemetry_collector import create_collector, retain

    path = tmp_path / "collector.db"
    create_collector(path)
    now = datetime(2026, 10, 23, tzinfo=timezone.utc).timestamp()
    with sqlite3.connect(path) as db:
        for i in range(12):
            data = usage() | {"day": "2026-06-01", "install": str(i).zfill(22)}
            db.execute(
                "INSERT INTO payload VALUES (?,?,?,?,?,?)",
                (str(i), data["install"], "usage", "2026-06-01", json.dumps(data), now - 100 * 86400),
            )
        db.execute(
            "INSERT INTO payload VALUES (?,?,?,?,?,?)",
            ("old-report", "a" * 22, "reports", "2026-01-01", "{}", now - 181 * 86400),
        )
        retain(db, now)
        assert db.execute("SELECT count(*) FROM payload").fetchone()[0] == 0
        month, totals = db.execute("SELECT month,totals FROM monthly").fetchone()
        assert month == "2026-06" and json.loads(totals)["os:linux"] == 12
        assert "install" not in totals
        retain(db, datetime(2027, 11, 1, tzinfo=timezone.utc).timestamp())
        assert db.execute("SELECT count(*) FROM monthly").fetchone()[0] == 0


async def test_local_retention_and_schema_consent(telemetry):
    import time

    from acc.server.models import Setting, TelemetryItem

    with telemetry.db() as s:
        telemetry.configure(s, False, True, "https://collector.example.com", "owner")
        s.commit()
    telemetry.capture(RuntimeError("first"))
    with telemetry.db() as s:
        item = s.query(TelemetryItem).one()
        item.created = time.time() - 8 * 86400
        s.commit()
    await telemetry.tick()
    with telemetry.db() as s:
        assert s.query(TelemetryItem).count() == 0
        c = s.get(Setting, "telemetry.consent")
        c.value = c.value | {"usage": True, "usage_schema": "usage/0"}
        assert not telemetry.consent(s)["usage"]


async def test_enabled_api_preview_is_the_queued_send(api, monkeypatch):
    import httpx

    from acc.server.models import TelemetryItem

    monkeypatch.delenv("ACC_TEST", raising=False)

    monkeypatch.delenv("RYNHULT_TEST", raising=False)  # its alias wins (host isolation sets both)
    monkeypatch.delenv("CI", raising=False)
    client = await telemetry_client(api)
    calls = []
    client.transport = httpx.MockTransport(lambda request: (calls.append(request.content) or httpx.Response(202)))
    assert (
        await api.put("/api/telemetry", json={"usage": True, "reports": False, "url": "https://collector.example.com"})
    ).status_code == 200
    preview = (await api.get("/api/telemetry/preview")).json()["usage"]
    with client.db() as s:
        item = s.query(TelemetryItem).filter_by(kind="usage").one()
        item.due = 0
        await client.send(s, item)
        assert calls == [preview.encode()]


async def test_all_telemetry_endpoints_are_owner_only(api, monkeypatch):
    from acc.server.api.context import Principal

    monkeypatch.setattr(api.app.state.ctx, "principal", lambda request: Principal("member", "member", "agent"))
    for method, path, payload in [
        ("GET", "/api/telemetry", None),
        ("GET", "/api/telemetry/preview", None),
        ("PUT", "/api/telemetry", {"usage": True, "reports": True}),
        ("POST", "/api/telemetry/delete", {}),
        ("POST", "/api/telemetry/reports/private-id", {"decision": "send"}),
    ]:
        assert (await api.request(method, path, json=payload)).status_code == 403


@pytest.mark.parametrize("secret", ["known-secret-very-private", "sk-" + "A" * 45])
def test_scrub_encoded_secrets_before_truncating(secret):
    import base64
    from urllib.parse import quote

    scrub = Scrubber([secret], ["alex"])
    for encoded in [secret, quote(secret, safe=""), base64.b64encode(secret.encode()).decode()]:
        result = scrub.text("x " * 95 + encoded + " end")
        assert secret[:12] not in result and encoded[:12] not in result


def test_reports_have_only_acc_frames_and_a_size_cap(telemetry):
    from acc.server.models import TelemetryItem

    with telemetry.db() as s:
        telemetry.configure(s, False, True, "https://collector.example.com", "owner")
        s.commit()
    try:
        raise RuntimeError("/home/alex/private " + "x" * 100000)
    except RuntimeError as error:
        telemetry.capture(error)
    with telemetry.db() as s:
        body = s.query(TelemetryItem).one().body
        assert len(body.encode()) <= 16384
        assert json.loads(body)["frames"] == []  # this test module is not Rynhult code
        assert "/home/alex" not in body


async def test_delete_uses_retired_collector_and_keeps_receipt(telemetry):
    import httpx

    from acc.server.models import TelemetryItem

    receipt = "c2183697-dc0c-4efc-8805-84bf19dbd848"
    calls = []
    telemetry.transport = httpx.MockTransport(
        lambda request: (calls.append(str(request.url)) or httpx.Response(200, json={"receipt": receipt}))
    )
    with telemetry.db() as s:
        telemetry.configure(s, True, False, "https://first.example.com", "owner")
        first = telemetry.key(s, "usage")["id"]
        telemetry.configure(s, False, False, "https://second.example.com", "owner")
        telemetry.request_delete(s, "owner")
        item = s.query(TelemetryItem).filter_by(kind="delete", key_id=first).one()
        await telemetry.send(s, item)
        assert calls == ["https://first.example.com/v1/delete"]
        assert item.receipt == receipt


@pytest.mark.parametrize("reply", ["compressed", "integer"])
async def test_deletion_receipts_reject_compression_and_non_string_ids(telemetry, reply):
    import gzip

    import httpx

    from acc.server.models import TelemetryItem

    receipt = "c2183697-dc0c-4efc-8805-84bf19dbd848"
    if reply == "compressed":
        response = httpx.Response(
            200, content=gzip.compress(json.dumps({"receipt": receipt}).encode()),
            headers={"content-encoding": "gzip"},
        )
    else:
        response = httpx.Response(200, json={"receipt": 123})
    telemetry.transport = httpx.MockTransport(lambda request: response)
    with telemetry.db() as s:
        telemetry.configure(s, True, False, "https://collector.example.com", "owner")
        telemetry.request_delete(s, "owner")
        s.flush()
        item = s.query(TelemetryItem).filter_by(kind="delete").one()
        await telemetry.send(s, item)
        assert item.state == "retry" and not item.receipt and item.result == "sending failed"


async def test_report_pipeline_scrubs_installation_vault_and_retired_values(api):
    from acc.server.models import TelemetryItem, VaultRetired

    old, current = "vault-old-canary-value", "vault-current-canary-value"
    key = (
        await api.post("/api/vault/keys", json={"name": "Private key label", "variable": "CANARY_KEY", "value": old})
    ).json()
    assert (await api.post(f"/api/vault/keys/{key['id']}/replace", json={"value": current})).status_code == 200
    assert (
        await api.put("/api/telemetry", json={"usage": False, "reports": True, "url": "https://collector.example.com"})
    ).status_code == 200
    client = await telemetry_client(api)
    # The vault retains old values only while a session holds them. Seed that sealed history
    # directly here: this privacy test does not need to launch an agent process.
    with client.db() as s:
        s.add(
            VaultRetired(
                key_id=key["id"],
                name="Private key label",
                variable="CANARY_KEY",
                ciphertext=client.vault.cipher.seal(key["id"], "CANARY_KEY", old),
            )
        )
        s.commit()
    client.capture(RuntimeError(old + " " + current + " /home/alex/private host.example.lan alex@example.com"))
    with client.db() as s:
        item = s.query(TelemetryItem).filter_by(kind="reports").one()
        assert old not in item.body and current not in item.body
        assert "Private key label" not in item.body
        assert "/home/" not in item.body and "example" not in item.body


async def test_report_approval_sends_the_reviewed_bytes(api, monkeypatch):
    import httpx

    from acc.server.models import TelemetryItem

    client = await telemetry_client(api)
    calls = []
    client.transport = httpx.MockTransport(lambda request: (calls.append(request.content) or httpx.Response(202)))
    monkeypatch.delenv("ACC_TEST", raising=False)
    monkeypatch.delenv("RYNHULT_TEST", raising=False)  # its alias wins (host isolation sets both)
    monkeypatch.delenv("CI", raising=False)
    await api.put("/api/telemetry", json={"usage": False, "reports": True, "url": "https://collector.example.com"})
    client.capture(RuntimeError("Rynhult failed safely"))
    data = (await api.get("/api/telemetry")).json()
    report = next(item for item in data["items"] if item["kind"] == "reports")
    await client.tick()
    assert calls == []
    assert (
        await api.post(
            f"/api/telemetry/reports/{report['id']}", json={"decision": "send", "expected_digest": report["digest"]}
        )
    ).status_code == 200
    with client.db() as s:
        item = s.get(TelemetryItem, report["id"])
        await client.send(s, item)
        assert calls == [report["body"].encode()]


def test_scrubber_includes_secret_file_json_values(telemetry):
    directory = telemetry.settings.data_dir / "secrets"
    directory.mkdir()
    (directory / "dns.json").write_text(json.dumps({"credentials": {"value": "file-secret-canary"}}))
    with telemetry.db() as s:
        assert "file-secret-canary" not in telemetry.scrubber(s).text("failed file-secret-canary")


@pytest.mark.parametrize(
    "text,canaries",
    [
        ("vault-canary-\nsecret-value", ["vault-canary", "secret-value"]),
        ("sk-canary12345\n678901234567890123456", ["canary12345", "678901234567890123456"]),
        ("Cookie: session=shortprivate; csrf=anotherprivate", ["shortprivate", "anotherprivate"]),
        ("Set-Cookie: auth=shortprivate; HttpOnly", ["shortprivate"]),
        ("/Users/Alex Example/private/file", ["Alex", "Example", "private/file"]),
        (r"C:\Users\Alex Example\private\file", ["Alex", "Example", r"private\file"]),
        ("~alex.example/private", ["alex", "example", "private"]),
        ("fe80::1%private-interface", ["fe80", "private-interface"]),
        ("%73%6b%2d" + "%41" * 25, ["%41", "AAAA"]),
        ("vault%2dcanary%2dsecret%2dvalue", ["canary", "secret-value", "%2d"]),
        ("Alex%20Example", ["Alex", "Example"]),
        ("vault%252dcanary%252dsecret%252dvalue", ["canary", "%25"]),
        ("file:///Users/Alex Example/private/file", ["Alex", "Example", "private/file"]),
        ("Cookie: session=shortprivate\n csrf=anotherprivate", ["shortprivate", "anotherprivate"]),
        ("/Users/Jo's House/private/file", ["Jo", "House", "private/file"]),
        (r'C:\Users\Odd<name>\private\file', ["Odd", "name", r"private\file"]),
        ("ASIA1234567890ABCDEF", ["ASIA1234567890ABCDEF"]),
        ("Authorization: Basic dTpw", ["dTpw"]),
        ("'Authorization': 'Basic dTpw'", ["dTpw"]),
        ('{"api_key": "shortprivate"}', ["shortprivate"]),
        ("reports/Jo's House/private.txt", ["reports", "Jo", "House", "private"]),
        (r"reports\Jo's House\private.txt", ["reports", "Jo", "House", "private"]),
    ],
)
def test_scrubber_wrapping_encodings_headers_and_spaced_paths(text, canaries):
    result = Scrubber(["vault-canary-secret-value"], ["Alex Example"]).text(text)
    for canary in canaries:
        assert canary not in result


def test_scrubber_short_known_credentials():
    scrubber = Scrubber(["p!n42", "known-line\nsecret"])
    for text in ["p!n42", "cCFuNDI=", "known-line\nsecret"]:
        result = scrubber.text("failed " + text)
        assert text not in result
        assert "known-line" not in result and "secret" not in result.replace("«secret»", "")


@pytest.mark.parametrize("value", ["AKIA1234567890ABCDEF", "alex@example.com", "private.example.lan", "/Users/Q/private"])
def test_scrubber_removes_unknown_base64_identifiers_and_provider_keys(value):
    import base64

    encoded = base64.b64encode(value.encode()).decode()
    result = Scrubber().text("error " + encoded)
    assert encoded not in result and encoded.rstrip("=") not in result


@pytest.mark.parametrize("text", ["alpha\nbeta gamma", "alpha beta\rgamma", "Alex\nExample"])
def test_multiline_text_cannot_hide_whitespace_inside_known_secrets_or_names(text):
    result = Scrubber(["alpha beta gamma"], ["Alex Example"]).text(text)
    for fragment in ("alpha", "beta", "gamma", "Alex", "Example"):
        assert fragment not in result


@pytest.mark.parametrize("text,canary", [("Alex+Example", "Alex"), ("user Q failed", "Q"), ("UQ==", "UQ")])
def test_encoded_and_one_character_known_user_names_are_removed(text, canary):
    assert canary not in Scrubber(identifiers=["Alex Example", "Q"]).text(text)


@pytest.mark.parametrize("value", [
    "sk-" + "A" * 24,
    "ghp_" + "A" * 36,
    "xoxb-" + "A" * 15,
    "AKIA" + "A" * 16,
    "ASIA" + "A" * 16,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJleGFtcGxlIn0.YWJjZGVmZ2hpamtsbW5vcA", # gitleaks:allow -- synthetic fixture
])
def test_provider_keys_and_jwts_are_removed_without_an_inventory(value):
    assert Scrubber().text("failed " + value) == "failed «secret»"


@pytest.mark.parametrize("kind", ["usage", "reports"])
async def test_client_and_collector_reject_oversized_canonical_payload_bytes(telemetry, tmp_path, kind):
    import httpx

    from acc.server.models import TelemetryItem
    from acc.telemetry_collector import create_collector
    from acc.telemetry_schema import Report

    calls = []
    telemetry.transport = httpx.MockTransport(lambda request: (calls.append(request) or httpx.Response(202)))
    with telemetry.db() as s:
        telemetry.configure(s, True, True, "https://collector.example.com", "owner")
        key = telemetry.key(s, kind)
        if kind == "usage":
            body = encode(Usage(**(usage() | {"install": key["id"], "acc": "9" * 4500 + ".0.0"})))
        else:
            body = encode(Report(
                schema="report/1", report="11111111-1111-4111-8111-111111111111",
                install=key["id"], acc="9" * 17000 + ".0.0", os="linux", install_kind="git-checkout",
                trigger="server_error", component="server", exception="RuntimeError",
                fingerprint="a" * 16, frames=[],
            ))
        item = TelemetryItem(
            id="oversized", kind=kind, key_id=key["id"], body=body.decode(),
            state="approved", created=0, due=0,
        )
        s.add(item)
        await telemetry.send(s, item)
        assert item.state == "discarded" and item.result == "invalid payload" and calls == []

    async def chunks():
        for start in range(0, len(body), 317):
            yield body[start:start + 317]

    app = create_collector(tmp_path / "collector.sqlite")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://collector.example.com",
    ) as client:
        endpoint = "/v1/usage" if kind == "usage" else "/v1/report"
        assert (await client.post(endpoint, content=chunks())).status_code == 413


def test_collector_destination_changes_are_audited(telemetry):
    from acc.server.models import AuditLog

    with telemetry.db() as s:
        telemetry.configure(s, False, False, "https://first.example.com", "owner")
        telemetry.configure(s, False, False, "https://second.example.com", "owner")
        s.flush()
        changes = list(s.query(AuditLog).filter_by(action="telemetry.collector").order_by(AuditLog.id))
        assert changes[-1].actor == "owner"
        assert changes[-1].detail == {
            "old": "https://first.example.com",
            "new": "https://second.example.com",
        }


async def test_managed_collector_overrides_and_removal_are_audited_while_off(telemetry, monkeypatch):
    import httpx

    from acc.server.models import AuditLog

    calls = []
    telemetry.transport = httpx.MockTransport(lambda request: (calls.append(request) or httpx.Response(202)))
    with telemetry.db() as s:
        telemetry.configure(s, False, False, "https://first.example.com", "owner")
        s.commit()
    monkeypatch.setenv("ACC_TELEMETRY_URL", "https://managed.example.com")
    await telemetry.tick()
    monkeypatch.delenv("ACC_TELEMETRY_URL")
    await telemetry.tick()
    with telemetry.db() as s:
        observed = list(s.query(AuditLog).filter_by(action="telemetry.collector", actor="system").order_by(AuditLog.id))
        assert [item.detail for item in observed] == [
            {"old": "https://first.example.com", "new": "https://managed.example.com", "source": "installation configuration"},
            {"old": "https://managed.example.com", "new": "https://first.example.com", "source": "installation configuration"},
        ]
    assert calls == []


@pytest.mark.parametrize(
    "text,canary",
    [
        ("failed at 2001:db8::123.", "2001:db8"),
        ("open \\\\host\\Users\\alex\\secret", "Users"),
        ("job c2183697-dc0c-4efc-8805-84bf19dbd848 failed", "c2183697"),
    ],
)
def test_additional_identifier_shapes(text, canary):
    assert canary not in Scrubber().text(text)


def test_report_storm_updates_only_pending_repeat_bucket(telemetry):
    from acc.server.models import TelemetryItem

    with telemetry.db() as s:
        telemetry.configure(s, False, True, "https://collector.example.com", "owner")
        s.commit()
    telemetry.capture(RuntimeError("first"))
    telemetry.capture(RuntimeError("second"))
    with telemetry.db() as s:
        item = s.query(TelemetryItem).one()
        assert json.loads(item.body)["repeats"] == "2-5"
        original = item.body
        item.state = "approved"
        s.commit()
    telemetry.capture(RuntimeError("third"))
    with telemetry.db() as s:
        assert s.query(TelemetryItem).one().body == original


async def test_policy_forces_off_and_discards_pending_usage(telemetry, monkeypatch):
    from acc.server.models import TelemetryItem

    with telemetry.db() as s:
        telemetry.configure(s, True, False, "https://collector.example.com", "owner")
        telemetry.prepare_usage(s)
        s.commit()
    monkeypatch.setenv("ACC_TELEMETRY_USAGE", "off")
    await telemetry.tick()
    with telemetry.db() as s:
        assert s.query(TelemetryItem).count() == 0
        with pytest.raises(ValueError, match="policy"):
            telemetry.configure(s, True, False, "https://collector.example.com", "owner")


async def test_stale_report_cannot_be_approved_and_edits_need_review(api):
    client = await telemetry_client(api)
    await api.put("/api/telemetry", json={"usage": False, "reports": True, "url": "https://collector.example.com"})
    client.capture(RuntimeError("first"))
    item = next(i for i in (await api.get("/api/telemetry")).json()["items"] if i["kind"] == "reports")
    client.capture(RuntimeError("second"))
    path = f"/api/telemetry/reports/{item['id']}"
    assert (await api.post(path, json={"decision": "send", "expected_digest": item["digest"]})).status_code == 409
    assert (await api.post(path, json={"decision": "edit", "remove_message": True})).status_code == 200
    edited = next(i for i in (await api.get("/api/telemetry")).json()["items"] if i["id"] == item["id"])
    assert json.loads(edited["body"])["message"] == "" and edited["state"] == "queued"
    assert (
        await api.post(path, json={"decision": "send", "expected_digest": edited["digest"], "remove_log": True})
    ).status_code == 422
    assert (await api.post(path, json={"decision": "send", "expected_digest": edited["digest"]})).status_code == 200


async def test_root_collector_change_does_not_redirect_queued_data(telemetry, monkeypatch):
    import httpx

    calls = []
    telemetry.transport = httpx.MockTransport(lambda r: (calls.append(r) or httpx.Response(202)))
    with telemetry.db() as s:
        telemetry.configure(s, True, False, "https://first.example.com", "owner")
        item = telemetry.prepare_usage(s)
        item.due = 0
        monkeypatch.setenv("ACC_TELEMETRY_URL", "https://second.example.com")
        await telemetry.send(s, item)
        assert calls == [] and item.state == "discarded"


async def test_invalid_queued_payload_is_dropped_without_transport(telemetry):
    import httpx

    calls = []
    telemetry.transport = httpx.MockTransport(lambda request: (calls.append(request) or httpx.Response(200)))
    with telemetry.db() as s:
        telemetry.configure(s, True, False, "https://collector.example.com", "owner")
        item = telemetry.prepare_usage(s)
        item.body = item.body[:-1] + ',"prompt":"private-canary"}'
        item.due = 0
        await telemetry.send(s, item)
        assert not calls and item.state == "discarded"


def test_inventory_scrubs_repo_parts_and_node_hostname(telemetry):
    from acc.server.models import Node, Project

    with telemetry.db() as s:
        s.add(
            Project(
                slug="demo",
                name="Demo",
                workspace_path="/srv/workspaces/folder-canary",
                repo="repo-owner-canary/repo-name-canary",
                account="agent",
            )
        )
        s.add(Node(id="demo-node", name="demo-node", status="active", info={"hostname": "node-host-canary"}))
        s.commit()
        text = telemetry.scrubber(s).text("repo-owner-canary repo-name-canary node-host-canary folder-canary")
        assert "canary" not in text


async def test_broker_launch_failure_reports_metadata_only(api, project, monkeypatch):
    from acc.server.broker_client import BrokerError
    from acc.server.models import TelemetryItem

    client = await telemetry_client(api)
    await api.put("/api/telemetry", json={"usage": False, "reports": True, "url": "https://collector.example.com"})
    original = api.app.state.supervisor.broker.call

    async def fail_launch(method, *args, **kwargs):
        if method == "launch":
            raise BrokerError("terminal-output-canary /home/private/secret")
        return await original(method, *args, **kwargs)

    monkeypatch.setattr(api.app.state.supervisor.broker, "call", fail_launch)
    response = await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "Demo"})
    assert response.status_code == 201
    with client.db() as s:
        body = s.query(TelemetryItem).filter_by(kind="reports").one().body
        assert "terminal-output-canary" not in body
        assert json.loads(body)["trigger"] == "session_crash"
        assert json.loads(body)["message"] == "" and json.loads(body)["frames"] == []


async def telemetry_client(api):
    # Drive telemetry directly so unrelated host refresh failures cannot race the report assertions.
    from helpers import pause_supervisor

    await pause_supervisor(api)
    return api.app.state.supervisor.telemetry


def test_usage_inventory_describes_enabled_features(telemetry):
    from acc.server.models import Project

    telemetry.settings.preview_domain = "preview.example.lan"
    (telemetry.settings.data_dir / "updates-config.json").write_text(
        json.dumps({"automatic": True, "repository": "example/acc"})
    )
    with telemetry.db() as s:
        s.add(
            Project(slug="demo", name="Demo", workspace_path="/srv/workspaces/demo", account="agent", git_backup=True)
        )
        s.commit()
        features = json.loads(telemetry.usage(s))["features_enabled"]
        assert "preview" in features and "auto_updates" in features
        assert "backups" not in features  # no HTTPS backup address configured


async def test_local_history_is_kept_for_90_days_after_send(telemetry):
    import time

    from acc.server.models import TelemetryItem

    now = time.time()
    with telemetry.db() as s:
        item = telemetry.prepare_usage(s)
        item.state, item.created, item.sent_at = "sent", now - 95 * 86400, now - 89 * 86400
        identity = item.id
        s.commit()
    await telemetry.tick()  # consent is off; this can only prune local state
    with telemetry.db() as s:
        assert s.get(TelemetryItem, identity) is not None
        s.get(TelemetryItem, identity).sent_at = now - 91 * 86400
        s.commit()
    await telemetry.tick()
    with telemetry.db() as s:
        assert s.get(TelemetryItem, identity) is None


async def test_client_to_reference_collector_round_trip_and_erasure(telemetry, tmp_path):
    import sqlite3

    import httpx

    from acc.server.models import TelemetryItem
    from acc.telemetry_collector import create_collector

    path = tmp_path / "collector.sqlite"
    telemetry.transport = httpx.ASGITransport(app=create_collector(path))
    with telemetry.db() as s:
        telemetry.configure(s, True, True, "https://collector.example.com", "owner")
        item = telemetry.prepare_usage(s)
        item.due = 0
        await telemetry.send(s, item)
        assert item.state == "sent"
        s.commit()
    telemetry.capture(RuntimeError("Safe demonstration error"))
    with telemetry.db() as s:
        item = s.query(TelemetryItem).filter_by(kind="reports").one()
        item.state = "approved"
        await telemetry.send(s, item)
        assert item.state == "sent"
        s.commit()
    with sqlite3.connect(path) as collector:
        assert collector.execute("SELECT count(DISTINCT install) FROM payload").fetchone()[0] == 2
    with telemetry.db() as s:
        telemetry.request_delete(s, "owner")
        s.flush()
        for item in s.query(TelemetryItem).filter_by(kind="delete"):
            await telemetry.send(s, item)
            assert item.state == "sent" and item.receipt
        s.commit()
    with sqlite3.connect(path) as collector:
        assert collector.execute("SELECT count(*) FROM payload").fetchone()[0] == 0


@pytest.mark.parametrize("unsafe", ["directory", "file", "symlink"])
def test_collector_refuses_unsafe_existing_storage(tmp_path, unsafe):
    from acc.telemetry_collector import create_collector

    directory = tmp_path / "collector"
    directory.mkdir(mode=0o700)
    path = directory / "collector.sqlite"
    if unsafe == "directory":
        directory.chmod(0o755)
    elif unsafe == "file":
        path.touch(mode=0o644)
        path.chmod(0o644)
    else:
        target = directory / "other.sqlite"
        target.touch(mode=0o600)
        path.symlink_to(target)
    with pytest.raises(ValueError, match="private|regular"):
        create_collector(path)


def test_collector_refuses_wal_that_can_retain_deleted_payloads(tmp_path):
    import sqlite3

    from acc.telemetry_collector import create_collector

    path = tmp_path / "collector.sqlite"
    create_collector(path)
    with sqlite3.connect(path) as held:
        held.execute("PRAGMA journal_mode=WAL")
        held.execute("PRAGMA wal_autocheckpoint=0")
        held.execute(
            "INSERT INTO payload VALUES (?,?,?,?,?,?)",
            ("canary", "a" * 22, "reports", "2026-10-23", "wal-retention-canary", 0),
        )
        held.commit()
        held.execute("PRAGMA secure_delete=ON")
        held.execute("DELETE FROM payload")
        held.commit()
        assert b"wal-retention-canary" in path.with_name(path.name + "-wal").read_bytes()
        with pytest.raises(ValueError, match="journal"):
            create_collector(path)


def test_default_collector_factory_uses_private_storage_without_chmodding_checkout(tmp_path, monkeypatch):
    import stat

    from deploy.telemetry.app import create_app

    checkout = tmp_path / "deployment"
    checkout.mkdir(mode=0o755)
    checkout.chmod(0o755)
    monkeypatch.chdir(checkout)
    monkeypatch.delenv("ACC_COLLECTOR_DB", raising=False)
    create_app()
    assert stat.S_IMODE(checkout.stat().st_mode) == 0o755
    databases = list(checkout.rglob("*.sqlite"))
    assert len(databases) == 1 and databases[0].parent != checkout
    assert stat.S_IMODE(databases[0].parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(databases[0].stat().st_mode) == 0o600


def test_default_collector_factory_does_not_silently_abandon_legacy_data(tmp_path, monkeypatch):
    from deploy.telemetry.app import create_app

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ACC_COLLECTOR_DB", raising=False)
    legacy = tmp_path / "telemetry.sqlite"
    legacy.write_bytes(b"legacy-data-canary")
    with pytest.raises(ValueError, match="Legacy"):
        create_app()
    assert legacy.read_bytes() == b"legacy-data-canary"
    assert not (tmp_path / "telemetry-data").exists()


async def test_idle_collector_retention_recovers_after_database_error(tmp_path, monkeypatch, caplog):
    import asyncio
    import sqlite3
    from types import SimpleNamespace

    from acc import telemetry_collector as collector

    path = tmp_path / "collector.sqlite"
    app = collector.create_collector(path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO payload VALUES (?,?,?,?,?,?)", ("old", "a" * 22, "reports", "2020-01-01", "{}", 0))
    real_retain = collector.retain
    attempts, delays = [], []
    recovered, waiting = asyncio.Event(), asyncio.Event()

    def retain(db, now):
        attempts.append(now)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("private-database-detail-canary")
        real_retain(db, now)

    async def sleep(delay):
        delays.append(delay)
        if len(delays) > 1:
            recovered.set()
            await waiting.wait()

    monkeypatch.setattr(collector, "retain", retain)
    monkeypatch.setattr(
        collector, "asyncio",
        SimpleNamespace(create_task=asyncio.create_task, sleep=sleep, CancelledError=asyncio.CancelledError),
    )
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(recovered.wait(), 1)
        assert len(attempts) == 2
        assert 0 < delays[0] <= 60 and delays[1] == 3600
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM payload").fetchone()[0] == 0
    assert "retention" in caplog.text.lower()
    assert "private-database-detail-canary" not in caplog.text


def test_privacy_upgrade_discards_old_unsent_reports_not_reviewed_bytes(tmp_path):
    from alembic import command

    from acc.server.attention import raise_item
    from acc.server.db import alembic_config, make_engine, make_sessionmaker, migrate
    from acc.server.models import AttentionItem, TelemetryItem

    url = "sqlite:///" + str(tmp_path / "upgrade.sqlite")
    command.upgrade(alembic_config(url), "0048")
    engine = make_engine(url)
    db = make_sessionmaker(engine)
    try:
        with db() as s:
            for identity, kind, state in [
                ("queued", "reports", "queued"),
                ("approved", "reports", "approved"),
                ("retry", "reports", "retry"),
                ("sent", "reports", "sent"),
                ("usage", "usage", "approved"),
                ("delete", "delete", "approved"),
            ]:
                s.add(TelemetryItem(
                    id=identity, kind=kind, state=state, key_id="a" * 22,
                    body='{"message":"pre-fix-canary"}', created=0, due=0,
                ))
                if kind == "reports" and state != "sent":
                    raise_item(s, "telemetry:" + identity, kind="telemetry_report", source="telemetry", title="Review")
            s.commit()
        migrate(url)
        with db() as s:
            assert {item.id for item in s.query(TelemetryItem)} == {"sent", "usage", "delete"}
            assert all(item.resolved_at for item in s.query(AttentionItem))
    finally:
        engine.dispose()


async def test_sender_rejects_untrusted_tls_certificate_before_http_payload(telemetry, tmp_path):
    import asyncio
    import ipaddress
    import ssl
    from datetime import timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(private, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    requests = []

    async def receive(reader, writer):
        requests.append(await reader.read(8192))
        writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(receive, "127.0.0.1", 0, ssl=context)
    async with server:
        port = server.sockets[0].getsockname()[1]
        with telemetry.db() as s:
            telemetry.configure(s, True, False, f"https://127.0.0.1:{port}", "owner")
            item = telemetry.prepare_usage(s)
            item.due = 0
            await telemetry.send(s, item)
            assert item.state == "retry" and item.result == "sending failed"
            assert requests == []


async def test_repeated_telemetry_lifetimes_release_database_descriptors(tmp_path, monkeypatch):
    """A flush listener must not retain its telemetry owner and SQLite pool forever."""
    import gc
    import os
    import weakref
    from pathlib import Path
    from types import SimpleNamespace

    import httpx

    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.models import Base
    from acc.server.telemetry import Telemetry
    from acc.telemetry_collector import create_collector

    if not Path('/proc/self/fd').is_dir():
        pytest.skip('Linux descriptor accounting required')
    monkeypatch.delenv('CI', raising=False)
    monkeypatch.delenv('ACC_TEST', raising=False)
    monkeypatch.delenv('RYNHULT_TEST', raising=False)  # its alias wins (host isolation sets both)

    async def exercise(index):
        root = tmp_path / str(index)
        root.mkdir(mode=0o700)
        engine = make_engine('sqlite:///' + str(root / 'acc.db'))
        Base.metadata.create_all(engine)
        client = Telemetry(SimpleNamespace(data_dir=root), make_sessionmaker(engine),
                           transport=httpx.ASGITransport(app=create_collector(root / 'collector.db')))
        with client.db() as s:
            client.configure(s, True, True, 'https://collector.example.com', 'owner')
            preview = client.usage(s)
            assert json.loads(preview)['schema'] == 'usage/1'
            assert 'canary' not in client.scrubber(s).text('password=canary')
            item = client.prepare_usage(s)
            item.due = 0
            await client.send(s, item)
            assert item.state == 'sent'
            s.commit()
        # Exercise normal reclamation; disposing here would conceal a retained pool.
        return weakref.ref(client)

    def ours():
        # Count only this test's descriptors: earlier tests' leftovers may be collected meanwhile.
        count = 0
        for fd in os.listdir('/proc/self/fd'):
            try:
                count += os.readlink('/proc/self/fd/' + fd).startswith(str(tmp_path))
            except OSError:
                pass
        return count

    await exercise('warmup')
    gc.collect()
    baseline = ours()
    owners = []
    for index in range(20):
        owners.append(await exercise(index))
        gc.collect()
        assert ours() == baseline
    assert all(owner() is None for owner in owners)


def test_expired_key_row_goes_before_its_file_and_never_during_a_capture(telemetry, monkeypatch):
    """Review 2026-10-25 (L-2): the file was deleted before the row was committed, so a capture's snapshot could list a
    key whose file its later secrets/ copy no longer had."""
    import os
    import pathlib
    import time

    from acc.maintenance import lease, secrets_lock
    from acc.server.models import Setting

    with telemetry.db() as s:
        telemetry.configure(s, False, True, "https://collector.example.com", "owner")
        key = telemetry.key(s, "reports")
        s.commit()
    path = telemetry.directory / (key["id"] + ".key")
    listed = lambda: [k["id"] for k in telemetry.db().get(Setting, "telemetry.keys").value["items"]]  # noqa: E731
    later = time.time() + 398 * 86400

    with lease(secrets_lock(telemetry.settings.data_dir)):  # a capture between its snapshot and its secrets/ copy
        with telemetry.db() as s:
            telemetry.retire_keys(s, later)
    assert listed() == [key["id"]] and path.exists()  # both, as the capture sees them

    seen = []
    unlink = pathlib.Path.unlink
    def observed(self, *args, **kwargs):
        seen.append((self.name, listed()))
        return unlink(self, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, "unlink", observed)
    with telemetry.db() as s:
        telemetry.retire_keys(s, later)
    assert listed() == [] and not path.exists()
    assert seen == [(path.name, [])]  # the row was committed away before the file went

    # A crash between the two leaves an unlisted file: swept once it's a day old, never a key being created now.
    stale, fresh = telemetry.directory / "stale.key", telemetry.directory / "fresh.key"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(stale, (time.time() - 2 * 86400,) * 2)
    with telemetry.db() as s:
        telemetry.retire_keys(s, time.time())
    assert not stale.exists() and fresh.exists()


@pytest.mark.parametrize('extra', [{'build': 'a' * 40}, {'build': 'dev'}, {'log': []}, {'log': ['private-canary']}])
async def test_sr10_4_client_discards_legacy_report_fields(telemetry, extra):
    import httpx

    from acc.server.models import TelemetryItem

    calls = []
    telemetry.transport = httpx.MockTransport(lambda request: (calls.append(request) or httpx.Response(202)))
    with telemetry.db() as s:
        telemetry.configure(s, False, True, 'https://collector.example.com', 'owner')
        s.commit()
    telemetry.capture(RuntimeError('failure'), 'server_error', 'server')
    with telemetry.db() as s:
        item = s.query(TelemetryItem).filter_by(kind='reports').one()
        payload = json.loads(item.body)
        item.body = encode(payload | extra).decode()
        item.state = 'approved'
        await telemetry.send(s, item)
        assert item.state == 'discarded' and item.result == 'invalid payload'
        assert calls == []
