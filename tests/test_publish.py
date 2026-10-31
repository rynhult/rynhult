"""Publishing flow: request -> approval -> Cloudflare (mocked API) + root helper (real code, no Caddy)."""
from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from helpers import wait_for

from acc.server.cloudflare import Cloudflare

HELPER = Path(__file__).resolve().parents[1] / "deploy/publish/acc-publish"


class FakeCloudflareAPI:
    """In-memory stand-in for the parts of the Cloudflare v4 API that ACC uses."""

    def __init__(self):
        self.zones = [{"id": "z1", "name": "example.com", "status": "active", "permissions": ["#dns_records:edit"]},
                      {"id": "z2", "name": "example.lan", "status": "active", "permissions": ["#dns_records:edit"]}]
        self.records: dict[str, dict] = {}
        self.counter = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path.replace("/client/v4", ""), request.method
        ok = lambda result: httpx.Response(200, json={"success": True, "result": result})
        if path == "/zones":
            return ok(self.zones)
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[2] == "dns_records":
            if len(parts) == 3 and method == "GET":
                name = request.url.params.get("name")
                return ok([r for r in self.records.values() if r["name"] == name])
            if len(parts) == 3 and method == "POST":
                self.counter += 1
                record = {**json.loads(request.content), "id": f"r{self.counter}", "zone": parts[1]}
                self.records[record["id"]] = record
                return ok(record)
            record_id = parts[3]
            if method == "GET":
                return ok(self.records[record_id])
            if method == "PUT":
                self.records[record_id].update(json.loads(request.content))
                return ok(self.records[record_id])
            if method == "DELETE":
                self.records.pop(record_id)
                return ok({"id": record_id})
        return httpx.Response(404, json={"success": False, "errors": [{"message": "not found"}]})


@pytest.fixture
async def world(api, env, tmp_path, monkeypatch):
    """Wire the app's publisher to the fake API and run the real helper against a temp Caddy dir."""
    cf_api = FakeCloudflareAPI()
    publisher = api.app.state.publisher
    publisher._cloudflare = Cloudflare("test-token", transport=httpx.MockTransport(cf_api.handler))

    async def https_check(host, ip):
        return "HTTP/1.1 200 OK"
    publisher.https_check = https_check

    monkeypatch.setenv("ACC_PUBLISH_ROOT", str(env["settings"].publish_dir))
    monkeypatch.setenv("ACC_CADDY_DIR", str(tmp_path / "acc.d"))
    monkeypatch.setenv("ACC_WILDCARD_ZONE", "example.com")
    monkeypatch.setenv("ACC_PUBLISH_ZONES", "example.com,example.lan")
    loader = importlib.machinery.SourceFileLoader("acc_publish_flow", str(HELPER))
    spec = importlib.util.spec_from_loader("acc_publish_flow", loader)
    helper = importlib.util.module_from_spec(spec)
    loader.exec_module(helper)
    helper.validate = lambda: None
    helper.reload = lambda: None
    (tmp_path / "acc.d").mkdir()

    async def run_helper():  # stands in for acc-publish.path + acc-publish.service
        while True:
            for path in sorted((env["settings"].publish_dir / "requests").glob("*.json")):
                helper.process(path)
            await asyncio.sleep(0.1)
    task = asyncio.create_task(run_helper())
    yield {"cf": cf_api, "helper": helper, "caddy": tmp_path / "acc.d"}
    task.cancel()


async def status_is(api, request_id, status):
    rows = (await api.get("/api/publish")).json()
    row = next(r for r in rows if r["id"] == request_id)
    return row if row["status"] == status else None


def agent_asks(env, session, request: dict) -> None:
    """What `acc-request` leaves in the session's spool."""
    record = {"provider": "acc", "acc_session": session["id"], "hook": {"hook_event_name": "AccRequest", "request": request}}
    with (env["run_dir"] / session["id"] / "events.jsonl").open("a") as handle:
        handle.write(json.dumps(record) + "\n")


async def inbox(api, kind="publish_request"):
    items = [i for i in (await api.get("/api/attention")).json() if i["kind"] == kind]
    return items[0] if items else None


