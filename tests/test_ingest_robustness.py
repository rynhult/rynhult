"""Spools live in folders the agent user controls: bad lines, links and special files never stall ingestion."""
from __future__ import annotations

import json
import os

from helpers import wait_for


async def external(api, project, env, name, run_as="casey"):
    sid = "aaaaaaaa-bbbb-cccc-dddd-" + os.urandom(6).hex()
    session = (await api.post("/api/sessions/external", json={
        "project_id": project["id"], "provider": "claude", "name": name, "native_id": sid,
        "run_as": run_as, "unit": "x.service"})).json()
    run_dir = env["run_dir"] / session["id"]
    run_dir.mkdir()
    return session, run_dir, sid


def line(sid, event="SessionStart", **fields):
    return json.dumps({"provider": "claude", "hook": {"hook_event_name": event, "session_id": sid, "source": "resume", **fields}}) + "\n"


async def state(api, acc_id):
    return (await api.get(f"/api/sessions/{acc_id}")).json()["observed_state"]


async def test_poison_lines_are_recorded_and_skipped(api, project, env):
    session, run_dir, sid = await external(api, project, env, "poisoned")
    healthy, healthy_dir, healthy_sid = await external(api, project, env, "healthy")
    poison = ["[]\n", "1\n", '"x"\n', '{"hook": "x"}\n', '{"hook": {"hook_event_name": ["list"]}}\n',
              "[" * 100000 + "\n", '{"hook": {"hook_event_name": "SessionStart", "session_id": {"a": 1}}}\n',
              '{"hook": {"hook_event_name": "SessionStart", "session_id": "../../etc"}}\n', "{bad json\n"]
    (run_dir / "events.jsonl").write_text("".join(poison) + line(sid))
    (healthy_dir / "events.jsonl").write_text(line(healthy_sid))

    async def both_idle():
        return await state(api, session["id"]) == "idle" and await state(api, healthy["id"]) == "idle"
    assert await wait_for(both_idle)  # the good line after the poison still lands, and the other session too
    events = (await api.get(f"/api/sessions/{session['id']}/events")).json()
    assert sum(e["kind"] == "spool_parse_error" for e in events) >= 6
    detail = (await api.get(f"/api/sessions/{session['id']}")).json()
    assert detail["native_id"] == sid  # the path-like id was never stored


async def test_links_and_fifos_in_run_folders_are_ignored(api, project, env, tmp_path):
    linked, linked_dir, _ = await external(api, project, env, "linked")
    piped, piped_dir, _ = await external(api, project, env, "piped")
    healthy, healthy_dir, healthy_sid = await external(api, project, env, "healthy2")
    secret = tmp_path / "acc-only.txt"
    secret.write_text(line("bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee", "Notification", message="SECRET"))
    (linked_dir / "events.jsonl").symlink_to(secret)
    (linked_dir / "statusline.json").symlink_to("/dev/zero")
    os.mkfifo(piped_dir / "events.jsonl")
    os.mkfifo(piped_dir / "remote.json")
    (healthy_dir / "events.jsonl").write_text(line(healthy_sid))
    assert await wait_for(lambda: _is(api, healthy["id"], "idle"))  # the supervisor never hung on the FIFO
    events = (await api.get(f"/api/sessions/{linked['id']}/events")).json()
    assert not any("SECRET" in json.dumps(e) for e in events)


async def _is(api, acc_id, observed):
    return await state(api, acc_id) == observed
