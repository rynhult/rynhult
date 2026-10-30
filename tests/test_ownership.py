"""Ownership (multi-user step 9): a member sees and changes only their own things; owners see everything."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.routing import APIRoute
from helpers import wait_for, write_db
from starlette.routing import Mount
from test_auth import do_setup, live  # noqa: F401 (fixture)
from test_publish import world  # noqa: F401 (fixture)
from test_users import browser, csrf, join

from acc.server import auth
from acc.server.db import WRITE_INTENT
from acc.server.models import AttentionItem, Device, DevicePairing, PublishRequest, PushSubscription, Schedule


@pytest.fixture(autouse=True)
def orchestrator_preview(monkeypatch):
    """The orchestrator v2 preview on (ACC_ORCHESTRATOR=1, before the app is built), so its routes are in the inventory."""
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')


# Every route with a resource in its path must appear here (test_every_resource_route_is_covered keeps it honest),
# with how a member is answered when it's someone else's: 404 (not theirs, existence not revealed) or 403
# (owner-only).
MATRIX = {
    ("GET", "/api/projects/{project_id}/delete"): 404,
    ("POST", "/api/projects/{project_id}/delete"): 404,
    ("GET", "/api/sessions/{acc_id}/deliveries"): 404,
    ("POST", "/api/deliveries/{identifier}/retry"): 404,
    ('GET', '/api/projects/{project_id}/git-identity'): 404,
    ('PUT', '/api/projects/{project_id}/git-identity'): 404,
    ('GET', '/api/projects/{project_id}/brief'): 404,
    ('POST', '/api/projects/{project_id}/brief'): 404,
    ('POST', '/api/projects/{project_id}/brief/import'): 404,
    ('POST', '/api/projects/{project_id}/brief/documents'): 404,
    ('POST', '/api/projects/{project_id}/brief/versions/{number}/accept'): 404,
    ('POST', '/api/project-brief-references/{project_id}'): 404,
    ('GET', '/api/projects/{project_id}/brief/references/{reference_id}'): 404,
    ('GET', '/api/teams/{team_id}/brief'): 404,
    ('POST', '/api/teams/{team_id}/brief/pin'): 404,

    ('GET', '/api/teams/{lead}/worklist'): 404,
    ('POST', '/api/teams/{lead}/worklist/items'): 404,
    ('PATCH', '/api/teams/{lead}/worklist/items/{item_id}'): 404,
    ('POST', '/api/teams/{lead}/worklist/items/{item_id}/discuss'): 404,
    ('PUT', '/api/teams/{lead}/worklist/now'): 404,
    ('GET', '/api/teams/{lead}/plan'): 404,
    ('GET', '/api/teams/{lead}/plan/versions/{number}'): 404,
    ('PATCH', '/api/teams/{lead}/plan'): 404,
    ("PUT", "/api/admin-features/{key}"): 403,
    ('POST', '/api/host/backups/{target_id}/remove'): 403,
    ('POST', '/api/group-machines/{binding_id}/verify'): 404,
    ('POST', '/api/group-machines/{binding_id}/approve'): 403,
    ('POST', '/api/group-machines/{binding_id}/revoke'): 404,
    ('GET', '/api/groups/{group_id}/projects/{project_id}/remote-placements'): 404,
    ('POST', '/api/groups/{group_id}/projects/{project_id}/remote-placements'): 404,
    ('POST', '/api/groups/{group_id}/projects/{project_id}/remote-placements/{placement_id}/approve'): 404,
    ('POST', '/api/groups/{group_id}/projects/{project_id}/remote-placements/{placement_id}/revoke'): 404,
    ("POST", "/api/mobile/v1/devices/{id}/rotate"): 404,
    ("POST", "/api/mobile/v1/devices/{id}/revoke"): 404,
    ("POST", "/api/mobile/v1/pairings/{id}/confirm"): 404,
    ("DELETE", "/api/devices/{id}"): 404,
    ("POST", "/api/devices/{id}/renew"): 404,
    ("GET", "/api/devices/pairings/{id}"): 404,
    ("POST", "/api/devices/pairings/{id}/cancel"): 404,
    ("POST", "/api/devices/pairings/{id}/confirm"): 403,
    ("PATCH", "/api/devices/{id}"): 404,
    ("POST", "/api/devices/{id}/revoke"): 404,
    ("GET", "/api/sessions/{acc_id}/terminal-output"): 404,
    ("POST", "/api/telemetry/reports/{report_id}"): 403,
    # Organizations mode is off in this fixture: hidden (test_oidc checks a member's 403 with it on).
    ("DELETE", "/api/organizations/identities/{identity_id}"): 404,
    ("DELETE", "/api/organizations/sso-invites/{invite_id}"): 404,
    ("DELETE", "/api/organizations/oidc/private-hosts/{entry_id}"): 404,
    ("GET", "/api/groups/{group_id}/projects/{project_id}/published/diff"): 404,
    ("POST", "/api/groups/{group_id}/operations/{operation_id}/update"): 404,
    ("GET", "/api/groups/{group_id}/projects/{project_id}/keys"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/keys/{key_id}/attach"): 404,
    ("GET", "/api/groups/{group_id}/board"): 404,
    ("GET", "/api/groups/{group_id}/usage"): 404,
    ("GET", "/api/groups/{group_id}/operations"): 404,
    ("POST", "/api/groups/{group_id}/policy"): 404,
    ("POST", "/api/groups/{group_id}/operations/{operation_id}/resolve"): 404,
    ("GET", "/api/groups/{group_id}/work/{session_id}/context"): 404,
    ("POST", "/api/groups/{group_id}/work/{session_id}/task"): 404,
    ("POST", "/api/groups/{group_id}/work/{session_id}/claim"): 404,
    ("POST", "/api/groups/{group_id}/work/{session_id}/release"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/sessions"): 404,
    ("POST", "/api/dns/credentials/{key_id}/validate"): 403,
    ("POST", "/api/dns/credentials/{key_id}/replace"): 403,
    ("POST", "/api/dns/credentials/{key_id}/remove"): 403,
    ("POST", "/api/dns/credentials/{key_id}/disable"): 403,
    ("GET", "/api/groups/{group_id}/projects/{project_id}/exchange/git/info/refs"): 401,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/exchange/git/git-receive-pack"): 401,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/exchange/git/git-upload-pack"): 401,

    ("POST", "/api/groups/{group_id}/projects/{project_id}/placements/{placement_id}/private-backups"): 404,
    ("GET", "/api/groups/{group_id}/projects/{project_id}/placements/{placement_id}/private-backups/{snapshot}"): 404,

    ("POST", "/api/groups/{group_id}/projects/{project_id}/exchange/grants"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/exchange/transfer"): 401,
    ("GET", "/api/groups/{group_id}/projects/{project_id}/exchange/refs"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/exchange/integrate"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/exchange/tags"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/exchange/publish"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/exchange/checkout"): 404,
    ("GET", "/api/groups/{group_id}/projects/{project_id}/published/file"): 404,
    ("GET", "/api/groups/{group_id}/projects/{project_id}/published/tree"): 404,
    ("GET", "/api/groups/{group_id}/projects/{project_id}/published/wiki"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/exchange/backup"): 404,
    ("GET", "/api/groups/{group_id}/projects/{project_id}/exchange/backups"): 404,
    ("GET", "/api/groups/{group_id}/projects/{project_id}/exchange/backup/{snapshot}"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/exchange/restore"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/placements/{placement_id}/provision"): 403,



    ("POST", "/api/auth/passkeys/{identifier:int}/name"): 404,
    ("POST", "/api/auth/passkeys/{identifier:int}/remove"): 404,
    ("POST", "/api/publish/addresses/{entry_id}/remove"): 403,
    ("POST", "/api/publish/addresses/{entry_id}/default"): 403,
    ("POST", "/api/issue-servers/{server_id}/confirm"): 403,
    ("POST", "/api/issue-servers/{server_id}/remove"): 403,
    ("GET", "/api/groups/{group_id}"): 404,
    ("GET", "/api/groups/{group_id}/people"): 404,
    ("POST", "/api/groups/{group_id}/members/{user_id}"): 404,
    ("POST", "/api/groups/{group_id}/archive"): 403,
    ("POST", "/api/groups/{group_id}/unpause"): 404,
    ("GET", "/api/groups/{group_id}/projects"): 404,
    ("POST", "/api/groups/{group_id}/projects"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/owners"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/archive"): 404,
    ("GET", "/api/groups/{group_id}/projects/{project_id}/placements"): 404,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/placements"): 403,
    ("POST", "/api/groups/{group_id}/projects/{project_id}/approvals"): 404,
    ("POST", "/api/groups/{group_id}/key-grants"): 404,
    ("GET", "/api/groups/{group_id}/audit"): 404,
    ("POST", "/api/groups/{group_id}/key-grants/{grant_id}/revoke"): 404,
    ("PUT", "/api/schedules/{schedule_id}"): 404,
    ("DELETE", "/api/schedules/{schedule_id}"): 404,
    ("PATCH", "/api/sessions/{acc_id}/pull"): 404,
    ("POST", "/api/host/backups/s3-targets"): 403,
    ("POST", "/api/host/backups/{target_id}/credentials"): 403,
    ("POST", "/api/host/backups/{target_id}/disable"): 403,
    ("POST", "/api/host/backups/{target_id}/schedule"): 403,
    ("GET", "/api/host/backups/{target_id}/setup"): 403,
    ("POST", "/api/host/backups/{target_id}/check"): 403,
    ("GET", "/api/host/backups/{target_id}/snapshots"): 403,
    ("POST", "/api/host/backups/{target_id}/verify"): 403,
    ("POST", "/api/host/backups/{target_id}/run"): 403,
    ("POST", "/api/host/backups/{target_id}/prune"): 403,
    ("POST", "/api/host/backups/{target_id}/unlock"): 403,
    ("POST", "/api/host/backups/{target_id}/enable"): 403,
    ("POST", "/api/schedules/{schedule_id}/enable"): 404,
    ("POST", "/api/schedules/{schedule_id}/disable"): 404,
    ("POST", "/api/schedules/{schedule_id}/run"): 404,
    ("POST", "/api/schedules/{schedule_id}/cancel"): 404,
    ("GET", "/api/multiruns/{group_id}"): 404,
    ("GET", "/api/multiruns/{group_id}/compare"): 404,
    ("POST", "/api/multiruns/{group_id}/keep"): 404,
    ("POST", "/api/tools/requests/{request_id}/approve"): 404,
    ("POST", "/api/tools/requests/{request_id}/reject"): 404,
    ("POST", "/api/tools/{tool_id}/remove"): 404,
    ("GET", "/api/projects/{project_id}/preview"): 404,
    ("POST", "/api/projects/{project_id}/preview"): 404,
    ("GET", "/api/files/{kind}/{key}"): 404,
    ("POST", "/api/files/{kind}/{key}"): 404,
    ("POST", "/api/files/{kind}/{key}/upload"): 404,
    ("GET", "/api/files/{kind}/{key}/read"): 404,
    ("GET", "/api/files/{kind}/{key}/download"): 404,
    ("POST", "/api/updates/machines/{name}/follow"): 403,
    ("POST", "/api/updates/machines/{name}"): 403,
    ("GET", "/api/projects/{project_id}/knowledge"): 404,
    ("GET", "/api/projects/{project_id}/knowledge/doc"): 404,
    ("GET", "/api/projects/{project_id}/github"): 404,
    ("GET", "/api/projects/{project_id}/issue-sources"): 404,
    ("POST", "/api/projects/{project_id}/issue-sources"): 404,
    ("GET", "/api/projects/{project_id}/issue-sources/{source}"): 404,
    ("GET", "/api/projects/{project_id}/issue-sources/{source}/{kind}/{item_id}"): 404,
    ("GET", "/api/projects/{project_id}/runtime"): 404,
    ("POST", "/api/projects/{project_id}/runtime"): 403,
    ("POST", "/api/projects/{project_id}/trust"): 404,
    ("POST", "/api/projects/{project_id}/archive"): 404,
    ("GET", "/api/sessions/{acc_id}"): 404,
    ("GET", "/api/sessions/{acc_id}/team"): 404,
    ("POST", "/api/sessions/{acc_id}/team/stop"): 404,
    ("GET", "/api/sessions/{acc_id}/events"): 404,
    ("GET", "/api/sessions/{acc_id}/changes"): 404,
    ("GET", "/api/sessions/{acc_id}/explanation"): 404,
    ("GET", "/api/sessions/{acc_id}/explanation/agents"): 404,
    ("POST", "/api/sessions/{acc_id}/explanation"): 404,
    ("GET", "/api/sessions/{acc_id}/artifacts"): 404,
    ("GET", "/api/sessions/{acc_id}/artifacts/{name:path}"): 404,
    ("GET", "/api/sessions/{acc_id}/export"): 404,
    ("GET", "/api/sessions/{acc_id}/knowledge"): 404,
    ("GET", "/api/sessions/{acc_id}/knowledge/doc"): 404,
    ("POST", "/api/sessions/{acc_id}/stop"): 404,
    ("POST", "/api/sessions/{acc_id}/archive"): 404,
    ("POST", "/api/sessions/{acc_id}/resume"): 404,
    ("POST", "/api/gates/{job_id}/cancel"): 404,
    ("POST", "/api/gates/{job_id}/accept-retry"): 403,
    ("GET", "/api/gates/projects/{project_id}/flakes"): 404,
    ("PUT", "/api/gates/projects/{project_id}/flakes"): 403,
    ("GET", "/api/teams/{lead_id}/tree"): 404,  # orchestrator v2 slice 2
    ("GET", "/api/teams/{lead_id}/events"): 404,
    ("GET", "/api/teams/{lead}/selections"): 404,  # orchestrator v2 slice 3 (the routes exist with ACC_ORCHESTRATOR=1)
    ("POST", "/api/teams/{lead}/attempts/{attempt_id}/fail"): 404,
    ("POST", "/api/teams/{lead}/selections/{selection_id}/override"): 404,
    ("GET", "/api/deliveries/{identifier}"): 404,  # orchestrator v2 slice 4 (404 also while delivery is off)
    ("GET", "/api/sessions/{acc_id}/report"): 404,
    ("GET", "/api/gates/projects/{project_id}/commands"): 403,
    ("PUT", "/api/gates/projects/{project_id}/commands"): 403,
    ("POST", "/api/sessions/{acc_id}/stop-held"): 403,
    ("POST", "/api/sessions/{acc_id}/interrupt"): 404,
    ("POST", "/api/sessions/{acc_id}/message"): 404,
    ("POST", "/api/sessions/{acc_id}/preview-element"): 404,
    ("POST", "/api/sessions/{acc_id}/preview-element/{draft}/send"): 404,
    ("PUT", "/api/sessions/{acc_id}/automation"): 404,
    ("POST", "/api/sessions/{acc_id}/rename"): 404,
    ("POST", "/api/sessions/{acc_id}/promote"): 404,
    ("GET", "/api/sessions/{acc_id}/pull"): 404,
    ("POST", "/api/sessions/{acc_id}/pull"): 404,
    ("GET", "/api/sessions/{acc_id}/pull/draft"): 404,
    ("POST", "/api/sessions/{acc_id}/pull/ready"): 404,
    ("POST", "/api/sessions/{acc_id}/pull/reviewers"): 404,
    ("POST", "/api/sessions/{acc_id}/pull/merge"): 404,
    ("POST", "/api/sessions/{acc_id}/pull/feedback"): 404,
    ("POST", "/api/sessions/{acc_id}/pull/feedback/{preview}/send"): 404,
    ("POST", "/api/workspaces/{folder}/delete"): 404,
    ("POST", "/api/workspaces/{folder}/development"): 403,
    ("GET", "/api/wiki/{kind}/{key}"): 404,
    ("GET", "/api/wiki/{kind}/{key}/page"): 404,
    ("GET", "/api/wiki/{kind}/{key}/attachment"): 404,
    ("POST", "/api/attention/{item_id}/ack"): 404,
    ("POST", "/api/attention/{item_id}/snooze"): 404,
    ("POST", "/api/publish/{request_id}/approve"): 403,
    ("POST", "/api/publish/{request_id}/reject"): 403,
    ("POST", "/api/publish/{request_id}/unpublish"): 404,
    ("POST", "/api/push/devices/{device_id}/remove"): 404,
    ("POST", "/api/users/{user_id}/reset"): 403,
    ("POST", "/api/nodes/{name}/remove"): 403,
    ("POST", "/api/nodes/{name}/policy"): 403,
    ("PUT", "/api/nodes/{name}/wsl-isolation"): 403,
    ("POST", "/api/users/{user_id}/role"): 403,
    ("POST", "/api/users/{user_id}/remove"): 403,
    ("POST", "/api/users/invites/{invite_id}/revoke"): 403,
    ("POST", "/api/catalog/{kind}/{item_id}/remove"): 403,
    ("POST", "/api/catalog/{kind}/{item_id}/profiles"): 403,
    ("POST", "/api/catalog/profiles/{profile_id}"): 403,
    ("POST", "/api/catalog/profiles/{profile_id}/reset"): 403,
    ("GET", "/api/accounts/login/{flow_id}"): 404,
    ("POST", "/api/accounts/login/{flow_id}/code"): 404,
    ("POST", "/api/accounts/login/{flow_id}/cancel"): 404,
    ("POST", "/api/accounts/{provider}/login"): None,  # a member logs in their own account: allowed (checked below)
    ("POST", "/api/auth/sessions/{session_id}/revoke"): 404,  # only your own sign-ins
    ("POST", "/api/projects/{project_id}/ports"): 404,
    ("POST", "/api/ports/{port}/release"): 404,
    ("POST", "/api/endpoints/{endpoint_id}/refresh"): 403,  # model endpoints are the owners' to manage
    ("POST", "/api/endpoints/{endpoint_id}/remove"): 403,
    ("GET", "/api/packages/requests/{request_id}"): 403,  # installing software is the owners' call
    ("POST", "/api/packages/{name}/auto"): 403,
    ("POST", "/api/packages/{name}/{action}"): 403,
    ("GET", "/api/projects/{project_id}/keys"): 404,  # the key vault: others' keys and projects aren't revealed
    ("POST", "/api/vault/keys/{key_id}"): 404,
    ("POST", "/api/vault/keys/{key_id}/replace"): 404,
    ("POST", "/api/vault/keys/{key_id}/remove"): 404,
    ("POST", "/api/vault/keys/{key_id}/give"): 404,
    ("POST", "/api/vault/keys/{key_id}/take"): 404,
    ("POST", "/api/vault/people/{username}/remove"): 403,
    ("GET", "/api/projects/{project_id}/backup"): 404,  # git backup: the project's owner (and owners) only
    ("POST", "/api/projects/{project_id}/backup"): 404,
    ("POST", "/api/projects/{project_id}/backup/run"): 404,
    ("GET", "/api/projects/{project_id}/backup/bundle"): 404,
    ("GET", "/api/git/{repo}/info/refs"): 403,  # backup pushes: a one-push token, never a login (no service: 403)
    ("POST", "/api/git/{repo}/git-receive-pack"): 401,
    ("POST", "/api/git/{repo}/git-upload-pack"): 403,
    ("POST", "/api/git/{repo}/token"): 401,
    ("PUT", "/api/containers/limits/{account}"): 403,  # owners only
    ("POST", "/api/projects/{project_id}/containers"): 403,
}
OWNER_ONLY = [("POST", "/api/providers/image-check"), ("PUT", "/api/gates/wrapup"), ("PUT", "/api/gates/projects/{project_id}/flakes"), ("POST", "/api/gates/{job_id}/accept-retry"), ("GET", "/api/admin-features"),("GET", "/api/remote-access/review"), ("POST", "/api/remote-access/apply"),
              ("POST", "/api/remote-access/off"), ("GET", "/api/remote-access"), ("PUT", "/api/remote-access"),
              ("GET", "/api/remote-access/policy"), ("GET", "/api/devices/pairings"),
              ("GET", "/api/telemetry"), ("GET", "/api/telemetry/preview"),
              ("PUT", "/api/telemetry"), ("POST", "/api/telemetry/delete"), ("GET", "/api/nodes/check"), ("POST", "/api/nodes/check"), ("GET", "/api/dns/providers"), ("GET", "/api/dns/credentials"), ("POST", "/api/dns/credentials"),
              ("GET", "/api/dns/binding"), ("POST", "/api/dns/binding"),
              ("POST", "/api/dns/adopt"), ("POST", "/api/dns/publishing-source"),
              ("POST", "/api/dns/apply"), ("POST", "/api/dns/rollback"), ("POST", "/api/dns/migrate"),
              ("POST", "/api/issue-servers"), ("POST", "/api/issue-servers/resolve"),
              ("GET", "/api/audit"), ("GET", "/api/users"), ("GET", "/api/observations"),
              ("POST", "/api/github/repos"), ("POST", "/api/sessions/external"), ("POST", "/api/catalog/mcps"),
              ("POST", "/api/catalog/skills"), ("POST", "/api/catalog/preview"), ("POST", "/api/users/invites"),
              ("POST", "/api/endpoints"), ("GET", "/api/packages"), ("GET", "/api/certificate"), ("GET", "/api/vault/people"),
              ("GET", "/api/tools/policy"), ("POST", "/api/groups"), ("PUT", "/api/updates/config"), ("PUT", "/api/tools/policy"),
              ("PUT", "/api/guardrails"), ("GET", "/api/containers")]


@pytest.fixture
def env(env, tmp_path):
    # The Guide is source data, but its git/container sidecars must also stay in the test world.
    import shutil
    from dataclasses import replace
    from pathlib import Path

    source = Path(__file__).resolve().parents[1]
    guide = tmp_path / 'guide-source'
    for relative in ('docs/wiki/.order', 'docs/wiki/Home.md', 'docs/wiki/User-Guide.md',
                     'docs/wiki/User-Guide/Features.md', 'docs/wiki/.attachments/session-page.png'):
        target = guide / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    env['settings'] = replace(env['settings'], guide_dir=guide)
    return env


@pytest.fixture
async def two(api, env, project, member_accounts):
    """The owner's project, session, inbox item, publish request and device; a member signed in separately."""
    await do_setup(api, env)
    owner = api  # still holds the bearer token: an owner
    session = (await owner.post("/api/sessions", json={"project_id": project["id"], "provider": "claude", "name": "mine"})).json()
    await wait_for(lambda: _idle(owner, session["id"]))
    intent = WRITE_INTENT.set(True)  # like an API write: wait for the supervisor's commits instead of failing
    with write_db(api.app.state.db) as s:
        item = AttentionItem(dedupe_key="test:x", kind="needs_input", title="owner's item", source="test",
                             session_id=session["id"], owner=session["owner"])
        request = PublishRequest(id="pub-ownertest01", action="publish", host="x.example.com", port=8100, access="public",
                                 requested_by="@api", owner=session["owner"])
        device = PushSubscription(endpoint="https://fcm.googleapis.com/x", p256dh="k", auth="a", username="casey", label="phone")
        schedule = Schedule(id="schedule-owner", owner="casey", account=api.app.state.ctx.settings.default_account,
                            launch={"project_id": project["id"], "provider": "claude", "name": "Review"},
                            timing={"kind": "daily", "time": "09:00"}, timezone="UTC", enabled=False)
        browser_device = Device(id='d' * 32, person='casey', label='Owner browser', kind='browser', status='active',
                                certificate_pem='not read by authorization checks', fingerprint='1' * 64,
                                serial='123', expires_at=auth.now() + timedelta(days=1))
        pairing = DevicePairing(id='b' * 32, person='casey', label='Owner browser', token_hash='2' * 64,
                                expires_at=auth.now() + timedelta(minutes=5))
        s.add_all([item, request, device, schedule, browser_device, pairing])
        s.commit()
        ids = {"item": item.id, "device": device.id, "schedule": schedule.id, "browser_device": browser_device.id, "pairing": pairing.id}
    WRITE_INTENT.reset(intent)
    link = (await owner.post("/api/users/invites", json={"role": "member", "account": "dana-agent"})).json()["link"]
    member = browser(api)
    assert (await join(member, link, "alex")).status_code == 200
    yield {"owner": owner, "member": member, "project": project, "session": session, **ids}
    await member.aclose()