async def test_agent_request_waits_for_approval_then_publishes(api, project, env, world):
    assert (await api.post(f"/api/projects/{project['id']}/ports", json={"port": 8004})).status_code == 201
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "web"})).json()
    agent_asks(env, session, {"type": "publish", "host": "demo.example.com", "port": 8004, "access": "internal",
                              "reason": "preview"})

    async def inbox_item():
        items = [i for i in (await api.get("/api/attention")).json() if i["kind"] == "publish_request"]
        return items[0] if items else None
    item = await wait_for(inbox_item)
    assert "demo.example.com" in item["title"] and "private networks only" in item["title"]
    assert "Port 8004 is reserved for project demo" in item["detail"] and item["publish"]["replaces"] is None
    # The Inbox carries the facts Publishing shows (UX review IN-1).
    assert item["publish"]["port_owner"] == {"project": "demo", "label": None} and item["publish"]["project"] == "demo"
    assert item["publish"]["requested_by"] == "session web" and item["publish"]["reason"] == "preview"
    assert world["cf"].records == {}  # nothing happens before approval
    assert not list(world["caddy"].rglob("*.caddy"))

    approved = await api.post(f"/api/publish/{item['publish_id']}/approve")
    assert approved.status_code == 200
    done = await wait_for(lambda: status_is(api, item["publish_id"], "applied"))
    assert "route in the *.example.com wildcard site" in done["outcome"]["route_detail"]
    assert "HTTP/1.1 200" in done["outcome"]["https"]["detail"]
    record = next(iter(world["cf"].records.values()))
    assert record["content"] == "10.0.0.10" and record["proxied"] is False and "managed by ACC" in record["comment"]
    assert (world["caddy"] / "routes-example.com/demo.example.com.caddy").exists()
    assert not [i for i in (await api.get("/api/attention")).json() if i["kind"] == "publish_request"]

    unpublished = (await api.post(f"/api/publish/{item['publish_id']}/unpublish")).json()
    await wait_for(lambda: status_is(api, unpublished["id"], "removed"))
    assert world["cf"].records == {} and not (world["caddy"] / "routes-example.com/demo.example.com.caddy").exists()


async def test_dashboard_publish_is_preapproved_public(api, world):
    created = (await api.post("/api/publish", json={"host": "app.example.lan", "port": 8100, "access": "public"})).json()
    done = await wait_for(lambda: status_is(api, created["id"], "applied"))
    assert done["decided_by"] == "@api"
    assert next(iter(world["cf"].records.values()))["content"] == "203.0.113.10"
    assert (world["caddy"] / "app.example.lan.caddy").exists()


async def test_reject_changes_nothing(api, world):
    request_id = api.app.state.publisher.request(action="publish", host="nope.example.com", port=8005, access="public",
                                                 requested_by="session x")
    assert (await api.post(f"/api/publish/{request_id}/reject")).json()["status"] == "rejected"
    assert not api.app.state.publisher.tasks
    assert world["cf"].records == {} and not list(world["caddy"].rglob("*.caddy"))


async def test_foreign_dns_record_is_never_overwritten(api, world):
    world["cf"].records["x1"] = {"id": "x1", "type": "CNAME", "name": "mail.example.com", "content": "elsewhere", "comment": ""}
    created = (await api.post("/api/publish", json={"host": "mail.example.com", "port": 8006, "access": "public"})).json()
    failed = await wait_for(lambda: status_is(api, created["id"], "failed"))
    assert "ownership" in failed["detail"]
    assert world["cf"].records["x1"]["content"] == "elsewhere"


async def test_caddy_refusal_rolls_back_dns(api, world):
    def refuse():
        raise world["helper"].Refused("caddy validate failed: test")
    world["helper"].validate = refuse
    created = (await api.post("/api/publish", json={"host": "broken.example.com", "port": 8007, "access": "public"})).json()
    failed = await wait_for(lambda: status_is(api, created["id"], "failed"))
    assert "Caddy refused" in failed["detail"] and world["cf"].records == {}
    rows = (await api.get('/api/publish/addresses')).json()
    assert (await api.post(f"/api/publish/addresses/{rows[0]['id']}/remove")).status_code == 200


async def test_bad_requests_are_refused_up_front(api, world):
    for body in ({"host": "x.example.com", "port": 22, "access": "public"},
                 {"host": "not a host", "port": 8004, "access": "public"}):
        response = await api.post("/api/publish", json=body)
        assert response.status_code in (409, 422)


@pytest.mark.parametrize("host, message", [
    ("acc", "valid hostname"), ("example.com", "reserved"), ("www.example.lan", "reserved"),
    ("x.example.org", "not in a zone"),
])
async def test_reserved_and_outside_names_refused_before_dns(api, world, host, message):
    refused = await api.post("/api/publish", json={"host": host, "port": 8100, "access": "public"})
    assert refused.status_code in (409, 422) and message in refused.text
    assert world["cf"].records == {}


# ------------------------------------------------------------------ D18: retargeting a live site is never silent

async def test_an_agent_publishes_only_its_own_projects_ports_whoever_runs_it(api, project, env, world):
    """The API token is an owner: its sessions used to skip the port check."""
    other = (await api.post("/api/projects", json={"slug": "other", "name": "Other",
                                                   "workspace_path": str(env["workspaces"] / "other")})).json()
    assert (await api.post(f"/api/projects/{other['id']}/ports", json={"port": 8010})).status_code == 201
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "web"})).json()
    agent_asks(env, session, {"type": "publish", "host": "theirs.example.com", "port": 8010, "access": "internal"})
    refused = await wait_for(lambda: inbox(api, "publish_failed"))
    assert "isn't reserved for this project" in refused["detail"]
    assert not [r for r in (await api.get("/api/publish")).json() if r["host"] == "theirs.example.com"]


async def test_retargeting_a_live_site_shows_what_it_replaces_and_needs_confirming(api, project, env, world):
    other = (await api.post("/api/projects", json={"slug": "other", "name": "Other",
                                                   "workspace_path": str(env["workspaces"] / "other")})).json()
    assert (await api.post(f"/api/projects/{other['id']}/ports", json={"port": 8020})).status_code == 201
    assert (await api.post(f"/api/projects/{project['id']}/ports", json={"port": 8004})).status_code == 201
    live = (await api.post("/api/publish", json={"host": "shop.example.com", "port": 8020, "access": "public",
                                                 "project_id": other["id"]})).json()
    await wait_for(lambda: status_is(api, live["id"], "applied"))

    # An agent in another project asks for the live name with a plausible reason
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "web"})).json()
    agent_asks(env, session, {"type": "publish", "host": "shop.example.com", "port": 8004, "access": "public",
                              "reason": "new preview"})
    item = await wait_for(lambda: inbox(api))
    assert item["title"].startswith("Retarget live site shop.example.com: port 8020 (other) → port 8004")
    assert item["severity"] == "critical" and item["publish"]["replaces"]["port"] == 8020
    assert item["publish"]["port"] == 8004 and item["publish"]["host"] == "shop.example.com"
    assert item["publish"]["replaces"]["project"] == "other" and "Asked for project demo" in item["detail"]
    row = next(r for r in (await api.get("/api/publish")).json() if r["id"] == item["publish_id"])
    assert row["replaces"]["id"] == live["id"] and row["port_owner"] == {"project": "demo", "label": None}

    plain = await api.post(f"/api/publish/{item['publish_id']}/approve")  # the old Approve button: refused
    assert plain.status_code == 409 and "retargets the live site" in plain.text
    assert (await status_is(api, live["id"], "applied"))  # still serving the old port
    confirmed = await api.post(f"/api/publish/{item['publish_id']}/approve", json={"confirm_replace": True})
    assert confirmed.status_code == 200
    await wait_for(lambda: status_is(api, item["publish_id"], "applied"))
    replaced = await status_is(api, live["id"], "removed")
    assert replaced and item["publish_id"] in replaced["detail"]
    with api.app.state.db() as s:
        from acc.server.models import AuditLog
        audit = s.query(AuditLog).filter_by(action="publish.request_publish", target="shop.example.com").all()
        assert audit[-1].detail["replaces"]["port"] == 8020