async def _idle(api, acc_id):
    return (await api.get(f"/api/sessions/{acc_id}")).json()["observed_state"] == "idle"


def fill(path: str, w: dict) -> str:
    return (path.replace("{team_id}", w["session"]["id"]).replace("{reference_id}", "REF-1").replace("{lead}", w["session"]["id"]).replace("{number}", "1").replace("{binding_id}", "a" * 32).replace("{report_id}", "not-your-report").replace("{id}", w["pairing"] if "/pairings/" in path else w["browser_device"]).replace("{identifier:int}", "1").replace("{entry_id}", "example-address").replace("{server_id}", "a" * 16).replace("{schedule_id}", w["schedule"]).replace("{project_id}", str(w["project"]["id"])).replace("{acc_id}", w["session"]["id"])
            .replace("{target_id}", "a" * 32).replace("{folder}", "nothing-here").replace("{kind}", "project").replace("{key}", str(w["project"]["id"]))
            .replace("{item_id}", str(w["item"])).replace("{request_id}", "pub-ownertest01")
            .replace("{device_id}", str(w["device"])).replace("{user_id}", "1").replace("{invite_id}", "1")
            .replace("{profile_id}", "frontend").replace("{flow_id}", "agentx.flow").replace("{provider}", "claude")
            .replace("{session_id}", "0123456789abcdef").replace("{port}", "8000").replace("{endpoint_id}", "local")
            .replace("{request_id}", "pkg-000000000000").replace("{name}", "codex").replace("{action}", "install")
            .replace("{operation_id}", "missing-operation").replace("{snapshot}", "a" * 32).replace("{placement_id}", "1").replace("{grant_id}", "1").replace("{group_id}", "not-your-group").replace("{source}", "gitlab").replace("{repo}", f"{w['project']['id']}.git").replace("{preview}", "nothing"))