async def test_an_owner_retargets_from_the_dashboard_only_when_confirming(api, world):
    first = (await api.post("/api/publish", json={"host": "app.example.com", "port": 8100, "access": "internal"})).json()
    await wait_for(lambda: status_is(api, first["id"], "applied"))
    again = await api.post("/api/publish", json={"host": "app.example.com", "port": 8101, "access": "internal"})
    assert again.status_code == 409 and "is live, serving port 8100" in again.text
    replaced = await api.post("/api/publish", json={"host": "app.example.com", "port": 8101, "access": "internal",
                                                    "replace": True})
    assert replaced.status_code == 201 and replaced.json()["replaces"]["port"] == 8100
    await wait_for(lambda: status_is(api, replaced.json()["id"], "applied"))


async def test_an_agent_cannot_unpublish_another_projects_site(api, project, env, world):
    other = (await api.post("/api/projects", json={"slug": "other", "name": "Other",
                                                   "workspace_path": str(env["workspaces"] / "other")})).json()
    live = (await api.post("/api/publish", json={"host": "shop.example.com", "port": 8020, "access": "internal",
                                                 "project_id": other["id"]})).json()
    await wait_for(lambda: status_is(api, live["id"], "applied"))
    session = (await api.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "web"})).json()
    agent_asks(env, session, {"type": "unpublish", "host": "shop.example.com"})
    refused = await wait_for(lambda: inbox(api, "publish_failed"))
    assert "another project's site" in refused["detail"]


async def test_address_selection_ipv6_retarget_and_removal(api, world):
    entries = (await api.post('/api/publish/addresses', json={
        'address': '2606:4700:1234::/48', 'label': 'Edge IPv6', 'access': 'public'})).json()
    entry = entries[-1]
    first = (await api.post('/api/publish', json={'host': 'edge.example.com', 'port': 8100,
                                                'access': 'public', 'target_ip': '2606:4700:1234::42'})).json()
    await wait_for(lambda: status_is(api, first['id'], 'applied'))
    record = next(iter(world['cf'].records.values()))
    assert record['type'] == 'AAAA' and record['content'] == '2606:4700:1234::42'
    assert (await api.post(f"/api/publish/addresses/{entry['id']}/remove")).status_code == 409
    again = (await api.post('/api/publish', json={'host': 'edge.example.com', 'port': 8100,
        'access': 'public', 'target_ip': '203.0.113.10', 'replace': True})).json()
    await wait_for(lambda: status_is(api, again['id'], 'applied'))
    assert len(world['cf'].records) == 1
    assert next(iter(world['cf'].records.values()))['type'] == 'A'
    assert (await api.post(f"/api/publish/addresses/{entry['id']}/remove")).status_code == 200


async def test_apply_rechecks_removed_pending_address(api, world):
    rows = (await api.post('/api/publish/addresses', json={
        'address': '8.8.8.8', 'label': 'Extra', 'access': 'public'})).json()
    publisher = api.app.state.publisher
    request = publisher.request(action='publish', host='pending.example.com', port=8100, access='public',
                                requested_by='owner', target_ip='8.8.8.8')
    assert (await api.post(f"/api/publish/addresses/{rows[-1]['id']}/remove")).status_code == 200
    await api.post(f'/api/publish/{request}/approve')
    failed = await wait_for(lambda: status_is(api, request, 'failed'))
    assert 'current address list' in failed['detail'] and not world['cf'].records


async def test_owner_can_review_pinned_target_before_approval(api, world):
    request = api.app.state.publisher.request(action='publish', host='review.example.com', port=8100,
                                              access='public', requested_by='owner')
    row = next(r for r in (await api.get('/api/publish')).json() if r['id'] == request)
    assert row['target_ip'] == '203.0.113.10'


async def test_failed_caddy_update_restores_owned_dns_and_releases_new_target(api, world):
    original = (await api.post('/api/publish', json={'host': 'rollback.example.com', 'port': 8100,
                                                   'access': 'public'})).json()
    await wait_for(lambda: status_is(api, original['id'], 'applied'))
    rows = (await api.post('/api/publish/addresses', json={
        'address': '8.8.8.8', 'label': 'Extra', 'access': 'public'})).json()
    def refuse():
        raise world['helper'].Refused('Caddy refused the updated route')
    world['helper'].validate = refuse
    updated = (await api.post('/api/publish', json={'host': 'rollback.example.com', 'port': 8100,
        'access': 'public', 'target_ip': '8.8.8.8', 'replace': True})).json()
    await wait_for(lambda: status_is(api, updated['id'], 'failed'))
    assert next(iter(world['cf'].records.values()))['content'] == '203.0.113.10'
    assert (await api.post(f"/api/publish/addresses/{rows[-1]['id']}/remove")).status_code == 200
    world['helper'].validate = lambda: None
    moved = (await api.post('/api/publish', json={'host': 'rollback.example.com', 'port': 8100,
        'access': 'public', 'replace': True})).json()
    await wait_for(lambda: status_is(api, moved['id'], 'applied'))
    assert next(iter(world['cf'].records.values()))['content'] == '203.0.113.10'


async def test_owner_adopts_only_matching_historical_record_then_unpublishes(api, env, world):
    from sqlalchemy import delete
    from test_auth import PASSWORD
    from test_dns_infrastructure import owner_client

    from acc.server.models import DNSOwnedRecord
    headers = await owner_client(api, env)
    original = (await api.post('/api/publish', headers=headers, json={
        'host': 'adopt.example.com', 'port': 8100, 'access': 'public'})).json()
    await wait_for(lambda: status_is(api, original['id'], 'applied'))
    with api.app.state.db() as s:
        s.execute(delete(DNSOwnedRecord).where(DNSOwnedRecord.host == 'adopt.example.com'))
        s.commit()
    record = next(iter(world['cf'].records.values()))
    record['content'] = '203.0.113.99'
    refused = await api.post('/api/dns/adopt', headers=headers,
        json={'request_id': original['id'], 'password': PASSWORD})
    assert refused.status_code == 409 and record['content'] == '203.0.113.99'
    record['content'] = '203.0.113.10'
    adopted = await api.post('/api/dns/adopt', headers=headers,
        json={'request_id': original['id'], 'password': PASSWORD})
    assert adopted.status_code == 200, adopted.text
    assert world['cf'].counter == 1  # adoption did not create/replace remote records
    removed = (await api.post(f"/api/publish/{original['id']}/unpublish", headers=headers)).json()
    await wait_for(lambda: status_is(api, removed['id'], 'removed'))
    assert not world['cf'].records


async def test_unanswered_caddy_job_keeps_dns_and_blocks_a_blind_retry(api, world, monkeypatch):
    from sqlalchemy import select

    from acc.server.models import DNSOwnedRecord
    async def unanswered(*args):
        raise TimeoutError('private-helper-error-canary')
    monkeypatch.setattr(api.app.state.publisher, '_run_helper', unanswered)
    first = (await api.post('/api/publish', json={
        'host': 'uncertain.example.com', 'port': 8100, 'access': 'public'})).json()
    failed = await wait_for(lambda: status_is(api, first['id'], 'failed'))
    assert 'private-helper-error-canary' not in failed['detail']
    assert len(world['cf'].records) == 1
    addresses = (await api.get('/api/publish/addresses')).json()
    reserved = next(row for row in addresses if row['access'] == 'public' and row['address'] == '203.0.113.10')
    assert (await api.post(f"/api/publish/addresses/{reserved['id']}/remove")).status_code == 409
    with api.app.state.db() as s:
        row = s.scalar(select(DNSOwnedRecord).where(DNSOwnedRecord.host == 'uncertain.example.com'))
        assert row.state == 'uncertain' and row.applied['values'] == ['203.0.113.10']
    second = (await api.post('/api/publish', json={
        'host': 'uncertain.example.com', 'port': 8101, 'access': 'public'})).json()
    refused = await wait_for(lambda: status_is(api, second['id'], 'failed'))
    assert 'reconciliation' in refused['detail']
    assert world['cf'].counter == 1 and len(world['cf'].records) == 1