BODIES = {'/verify': {'code': 'ABCDEFGH23'},'/remote-placements': {'binding_id': 'a' * 32, 'revision': 1}, "d" * 32: {"label": "Taken"}, "/update": {"revision": 1, "action": "resolve"}, "policy": {"role": "workstation", "revision": 1, "concurrent": 4, "daily_launches": 50},
    "/task": {"task": "Review tests"}, "/claim": {"path": "src/"}, "/release": {"path": "src/"},
    "/sessions": {"checkout_id": "a" * 12, "task": "Review tests", "name": "Demo", "provider": "claude"},
    "containers": {"enabled": False},
    '/private-backups': {'session_id': 'a' * 12, 'commit': 'a' * 40},
    '/exchange/grants': {'operation': 'fetch', 'session_id': 'a' * 12, 'old_sha': '0' * 40, 'new_sha': 'a' * 40},
    '/exchange/transfer': {},
    '/exchange/integrate': {'approval_id': 1},
    '/exchange/tags': {'name': 'v1', 'commit': 'a' * 40},
    '/exchange/publish': {'placement_id': 1, 'session_id': 'a' * 12, 'old_sha': '0' * 40, 'new_sha': 'a' * 40},
    '/exchange/checkout': {'placement_id': 1, 'session_id': 'a' * 12, 'commit': 'a' * 40},
    '/exchange/restore': {'snapshot': 'a' * 32, 'commit': 'a' * 40},
"schedule-owner": {"launch": {"project_id": 1, "provider": "claude", "name": "Review"},
                              "timing": {"kind": "daily", "time": "09:00"}, "timezone": "UTC"},
          "keep": {"session_id": "not-your-session"}, "explanation": {"provider": "claude", "model": "sonnet"}, "preview": {"port": 8000},
          "preview-element": {"port": 8000, "selector": "body:nth-of-type(1)", "text": "x", "box": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}},
          "name": {"name": "taken"}, "rename": {"name": "taken"}, "message": {"text": "hello"}, "promote": {"name": "X", "slug": "x", "github": False}, "delete": {"confirm": "nothing-here"},
          "role": {"role": "member"}, "runtime": {"compose_autostart": True}, "code": {"code": "x"},
          "profiles": {"profiles": []}, "frontend": {"name": "F"},
          "replace": {"value": "x"}, "backup": {"enabled": False}, "give": {"project_id": 1}, "take": {"project_id": 1},
          "pull": {"title": "x"}, "ready": {"ready": True}, "reviewers": {"reviewers": ["x"]}, "feedback": {"kind": "checks"},
          "merge": {"method": "squash", "sha": "a" * 40, "confirm": True},
          "discuss": {"revision": 1, "text": "Question about this decision"},
          "/override": {"choice": 0, "reason": "x"}}


async def test_member_cannot_reach_anything_of_the_owners(two):
    member = two["member"]
    for (method, path), expected in MATRIX.items():
        if expected is None:
            continue
        url = fill(path, two)
        query = {"path": "README.md"} if path.endswith(("/doc", "/page", "/attachment")) else None
        if path.endswith("/compare"):
            query = {"left": "a", "right": "b"}
        body = next((b for k, b in BODIES.items() if url.endswith(k)), {})
        if url.startswith('/api/groups/'):
            body = {"revision": 1, "role": "member", "name": "Demo", "slug": "demo-group", "owners": [1],
                    "user_id": 1, "key_id": "missing", "branch": "main", "old_sha": "a" * 40, "new_sha": "b" * 40, **body}
        response = await (member.get(url, params=query) if method == "GET"
                          else member.request(method, url, params=query, json=body, headers=csrf(member)))
        assert response.status_code == expected, (method, path, response.status_code, response.text[:200])
    for method, path in OWNER_ONLY:
        response = await (member.get(path) if method == "GET" else member.request(method, path, json={}, headers=csrf(member)))
        assert response.status_code in (403, 422), (path, response.status_code)  # 422: a body check ran first
        if response.status_code == 422:  # make sure it is still refused with a valid-looking body
            response = await member.request(method, path, json={"name": "x", "confirm": True, "id": "x", "command": "x", "url": "https://github.com/o/r",
                                                       "content": "---\nname: x\n---\nhello there", "role": "member", "account": "agent",
                                                       "project_id": 1, "provider": "claude", "native_id": "x", "run_as": "root", "unit": "x"},
                                         headers=csrf(member))
            assert response.status_code == 403, (path, response.status_code, response.text[:200])