async def test_unanswered_unpublish_blocks_retargeting_until_reconciled(api, world, monkeypatch):
    from sqlalchemy import select

    from acc.server.models import DNSOwnedRecord
    original = (await api.post('/api/publish', json={
        'host': 'withdraw.example.com', 'port': 8100, 'access': 'public'})).json()
    await wait_for(lambda: status_is(api, original['id'], 'applied'))
    async def unanswered(*args):
        raise TimeoutError('private-helper-error-canary')
    monkeypatch.setattr(api.app.state.publisher, '_run_helper', unanswered)
    removal = (await api.post(f"/api/publish/{original['id']}/unpublish")).json()
    await wait_for(lambda: status_is(api, removal['id'], 'failed'))
    with api.app.state.db() as s:
        row = s.scalar(select(DNSOwnedRecord).where(DNSOwnedRecord.host == 'withdraw.example.com'))
        assert row.state == 'uncertain'
    replacement = (await api.post('/api/publish', json={
        'host': 'withdraw.example.com', 'port': 8101, 'access': 'public', 'replace': True})).json()
    refused = await wait_for(lambda: status_is(api, replacement['id'], 'failed'))
    assert 'reconciliation' in refused['detail'] and len(world['cf'].records) == 1


async def test_agent_refusal_is_confirmed_and_actionable(api, project, env, world):
    from acc.server.models import Observation
    session = (await api.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Lead'})).json()
    rid = 'a' * 32
    agent_asks(env, session, {'type': 'publish', 'host': 'preview.example.com', 'port': 8102,
                              'request_id': rid})
    item = await wait_for(lambda: inbox(api, 'publish_failed'))
    assert item['severity'] == 'warn'
    assert 'Reserve port 8102' in item['detail'] and 're-file' in item['detail']
    with api.app.state.db() as db:
        replies = db.get(Observation, 'request.reply.' + session['id']).value
        assert replies[rid]['status'] == 'refused'
        assert "isn't reserved" in replies[rid]['reason']


@pytest.mark.parametrize('kind', ['publish', 'tool'])
async def test_agent_approval_reply_is_delivered_and_replays_do_not_duplicate(api, project, env, world, kind):
    from sqlalchemy import select

    from acc.server.models import PublishRequest, ToolRequest
    model = PublishRequest if kind == "publish" else ToolRequest
    await api.post(f"/api/projects/{project['id']}/ports", json={'port':8102})
    session = (await api.post('/api/sessions', json={'project_id':project['id'], 'provider':'claude', 'name':'Lead'})).json()
    rid = 'b' * 32
    request = ({'type':'publish', 'host':'preview.example.com', 'port':8102, 'request_id':rid} if kind == 'publish'
               else {'type':'tool', 'manager':'npm', 'name':'prettier', 'reason':'Format output', 'request_id':rid})
    agent_asks(env, session, request)
    await wait_for(lambda: inbox(api, 'tool_request' if kind == 'tool' else 'publish_request'))
    async def delivered():
        path = env['settings'].request_replies_root / session['id'] / (rid + '.json')
        return json.loads(path.read_text()).get('reply') if path.exists() else None
    reply = await wait_for(delivered)
    assert reply['status'] == 'accepted'
    with api.app.state.db() as db:
        approval = db.get(model, reply['approval_id'])
        assert approval.status == 'pending_approval'
        assert approval.session_id == session['id']
    agent_asks(env, session, request)
    # Process the replay synchronously; no sleep can accidentally assert before ingestion.
    api.app.state.supervisor.ingest_all()
    with api.app.state.db() as db:
        assert len(list(db.scalars(select(model).where(model.session_id == session['id'])))) == 1


async def test_tool_refusal_is_returned_to_agent_and_warns_owner(api, project, env):
    from acc.server.models import Observation
    session = (await api.post('/api/sessions', json={'project_id':project['id'], 'provider':'claude', 'name':'Lead'})).json()
    rid = 'c' * 32
    agent_asks(env, session, {'type':'tool', 'manager':'sh', 'name':'anything', 'reason':'test', 'request_id':rid})
    item = await wait_for(lambda: inbox(api, 'tool_failed'))
    assert item['severity'] == 'warn' and 're-file' in item['detail']
    with api.app.state.db() as db:
        assert db.get(Observation, 'request.reply.' + session['id']).value[rid]['status'] == 'refused'