async def test_member_lists_show_only_their_own_and_owners_see_everything(two, env):
    member, owner = two["member"], two["owner"]
    for path in ("/api/projects", "/api/sessions", "/api/attention", "/api/publish", "/api/workspaces", "/api/schedules"):
        assert (await member.get(path)).json() == [], path
    assert (await member.get("/api/push")).json()["devices"] == []
    assert (await member.get("/api/host")).status_code == 200  # the machine's health is everyone's to see
    (env["workspaces"] / "pm").mkdir()
    mine = await member.post("/api/projects", json={"slug": "pm", "name": "PM", "workspace_path": str(env["workspaces"] / "pm")},
                             headers=csrf(member))
    assert mine.status_code == 201 and mine.json()["account"] == "dana-agent"
    assert [p["slug"] for p in (await member.get("/api/projects")).json()] == ["pm"]
    assert {"demo", "pm"} <= {p["slug"] for p in (await owner.get("/api/projects")).json()}  # owners see all
    accounts = (await member.get("/api/accounts")).json()
    assert accounts["account"] == "dana-agent" and accounts["accounts"] == ["dana-agent"]


async def test_member_publishing_waits_for_an_owner(two, env):
    member = two["member"]
    (env["workspaces"] / "site").mkdir()
    project = (await member.post("/api/projects", json={"slug": "site", "name": "Site", "workspace_path": str(env["workspaces"] / "site")},
                                 headers=csrf(member))).json()
    port = (await member.post(f"/api/projects/{project['id']}/ports", json={"range": "prod"}, headers=csrf(member))).json()["port"]
    asked = await member.post("/api/publish", json={"host": "site.example.com", "port": port, "access": "internal",
                                                     "project_id": project["id"]}, headers=csrf(member))
    assert asked.status_code == 201 and asked.json()["status"] == "pending_approval"
    assert (await member.post(f"/api/publish/{asked.json()['id']}/approve", headers=csrf(member))).status_code == 403
    titles = [i["title"] for i in (await two["owner"].get("/api/attention")).json()]
    assert any("site.example.com" in t for t in titles)  # the owner is asked
    assert not any("site.example.com" in i["title"] for i in (await member.get("/api/attention")).json())


def all_routes(routes, prefix=""):
    """Every route, including those inside included routers (newer FastAPI wraps them)."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from all_routes(inner.routes, prefix + getattr(route, "prefix", ""))
        elif isinstance(route, Mount):
            yield from all_routes(route.routes, prefix + route.path)
        else:
            yield prefix, route


def test_every_resource_route_is_covered(api):
    flat = list(all_routes(api.app.routes))
    assert len(flat) > 60  # sanity: the walk found the routers' routes
    routes = [(m, prefix + r.path) for prefix, r in flat if isinstance(r, APIRoute) and "{" in prefix + r.path
              for m in r.methods if m in ("GET", "POST", "PUT", "PATCH", "DELETE")]
    missing = [(m, p) for m, p in routes if (m, p) not in MATRIX and not p.startswith("/api/auth/invite")
               and p not in ("/{path:path}", "/api/{path:path}")]
    assert not missing, f"add these to MATRIX: {json.dumps(missing)}"
    # Extend the same MATRIX inventory to fixed API paths and every WebSocket.
    from pathlib import Path

    from scripts.generate_organizations_authz import render

    root = Path(__file__).resolve().parents[1]
    records = json.loads((root / 'tests/ownership_inventory.json').read_text())
    classified = {(r['method'], r['path']): r for r in records}
    assert len(classified) == len(records), 'duplicate classifications'
    actual = set()
    for prefix, route in all_routes(api.app.routes):
        path = prefix + getattr(route, 'path', '')
        if not (path == '/api' or path.startswith('/api/') or 'WebSocket' in type(route).__name__):
            continue
        for method in getattr(route, 'methods', {'WS'}):
            actual.add((method, path))
    normal = {key for key, record in classified.items() if not record.get("configuration")}
    assert actual == normal, f'classify additions/removals: {actual ^ normal}'
    assert all(record.get("configuration") in (None, "ha", "desktop") for record in records)
    for key, record in classified.items():
        assert all(record.get(field) for field in ('principal', 'ownership', 'audit', 'source'))
        if key in MATRIX:
            assert record['other_member_status'] == MATRIX[key], key
        if key in OWNER_ONLY:
            # Device policy and DNS metadata deliberately require a human owner, unlike ctx.owner.
            human_owner = key in {
                ('GET', '/api/dns/providers'),
                ('GET', '/api/remote-access'), ('PUT', '/api/remote-access'),
                ('GET', '/api/remote-access/policy'), ('GET', '/api/devices/pairings'),
                ('GET', '/api/remote-access/review'), ('POST', '/api/remote-access/apply'), ('POST', '/api/remote-access/off'),
            }
            expected = ('signed-in human owner (no installation API token)' if human_owner
                        else 'owner / installation API token')
            assert record['principal'] == expected, key
    assert (root / 'docs/ORGANIZATIONS-AUTHZ.md').read_text() == render(records)


async def test_member_cannot_open_the_owners_terminal(two, live):  # noqa: F811 (fixture)
    import websockets
    url = f"{live}/api/sessions/{two['session']['id']}/tty"
    cookie = {"Cookie": f"acc_session={two['member'].cookies.get('acc_session')}", "Origin": "http://127.0.0.1:18740"}
    with pytest.raises(websockets.InvalidStatus) as refused:
        async with websockets.connect(url, additional_headers=cookie):
            pass
    assert refused.value.response.status_code == 403


async def test_port_registry_and_members_publish_only_their_reserved_ports(two, env):
    member, owner = two["member"], two["owner"]
    (env["workspaces"] / "app").mkdir()
    project = (await member.post("/api/projects", json={"slug": "app", "name": "App", "workspace_path": str(env["workspaces"] / "app")},
                                 headers=csrf(member))).json()
    taken = await owner.post("/api/ports", json={"port": 8100, "label": "example-service (outside Rynhult)"})
    assert taken.status_code == 201
    reserved = (await member.post(f"/api/projects/{project['id']}/ports", json={"range": "prod"}, headers=csrf(member))).json()
    assert reserved["port"] == 8101 and reserved["mine"]  # 8100 was taken
    clash = await member.post(f"/api/projects/{project['id']}/ports", json={"port": 8100}, headers=csrf(member))
    assert clash.status_code == 409
    listing = (await member.get("/api/ports")).json()
    assert {(p["port"], p["project"]) for p in listing} == {(8100, None), (8101, "app")}  # others' not named
    refused = await member.post("/api/publish", json={"host": "app.example.com", "port": 8100, "access": "internal",
                                                       "project_id": project["id"]}, headers=csrf(member))
    assert refused.status_code == 409 and "reserve" in refused.text
    ok = await member.post("/api/publish", json={"host": "app.example.com", "port": 8101, "access": "internal",
                                                  "project_id": project["id"]}, headers=csrf(member))
    assert ok.status_code == 201 and ok.json()["status"] == "pending_approval"
    assert (await member.post("/api/ports/8100/release", headers=csrf(member))).status_code == 404
    assert (await member.post("/api/ports", json={"port": 9000, "label": "x"}, headers=csrf(member))).status_code == 403


async def test_inbox_is_personal_and_unlinked_accounts_raise_nothing(two):
    from acc.server import attention
    owner, member = two["owner"], two["member"]
    with write_db(owner.app.state.db) as s:
        s.add(AttentionItem(dedupe_key="test:alex", kind="needs_input", title="alex's own alert", source="test", owner="alex"))
        attention.evaluate_auth(s, {"claude": {"logged_in": False}, "codex": {"logged_in": False}}, "nobody-agent", key_suffix=":nobody-agent")
        s.commit()
    owner_titles = [i["title"] for i in (await owner.get("/api/attention")).json()]
    assert "alex's own alert" not in owner_titles and "owner's item" in owner_titles  # owners: their own + host-wide
    assert not any("nobody-agent" in t for t in owner_titles)  # an account nobody uses yet raises nothing
    assert [i["title"] for i in (await member.get("/api/attention")).json()] == ["alex's own alert"]


async def test_everyone_reads_the_guide_and_nothing_outside_it(two):
    member = two["member"]
    tree = (await member.get("/api/wiki/guide/acc")).json()
    assert [n["title"] for n in tree["pages"]][:2] == ["Home", "User Guide"], tree
    page = await member.get("/api/wiki/guide/acc/page", params={"path": "User-Guide/Features.md"})
    assert page.status_code == 200 and "Every feature at a glance" in page.text
    image = await member.get("/api/wiki/guide/acc/attachment", params={"path": ".attachments/session-page.png"})
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    for path in ("../../../etc/passwd.md", "../../pyproject.toml"):
        assert (await member.get("/api/wiki/guide/acc/page", params={"path": path})).status_code == 404
    assert (await member.get("/api/wiki/guide/other")).status_code == 404


def test_inventory_walk_includes_mounted_api_routes_and_websockets():
    """A future mounted API must not hide its routes from the classification gate."""
    from starlette.routing import Mount, Route, WebSocketRoute

    async def endpoint(scope):
        pass

    mounted = Mount('/api/extension', routes=[Route('/items', endpoint), WebSocketRoute('/events', endpoint)])
    found = {(prefix + route.path, type(route).__name__) for prefix, route in all_routes([mounted])}
    assert found == {('/api/extension/items', 'Route'), ('/api/extension/events', 'WebSocketRoute')}


async def test_member_cannot_request_provider_image_qualification(two):
    member = two['member']
    response = await member.post('/api/providers/image-check', headers=csrf(member), json={
        'project_id':two['project']['id'], 'provider':'claude', 'model':'fable'})
    assert response.status_code == 403


async def test_publishing_current_and_audit_views_keep_member_scope(two, api):
    with write_db(api.app.state.db) as db:
        for status in ('failed', 'applied'):
            for owner in ('alex', 'dana'):
                db.add(PublishRequest(id=f'view-{owner}-{status}', owner=owner, requested_by=owner,
                                      host=f'{owner}.example.com', action='publish', status=status,
                                      port=8004, access='internal', context={'target_ip': '192.0.2.10'}))
        db.commit()
    for view in ('current', 'audit'):
        response = (await two['member'].get('/api/publish', params={'view': view})).json()
        rows = response['items'] if view == 'audit' else response
        assert rows and all(row['requested_by'] == 'alex' for row in rows)
        assert all('target_ip' not in row for row in rows)
        if view == 'audit':
            assert response['total'] == 1
            assert (await two['member'].get('/api/publish?view=audit&host=dana')).json()['total'] == 0


@pytest.mark.usefixtures('world')
async def test_member_publication_outcomes_stay_private_through_current_and_audit(two, api, env):
    from test_publish import status_is
    member = two['member']
    (env['workspaces'] / 'member-pub-safe').mkdir()
    project = (await member.post('/api/projects', json={'slug':'member-pub-safe','name':'Member site',
        'workspace_path':str(env['workspaces'] / 'member-pub-safe')}, headers=csrf(member))).json()
    port = (await member.post(f"/api/projects/{project['id']}/ports", json={'range':'prod'}, headers=csrf(member))).json()['port']
    request = (await member.post('/api/publish', json={'host':'member.example.com','port':port,
        'access':'internal','project_id':project['id']}, headers=csrf(member))).json()
    assert (await api.post(f"/api/publish/{request['id']}/approve")).status_code == 200
    owner_row = await wait_for(lambda: status_is(api, request['id'], 'applied'))
    address = owner_row['target_ip']
    assert address and owner_row['outcome']['dns']['target_ip'] == address
    assert address not in owner_row['detail']
    current = (await member.get('/api/publish?view=current')).json()
    assert address not in json.dumps(current) and current[0]['outcome']['https'] == {'checked':True}
    removed = (await api.post(f"/api/publish/{request['id']}/unpublish")).json()
    await wait_for(lambda: status_is(api, removed['id'], 'removed'))
    for path in ('/api/publish', '/api/publish?view=audit'):
        response = (await member.get(path)).json()
        assert address not in json.dumps(response)
    # Old stored success details must be scrubbed too, without rewriting history in the database.
    with write_db(api.app.state.db) as db:
        db.get(PublishRequest, request['id']).detail = f'DNS member.example.com → {address} (DNS-only); HTTPS OK'
        db.commit()
    assert address not in json.dumps((await member.get('/api/publish?view=audit')).json())
    assert address in json.dumps((await api.get('/api/publish?view=audit')).json())
