"""deploy/containers/acc-containers: the trusted container launcher, with fakes for systemd-run, systemctl and podman.

No real systemd or container commands run, even when pytest runs as root: each command is a fake script that
records its arguments, and every folder is under a temporary root."""
from __future__ import annotations

import errno
import grp
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import pwd
import shlex
import shutil
import socket
import stat
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

HELPER = Path(__file__).resolve().parents[1] / "deploy/containers/acc-containers"
AGENT_UID = 1004
BROKER_PID = 4242
IMAGE = "sha256:" + "ab" * 32
MEMBER = {"cpus": 2, "memory_mib": 4096, "pids": 512, "disk_gib": 20, "sessions": 4}
UNLIMITED = {"cpus": None, "memory_mib": None, "pids": None, "disk_gib": None, "sessions": None}

FAKE = """#!/bin/sh
# {name}: records its arguments; answers from files in FAKE_DIR (baked in: the helper runs commands in a clean env)
FAKE_DIR={fakes}
printf '%s\\n' "{name} $*" >> "$FAKE_DIR/calls"
case "{name}" in
  systemctl)
    case "$*" in
      *MainPID*) cat "$FAKE_DIR/mainpid" ;;
      *ActiveState*) cat "$FAKE_DIR/active" 2>/dev/null || echo active ;;
      list-units*) cat "$FAKE_DIR/units" 2>/dev/null ;;
      stop*) [ -f "$FAKE_DIR/stuck" ] || echo inactive > "$FAKE_DIR/active" ;;
    esac ;;
  systemd-run) exit "$(cat "$FAKE_DIR/run_rc" 2>/dev/null || echo 0)" ;;
  journalctl) cat "$FAKE_DIR/journal" 2>/dev/null ;;
  runuser)
    case "$*" in
      *"image inspect"*) [ -f "$FAKE_DIR/inspect_rc" ] && {{ cat "$FAKE_DIR/inspect_err" >&2; exit "$(cat "$FAKE_DIR/inspect_rc")"; }}
                         # Rootless Podman 4.9.3 (rootless_test below): without its alive file it refreshes, and the new
                         # pause process loses rename_noreplace to an existing pause.pid, whose PID it must then join.
                         if [ -f "$FAKE_DIR/rootless" ]; then
                           for arg in "$@"; do case "$arg" in XDG_RUNTIME_DIR=*) tmpd="${{arg#XDG_RUNTIME_DIR=}}/libpod/tmp" ;; esac; done
                           if [ ! -f "$tmpd/alive" ] && [ -f "$tmpd/pause.pid" ] && [ ! -d "$FAKE_DIR/../proc/$(cat "$tmpd/pause.pid")" ]; then
                             echo "Error: cannot re-exec process to join the existing user namespace" >&2; exit 125
                           fi
                         fi
                         cat "$FAKE_DIR/image" ;;
      *"container inspect"*) [ -f "$FAKE_DIR/label" ] && cat "$FAKE_DIR/label" && exit 0; exit 125 ;;
      *"container exists"*) {{ [ -f "$FAKE_DIR/stuck" ] || [ -f "$FAKE_DIR/label" ]; }} && exit 0; exit 1 ;;
      *" rm "*) [ -f "$FAKE_DIR/stuck" ] || rm -f "$FAKE_DIR/label" ;;
    esac ;;
esac
exit 0
"""


@pytest.fixture(params=[False, True], ids=["runner", "root-owned-run-files"])
def env(tmp_path, monkeypatch, request):
    # Files retain the real runner's ownership even when getuid/geteuid are simulated as root.
    runner_uid = tmp_path.stat().st_uid
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    for name in ("systemctl", "systemd-run", "runuser", "podman", "journalctl"):
        script = fakes / name
        script.write_text(FAKE.format(name=name, fakes=fakes))
        script.chmod(0o755)
    (fakes / "mainpid").write_text(f"{BROKER_PID}\n")
    root = tmp_path / "containers"
    for folder, mode in (("grants", 0o750), ("state", 0o700), ("audit", 0o750)):
        (root / folder).mkdir(parents=True)
        (root / folder).chmod(mode)
    (root / 'acc-container-exec').write_text('trusted execution guard')
    (root / 'acc-container-exec').chmod(0o644)
    home = tmp_path / "home/alex-agent"
    home.mkdir(parents=True)
    ws = tmp_path / "ws"
    checkout = ws / ".worktrees/demo-1a2b3c4d"
    (checkout / ".git").mkdir(parents=True)
    run_root = tmp_path / "run/alex-agent"
    run_dir = run_root / "s1a2b3c4d5"
    run_dir.mkdir(parents=True)
    broker_env = tmp_path / "broker-env"
    broker_env.mkdir()
    settings = broker_env / "alex-agent.env"
    settings.write_text(f"ACC_BROKER_ROLE=agents\nACC_ALLOWED_ROOTS={ws}\nACC_RUN_DIR={run_root}\n")
    settings.chmod(0o644)
    store = tmp_path / 'images'
    layer = store / 'overlay/layer/diff'
    layer.mkdir(parents=True, exist_ok=True)
    rootfs = store / 'rootfs' / IMAGE.removeprefix('sha256:')
    (rootfs / 'usr/local/bin').mkdir(parents=True)
    (rootfs / 'usr/local/bin/acc-session').write_text('reviewed entrypoint')
    (rootfs / '.acc-image-id').write_text(IMAGE + '\n')
    (fakes / 'image').write_text(json.dumps([{'Id': IMAGE, 'GraphDriver': {'Name': 'overlay', 'Data': {'UpperDir': str(layer)}}}]))
    (tmp_path / "storage.conf").write_text('[storage]\ndriver = "overlay"\n[storage.options]\nadditionalimagestores = ' + json.dumps([str(store)]) + '\n')
    (tmp_path / "storage.conf").chmod(0o644)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"version": 1, "images": {"default": {
        "image": IMAGE, "network": "none", "providers": {"claude": "/usr/local/bin/claude"}}}}))
    policy.chmod(0o644)
    (tmp_path / "subuid").write_text("alex-agent:200000:65536\nother-agent:300000:65536\n")
    (tmp_path / "subgid").write_text("alex-agent:200000:65536\nother-agent:300000:65536\n")
    for key, value in {
        "ACC_CONTAINERS_IMAGE_STORE": store, "ACC_CONTAINERS_ROOT": root, "ACC_CONTAINERS_POLICY": policy, "ACC_BROKER_ENV_DIR": broker_env,
        "ACC_CONTAINERS_CONTROL_USER": "acc", "ACC_CONTAINERS_STORAGE_CONFIG": tmp_path / "storage.conf", "ACC_SUBUID": tmp_path / "subuid", "ACC_SUBGID": tmp_path / "subgid",
        "ACC_SYSTEMD_RUN": fakes / "systemd-run", "ACC_SYSTEMCTL": fakes / "systemctl", "ACC_JOURNALCTL": fakes / "journalctl",
        "ACC_PODMAN": "/usr/bin/podman", "ACC_RUNUSER": fakes / "runuser", "ACC_CONTAINERS_ROOT_UID": runner_uid,
        "ACC_CONTAINERS_TMPFS_TARGETS": f"{run_root}:{ws}",  # the image's /srv/acc/run and /srv/acc/workspaces here
        "ACC_CONTAINERS_ATTACH_ROOT": tmp_path / "attach",  # /run/acc-ct-attach
        "ACC_USER_RUNTIME": tmp_path / "run-user",  # never the real /run/user: AGENT_UID may be the runner's own uid
        "FAKE_DIR": fakes,
    }.items():
        monkeypatch.setenv(key, str(value))
    loader = importlib.machinery.SourceFileLoader("acc_containers", str(HELPER))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("acc_containers", loader))
    loader.exec_module(module)
    # Keep real parent-mode checks and Podman's <=50-character runroot bound, never probe /run.
    short = tempfile.TemporaryDirectory(prefix='ct-')
    request.addfinalizer(short.cleanup)
    module.STAGING_PARENT = Path(short.name) / 'staging'
    module.RUNTIME_PARENT = Path(short.name) / 'r'
    # Model agent-owned mount objects and run inputs as uid 1004. Keep real types, modes and identities,
    # and protected grant/policy/settings ownership, so the security checks remain meaningful on any runner uid.
    agent_dirs = {(info.st_dev, info.st_ino) for path in (checkout, checkout / ".git", run_dir)
                  for info in [path.stat()]}

    def agent_owner(info):
        # Inputs created during a test belong to the session account too. Identify them by
        # inode so descriptor checks retain real link counts, types and permissions.
        run_objects = {(item.st_dev, item.st_ino) for path in run_dir.rglob('*')
                       for item in [path.lstat()]}
        # Exercise the root-owned temporary-file case even on an unprivileged runner.
        if request.param and (info.st_dev, info.st_ino) in run_objects:
            info = SimpleNamespace(**{name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                                   | {"st_uid": 0})
        if (info.st_dev, info.st_ino) not in agent_dirs | run_objects:
            return info
        return SimpleNamespace(**{name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                               | {"st_uid": AGENT_UID})

    module.os = SimpleNamespace(**(vars(os) | {
        "getuid": (lambda: 0) if request.param else os.getuid,
        "geteuid": (lambda: 0) if request.param else os.geteuid,
        "fstat": lambda fd: agent_owner(os.fstat(fd)),
        "stat": lambda *args, **kwargs: agent_owner(os.stat(*args, **kwargs)),
    }))
    real_peer = module.peer
    module.peer = lambda sock: (BROKER_PID, AGENT_UID)
    (tmp_path / "proc").mkdir()
    module.PROC = tmp_path / "proc"  # no real process is ever scanned or signalled; tests add fake ones here
    users = {"alex-agent": (AGENT_UID, str(home)), "other-agent": (AGENT_UID + 1, str(tmp_path / "home/other-agent")),
             "acc": (runner_uid, "/srv/acc/data"), "root-agent": (0, "/root")}

    def getpwnam(name):
        if name not in users:
            raise KeyError(name)
        uid, home_ = users[name]
        return pwd.struct_passwd((name, "x", uid, uid, "", home_, "/bin/sh"))
    module.pwd = SimpleNamespace(getpwnam=getpwnam, struct_passwd=pwd.struct_passwd)
    groups = {}
    module.grp = SimpleNamespace(getgrall=lambda: list(groups.values()),
                                 getgrgid=lambda gid: SimpleNamespace(gr_name="alex-agent", gr_mem=[]),
                                 getgrnam=grp.getgrnam)
    return SimpleNamespace(m=module, root=root, fakes=fakes, checkout=checkout, run_dir=run_dir, ws=ws, home=home,
                           policy=policy, settings=settings, groups=groups, users=users, tmp=tmp_path, runner_uid=runner_uid, real_peer=real_peer)


def grant(env, **overrides) -> dict:
    (env.run_dir / 'settings.json').write_text('{}')
    (env.run_dir / 'events.jsonl').touch()
    value = {"version": 1, "session_id": "s1a2b3c4d5", "generation": 1, "policy_id": "default",
             "account": "alex-agent", "provider": "claude", "args": ["--session-id", "abc"],
             "checkout": str(env.checkout), "run_dir": str(env.run_dir), "guard_files": "settings.json=" + hashlib.sha256(b"{}").hexdigest(),
             "limits": dict(MEMBER)}
    value.update(overrides)
    path = env.root / "grants" / f"{value['session_id']}.json"
    path.write_text(json.dumps(value))
    path.chmod(0o640)
    return value


def request(**overrides) -> bytes:
    value = {"version": 1, "request_id": "req-00000001", "operation": "launch", "session_id": "s1a2b3c4d5",
             "generation": 1, "policy_id": "default"}
    value.update(overrides)
    return json.dumps(value).encode()


def ask(env, line: bytes, pid: int = BROKER_PID, uid: int = AGENT_UID) -> dict:
    return env.m.handle(line, pid, uid)


def calls(env) -> list[str]:
    path = env.fakes / "calls"
    return path.read_text().splitlines() if path.exists() else []


def runs(env) -> list[str]:
    return [c for c in calls(env) if c.startswith("systemd-run ")]


def audit(env) -> list[dict]:
    return [json.loads(line) for path in sorted((env.root / "audit").glob("*.jsonl")) for line in path.read_text().splitlines()]


def record(env, session_id="s1a2b3c4d5") -> dict | None:
    return env.m.read_record(session_id)


# ---- the launch contract --------------------------------------------------------------------------------------------

def test_launch_runs_a_delegated_unit_as_the_account_with_member_limits(env):
    grant(env)
    out = ask(env, request())
    assert out["ok"] and out["code"] == "launched", out
    [run] = runs(env)
    assert "--uid=0" in run and "--gid=0" in run and "--property=Delegate=yes" in run
    assert "--unit=acc-ct-s1a2b3c4d5-g1.service" in run and f"--slice=acc-containers-{AGENT_UID}.slice" in run
    assert "--property=CPUQuota=200%" in run and "--property=MemoryMax=4096M" in run
    assert "--property=TasksMax=512" in run and "--pids-limit=512" in run
    # The pinned image's held rootfs is used without a second lookup by image ID.
    assert f" {IMAGE} " not in run and "--rootfs" in run and "--cap-drop=ALL" in run and "--userns=keep-id" in run
    assert '--userns=keep-id:uid=1000,gid=1000' in run and '--user=1000:1000' in run
    assert run.count("--mount=") == 6  # cnsr added the team-reply bind
    assert f"--mount=type=bind,src={env.m.STAGING_PARENT}/acc-ct-s1a2b3c4d5-g1/checkout,dst={env.checkout}" in run
    assert f"--mount=type=bind,src={env.m.STAGING_PARENT}/acc-ct-s1a2b3c4d5-g1/run,dst={env.run_dir},ro" in run
    assert "/usr/local/bin/claude --session-id abc" in run
    # and the account's slice holds 4 sessions' worth
    [slice_] = [c for c in calls(env) if "set-property" in c]
    assert "CPUQuota=800%" in slice_ and "MemoryMax=16384M" in slice_ and "TasksMax=2048" in slice_
    assert record(env)["state"] == "running"
    [entry] = audit(env)
    assert entry["action"] == "container.launch" and entry["correlation"] == "req-00000001"
    assert entry["limits"] == MEMBER and entry["account"] == "alex-agent"


def test_owner_unlimited_applies_no_limits(env):
    grant(env, limits=dict(UNLIMITED))
    assert ask(env, request())["ok"]
    [run] = runs(env)
    assert "CPUQuota" not in run and "MemoryMax" not in run and "TasksMax" not in run and "--pids-limit=-1" in run
    [slice_] = [c for c in calls(env) if "set-property" in c]
    assert "CPUQuota= " in slice_ + " " and "MemoryMax=infinity" in slice_ and "TasksMax=infinity" in slice_


def test_overrides_apply_as_given(env):
    grant(env, limits={"cpus": 6, "memory_mib": 1024, "pids": None, "disk_gib": 5, "sessions": 1})
    assert ask(env, request())["ok"]
    [run] = runs(env)
    assert "CPUQuota=600%" in run and "MemoryMax=1024M" in run and "TasksMax" not in run


@pytest.mark.parametrize("limits, code", [
    ({k: v for k, v in MEMBER.items() if k != "pids"}, "bad_limits"),  # missing is never "no limit"
    ({**MEMBER, "cpus": 0}, "bad_limits"), ({**MEMBER, "memory_mib": 10}, "bad_limits"),
    ({**MEMBER, "pids": "512"}, "bad_limits"), ({**MEMBER, "cpus": True}, "bad_limits"),
    ({**MEMBER, "swap": 1}, "bad_limits"),
])
def test_bad_limits_are_refused(env, limits, code):
    grant(env, limits=limits)
    out = ask(env, request())
    assert not out["ok"] and out["code"] == code
    assert not runs(env)


def test_session_cap_reserves_account_capacity(env):
    grant(env, limits={**MEMBER, "sessions": 1})
    assert ask(env, request())["ok"]
    second = "s9a8b7c6d5"
    run2 = env.run_dir.parent / second
    run2.mkdir()
    grant(env, session_id=second, run_dir=str(run2), limits={**MEMBER, "sessions": 1})
    out = ask(env, request(session_id=second, request_id="req-00000002"))
    assert out["code"] == "account_capacity" and len(runs(env)) == 1


# ---- refusals -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("line, code", [
    (b"not json", "bad_request"),
    (request()[:-1] + b', "image": "evil"}', "bad_request"),  # any extra field
    (request()[:-1] + b', "mounts": ["/"]}', "bad_request"),
    (request(version=2), "bad_version"), (request(version=True), "bad_version"),
    (request(operation="exec"), "bad_request"), (request(session_id="../etc"), "bad_request"),
    (request(generation=0), "bad_request"), (request(generation="1"), "bad_request"),
    (request(policy_id="Default Image"), "bad_request"), (b"x" * 5000, "too_large"),
])
def test_malformed_requests(env, line, code):
    grant(env)
    out = ask(env, line)
    assert not out["ok"] and out["code"] == code
    assert not runs(env)
    assert audit(env)[-1]["action"] == "container.refused" and audit(env)[-1]["code"] == code


def test_wrong_account_is_refused(env):
    grant(env)
    out = ask(env, request(), uid=AGENT_UID + 1)  # another account's broker asking for alex-agent's session
    assert out["code"] == "wrong_account" and not runs(env)


def test_other_accounts_session_is_refused(env):
    # the grant names other-agent; alex-agent's broker (uid AGENT_UID) asks for it
    (env.tmp / "broker-env/other-agent.env").write_text(env.settings.read_text())
    (env.tmp / "broker-env/other-agent.env").chmod(0o644)
    grant(env, account="other-agent")
    out = ask(env, request())
    assert out["code"] == "wrong_account" and not runs(env)


def test_an_agent_process_of_the_same_account_is_not_the_broker(env):
    grant(env)
    out = ask(env, request(), pid=BROKER_PID + 7)  # same uid, but a session's process, not the broker's MainPID
    assert out["code"] == "not_broker" and not runs(env)
    (env.fakes / "mainpid").write_text("0\n")  # broker not running
    assert ask(env, request())["code"] == "not_broker"


def test_without_a_grant_nothing_runs(env):
    out = ask(env, request())
    assert out["code"] == "no_grant" and not runs(env)


def test_grant_must_be_the_control_planes(env):
    grant(env)
    path = env.root / "grants/s1a2b3c4d5.json"
    path.chmod(0o666)
    assert ask(env, request())["code"] == "no_grant"
    path.chmod(0o640)
    env.users["acc"] = (env.runner_uid + 1, "/srv/acc/data")  # written by someone other than accd
    assert ask(env, request())["code"] == "no_grant"


def test_grant_is_a_link_is_refused(env):
    real = env.tmp / "elsewhere.json"
    real.write_text(json.dumps(grant(env)))
    (env.root / "grants/s1a2b3c4d5.json").unlink()
    (env.root / "grants/s1a2b3c4d5.json").symlink_to(real)
    assert ask(env, request())["code"] == "no_grant"


def test_stale_and_mismatched_generations(env):
    grant(env, generation=3)
    assert ask(env, request(generation=2))["code"] == "stale_generation"
    assert ask(env, request(generation=3, policy_id="other"))["code"] == "stale_generation"
    assert ask(env, request(generation=3))["ok"]
    grant(env, generation=2)  # an older grant can't reuse a newer record
    assert ask(env, request(generation=2, request_id="req-00000009"))["code"] == "stale_generation"


def test_exact_retry_is_idempotent_and_a_different_one_conflicts(env):
    grant(env)
    assert ask(env, request())["code"] == "launched"
    assert ask(env, request())["code"] == "already"
    assert ask(env, request(request_id="req-00000002"))["code"] == "conflict"
    assert len(runs(env)) == 1


@pytest.mark.parametrize("policy, code", [
    (None, "unpinned_image"),
    ({"version": 1, "images": {"default": {"image": "docker.io/library/ubuntu:latest", "providers": {}}}}, "unpinned_image"),
    ({"version": 1, "images": {"default": {"image": "sha256:" + "0" * 64, "providers": {}}}}, "unpinned_image"),
    ({"version": 1, "images": {"other": {"image": IMAGE, "providers": {}}}}, "unpinned_image"),
    ({"version": 1, "images": {"default": {"image": IMAGE, "network": "host", "providers": {}}}}, "unpinned_image"),
    ({"version": 1, "images": {"default": {"image": IMAGE, "providers": {"claude": "../bin/sh"}}}}, "unpinned_image"),
    ({"version": 1, "images": {"default": {"image": IMAGE, "providers": {"codex": "/usr/bin/codex"}}}}, "unpinned_image"),
])
def test_only_pinned_images(env, policy, code):
    grant(env)
    if policy is None:
        env.policy.unlink()
    else:
        env.policy.write_text(json.dumps(policy))
    out = ask(env, request())
    assert out["code"] == code and not runs(env)


def test_writable_policy_is_refused(env):
    grant(env)
    env.policy.chmod(0o666)
    assert ask(env, request())["code"] == "unpinned_image"


def test_extra_mounts_and_fields_in_a_grant_are_refused(env):
    value = grant(env)
    value["mounts"] = ["/var/run/docker.sock"]
    (env.root / "grants/s1a2b3c4d5.json").write_text(json.dumps(value))
    assert ask(env, request())["code"] == "bad_grant" and not runs(env)


@pytest.mark.parametrize("field, value", [
    ("checkout", "{ws}/.worktrees/../../etc"),
    ("checkout", "{ws}/.worktrees//demo-1a2b3c4d"),
    ("checkout", "{ws}/.worktrees/demo-1a2b3c4d/"),
    ("checkout", "relative/.worktrees/demo"),
    ("checkout", "{ws}/other/demo-1a2b3c4d"),  # not a session checkout
    ("checkout", "/etc/.worktrees/x"),  # outside the account's roots
    ("checkout", "{ws}/.worktrees/demo,dst=/etc"),  # mount option injection
    ("run_dir", "{run}/../s1a2b3c4d5"),
    ("run_dir", "{ws}/.worktrees/s1a2b3c4d5"),  # not this account's run folder for the session
])
def test_path_tricks(env, field, value):
    grant(env, **{field: value.format(ws=env.ws, run=env.run_dir.parent)})
    out = ask(env, request())
    assert not out["ok"] and out["code"] in ("bad_mount", "bad_grant"), out
    assert not runs(env)


def test_symlinked_checkout_is_refused(env):
    target = env.ws / ".worktrees/demo-1a2b3c4d"
    link = env.ws / ".worktrees/link-1a2b3c4d"
    link.symlink_to(target)
    grant(env, checkout=str(link))
    assert ask(env, request())["code"] == "bad_mount" and not runs(env)


def test_symlinked_ancestor_is_refused(env):
    (env.tmp / "ws2").symlink_to(env.ws)
    env.settings.write_text(env.settings.read_text().replace(f"ACC_ALLOWED_ROOTS={env.ws}", f"ACC_ALLOWED_ROOTS={env.tmp}/ws2"))
    grant(env, checkout=f"{env.tmp}/ws2/.worktrees/demo-1a2b3c4d")
    assert ask(env, request())["code"] == "bad_mount"


def test_worktree_with_shared_git_is_refused(env):
    (env.checkout / ".git").rmdir()
    (env.checkout / ".git").write_text("gitdir: /srv/project/.git/worktrees/demo\n")
    grant(env)
    assert ask(env, request())["code"] == "shared_git" and not runs(env)


def test_world_writable_mount_is_refused(env):
    env.run_dir.chmod(0o777)
    grant(env)
    assert ask(env, request())["code"] == "bad_mount"


def test_a_mount_inside_home_is_refused(env):
    inside = env.home / "ws"
    (inside / ".worktrees/demo-1a2b3c4d/.git").mkdir(parents=True)
    env.settings.write_text(env.settings.read_text().replace(f"ACC_ALLOWED_ROOTS={env.ws}", f"ACC_ALLOWED_ROOTS={inside}"))
    grant(env, checkout=str(inside / ".worktrees/demo-1a2b3c4d"))
    assert ask(env, request())["code"] == "bad_mount"


@pytest.mark.parametrize("change, code", [
    ("admin", "unknown_account"), ("no-subuid", "no_subordinate_ids"), ("overlap", "no_subordinate_ids"),
    ("services", "unknown_account"), ("writable-env", "unknown_account"),
])
def test_account_checks(env, change, code):
    grant(env)
    if change == "admin":
        env.groups["sudo"] = SimpleNamespace(gr_name="sudo", gr_mem=["alex-agent"])
    elif change == "no-subuid":
        (env.tmp / "subuid").write_text("other-agent:300000:65536\n")
    elif change == "overlap":
        (env.tmp / "subgid").write_text("alex-agent:200000:65536\nother-agent:250000:65536\n")
    elif change == "services":
        env.settings.write_text("ACC_BROKER_ROLE=services\n")
    elif change == "writable-env":
        env.settings.chmod(0o666)
    assert ask(env, request())["code"] == code and not runs(env)


def test_root_and_non_agent_accounts_are_refused(env):
    grant(env, account="root-agent")
    out = ask(env, request(), uid=0)
    assert out["code"] == "unknown_account" and out["detail"] == "never root"
    grant(env, account="alex")
    assert ask(env, request())["code"] == "bad_grant"


# ---- cleanup, stop, status ------------------------------------------------------------------------------------------

def test_failed_start_is_cleaned_up_and_releases_capacity(env):
    (env.fakes / "run_rc").write_text("1")
    (env.fakes / "active").write_text("failed\n")
    grant(env)
    out = ask(env, request())
    assert out["code"] == "start_failed"
    assert any("systemctl stop acc-ct-s1a2b3c4d5-g1.service" in c for c in calls(env))
    assert not any(" rm " in c for c in calls(env))
    assert record(env)["state"] == "failed"
    failed = [e for e in audit(env) if e["action"] == "container.launch_failed"]
    assert failed and failed[0]["cleanup"] == "done"
    # capacity is free again: a retry with a new generation works
    (env.fakes / "run_rc").write_text("0")
    grant(env, generation=2)
    assert ask(env, request(generation=2, request_id="req-00000003"))["ok"]


def test_failed_cleanup_stays_visible_and_blocks_a_relaunch(env):
    (env.fakes / "run_rc").write_text("1")
    (env.fakes / "label").write_text("s1a2b3c4d5\n")
    (env.fakes / "stuck").write_text("")
    grant(env)
    ask(env, request())
    assert record(env)["state"] == "cleanup_failed"
    grant(env, generation=2)
    assert ask(env, request(generation=2, request_id="req-00000004"))["code"] == "conflict"


def test_a_container_named_like_the_session_in_the_accounts_own_store_never_blocks_it(env):
    """The container only ever exists inside its unit (private runtime tmpfs and namespaces). A same-named container
    that anything running as the account made in its own store proves nothing: it used to quarantine every stop of
    that session (cleanup_failed, capacity held, no relaunch). It is neither consulted nor removed."""
    (env.fakes / "run_rc").write_text("1")
    (env.fakes / "label").write_text("zzzzzzzzzz\n")
    grant(env)
    ask(env, request())
    assert not any("container exists" in c or " rm " in c for c in calls(env) if c.startswith("runuser"))
    assert not any(e["action"] == "container.quarantined" for e in audit(env))
    assert record(env)["state"] == "failed"  # cleaned: the unit stopped
    grant(env, generation=2)
    (env.fakes / "run_rc").write_text("0")
    assert ask(env, request(generation=2, request_id="req-00000005"))["ok"]
    assert ask(env, request(operation="stop", generation=2, request_id="req-00000006"))["code"] == "stopped"


def test_replaced_mount_after_start_is_stopped(env, monkeypatch):
    grant(env)
    monkeypatch.setattr(env.m, "same_dirs", lambda identity, uid: False)
    (env.fakes / "active").write_text("inactive\n")
    out = ask(env, request())
    assert out["code"] == "bad_mount" and record(env)["state"] == "failed"


def test_stop_and_status(env):
    grant(env)
    ask(env, request())
    status = ask(env, request(operation="status", request_id="req-00000005"))
    assert status["state"] == "running" and status["active"] == "active"
    (env.fakes / "active").write_text("inactive\n")
    out = ask(env, request(operation="stop", request_id="req-00000006"))
    assert out["code"] == "stopped" and record(env)["state"] == "stopped"
    assert ask(env, request(operation="stop", request_id="req-00000007"))["code"] == "already"
    stops = [e for e in audit(env) if e["action"] == "container.stop"]
    assert stops and stops[0]["cleanup"] == "done"


def test_stop_by_another_account_is_refused(env):
    grant(env)
    ask(env, request())
    out = ask(env, request(operation="stop", request_id="req-00000008"), uid=AGENT_UID + 1)
    assert out["code"] == "wrong_account"
    assert not any("systemctl stop" in c for c in calls(env))


# ---- reconcile ------------------------------------------------------------------------------------------------------

def test_reconcile_stops_a_container_whose_grant_was_revoked(env):
    grant(env)
    ask(env, request())
    (env.root / "grants/s1a2b3c4d5.json").unlink()
    [done] = env.m.reconcile()
    assert done["reason"] == "grant_revoked" and record(env)["state"] == "stopped"


def test_reconcile_notices_exits_and_disk_limit(env):
    grant(env, limits={**MEMBER, "disk_gib": 1})
    ask(env, request())
    big = env.checkout / "big.bin"
    with open(big, "wb") as handle:  # real blocks, not a sparse file
        chunk = b"x" * (1 << 20)
        for _ in range(1100):
            handle.write(chunk)
    [done] = env.m.reconcile()
    assert done["reason"] == "disk_limit"
    big.unlink()


def test_reconcile_reports_unknown_units_without_touching_them(env):
    (env.fakes / "units").write_text("acc-ct-zzzzzzzzzz-g1.service loaded active running x\n")
    env.m.reconcile()
    assert any(e["action"] == "container.unknown_unit" for e in audit(env))
    assert not any("systemctl stop" in c for c in calls(env))


def test_reconcile_marks_an_exited_container(env):
    grant(env)
    ask(env, request())
    (env.fakes / "active").write_text("inactive\n")
    [done] = env.m.reconcile()
    assert done["reason"] == "exited" and record(env)["state"] == "exited"


# ---- the socket ---------------------------------------------------------------------------------------------------

def test_serve_reads_one_line_and_answers_with_peer_credentials(env, monkeypatch):
    grant(env)
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    assert env.real_peer(right) == (os.getpid(), env.runner_uid)  # retain real SO_PEERCRED coverage
    thread = threading.Thread(target=env.m.serve, args=(right,))
    thread.start()
    left.sendall(request() + b"\n")
    answer = json.loads(left.makefile().readline())
    thread.join(5)
    assert answer["ok"] and answer["code"] == "launched"


def test_root_peer_cannot_launch_a_non_root_account(env):
    grant(env)
    out = ask(env, request(), uid=0)
    assert out["code"] == "wrong_account" and not runs(env)


def test_audit_files_are_group_readable_only(env):
    grant(env)
    ask(env, request())
    [path] = (env.root / "audit").glob("*.jsonl")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_reconcile_cleans_up_a_launch_that_died_while_starting(env):
    grant(env)
    record_ = {"session_id": "s1a2b3c4d5", "generation": 1, "request_id": "req-00000001", "account": "alex-agent",
               "uid": AGENT_UID, "unit": "acc-ct-s1a2b3c4d5-g1.service", "policy_id": "default", "image": IMAGE,
               "limits": MEMBER, "mounts": {}, "state": "starting", "started_at": "2026-01-01T00:00:00+00:00"}
    env.m.write_record(record_)
    (env.fakes / "active").write_text("inactive\n")
    [done] = env.m.reconcile()
    assert done["reason"] == "exited" and record(env)["state"] == "exited"
    # a fresh "starting" record is left alone (its launch may still be running)
    env.m.write_record({**record_, "state": "starting", "started_at": env.m.now()})
    assert env.m.reconcile() == []

# Security-review proofs: each strict xfail is removed only with its corresponding fix.
def test_ls01_mount_sources_are_pinned_until_namespace_setup(env, monkeypatch):
    value = grant(env)
    held, _ = env.m.check_mounts(value, env.m.pwd.getpwnam("alex-agent"), env.m.broker_settings("alex-agent"))
    try:
        import inspect
        extra = {"held": held} if "held" in inspect.signature(env.m.systemd_run_argv).parameters else {}
        held.append(env.m.open_image_rootfs(env.m.load_policy("default")))
        argv = env.m.systemd_run_argv(value, env.m.load_policy("default"), env.m.pwd.getpwnam("alex-agent"), **extra)
        binds = [a for a in argv if a.startswith("--property=BindPaths=")]
        assert len(binds) == 2
        for fd, bind in zip(held, binds):
            assert f"/proc/{os.getpid()}/fd/{fd}:" in bind
        mounts = [a for a in argv if a.startswith("--mount=")]
        assert all(f"src={env.m.STAGING_PARENT}/" in a for a in mounts)
    finally:
        for fd in held:
            os.close(fd)


def test_ls02_forged_label_never_authorizes_removal_on_failed_start(env):
    grant(env)
    (env.fakes / "run_rc").write_text("1")
    (env.fakes / "label").write_text("s1a2b3c4d5\n")
    answer = ask(env, request())
    assert answer["code"] == "start_failed"
    assert not any(" rm " in c for c in calls(env))
    assert record(env)["state"] == "failed"  # the unit stopped: nothing of it is left, whatever the store says
    assert not any(e["action"] == "container.quarantined" for e in audit(env))


def test_a_unit_that_does_not_stop_is_quarantined_and_holds_its_capacity(env):
    (env.fakes / "stuck").write_text("")
    grant(env)
    assert ask(env, request())["ok"]
    answer = ask(env, request(operation="stop", request_id="req-00000002"))
    assert not answer["ok"] and answer["code"] == "cleanup_failed"
    assert record(env)["state"] == "cleanup_failed"
    assert [e["detail"] for e in audit(env) if e["action"] == "container.quarantined"] == [
        "the session's unit didn't stop; owner review required"]


def test_the_container_uid_is_one_launcher_constant_behind_both_flags(env, monkeypatch):
    """L-2 (phase1-in-container independent review): keep-id and --user come from CONTAINER_UID, the single line the
    qualification driver reads from the installed launcher."""
    value = grant(env)
    argv = env.m.podman_argv(value, env.m.load_policy("default"), env.m.pwd.getpwnam("alex-agent"))
    uid = env.m.CONTAINER_UID
    assert f"--userns=keep-id:uid={uid},gid={uid}" in argv and f"--user={uid}:{uid}" in argv
    monkeypatch.setattr(env.m, "CONTAINER_UID", 2345)
    argv = env.m.podman_argv(value, env.m.load_policy("default"), env.m.pwd.getpwnam("alex-agent"))
    assert "--userns=keep-id:uid=2345,gid=2345" in argv and "--user=2345:2345" in argv
    assert not any(f"{uid}" in arg for arg in argv if arg.startswith(("--userns", "--user=")))


def test_ls03_image_defaults_cannot_extend_fixed_contract(env):
    value = grant(env)
    argv = env.m.podman_argv(value, env.m.load_policy("default"), env.m.pwd.getpwnam("alex-agent"))
    assert "--image-volume=ignore" in argv
    # No healthcheck, with a zero interval: --health-cmd=none kept the default 30 s interval, so Podman 4.9 still tried
    # to add a systemd timer over a bus the unit doesn't have and logged an error (attempt 15's unit log).
    assert "--no-healthcheck" in argv and not any(arg.startswith("--health-") for arg in argv)
    assert "--cgroupns=private" in argv
    assert "--read-only-tmpfs=false" in argv
    assert "--log-driver=none" in argv


def test_ls04_stop_cannot_overtake_an_inflight_launch(env, monkeypatch):
    grant(env)
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    run = env.m.subprocess.run
    def pause(argv, **kwargs):
        if argv[0] == env.m.SYSTEMD_RUN:
            entered.set()
            assert release.wait(5)
        return run(argv, **kwargs)
    monkeypatch.setattr(env.m.subprocess, "run", pause)
    launch_thread = threading.Thread(target=lambda: ask(env, request()))
    def stop():
        ask(env, request(operation="stop", request_id="req-00000002"))
        stopped.set()
    stop_thread = threading.Thread(target=stop)
    launch_thread.start()
    try:
        assert entered.wait(5)
        stop_thread.start()
        assert not stopped.wait(0.2)
    finally:
        release.set()
        launch_thread.join(5)
        if stop_thread.ident:
            stop_thread.join(5)
    assert record(env)["state"] == "stopped"


def test_ls05_disk_scan_never_follows_replaced_root(env):
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "data").write_bytes(b"x" * 8192)
    original = env.checkout.with_name("saved")
    env.checkout.rename(original)
    env.checkout.symlink_to(outside, target_is_directory=True)
    with pytest.raises(env.m.Refused, match="disk"):
        env.m.disk_bytes(str(env.checkout), 1 << 20)


def test_ls06_runtime_diagnostics_are_not_audited(env, monkeypatch):
    import subprocess
    grant(env, args=["SYNTHETIC_PRIVATE_PROMPT"])
    run = env.m.subprocess.run
    def timeout(argv, **kwargs):
        if argv[0] == env.m.SYSTEMD_RUN:
            raise subprocess.TimeoutExpired(argv, 45)
        return run(argv, **kwargs)
    monkeypatch.setattr(env.m.subprocess, "run", timeout)
    answer = ask(env, request())
    assert answer["code"] == "start_failed"
    assert "SYNTHETIC_PRIVATE_PROMPT" not in json.dumps(audit(env))
    assert "SYNTHETIC_PRIVATE_PROMPT" not in json.dumps(answer)


def test_ls03_network_is_none_until_egress_policy_is_qualified(env):
    policy = json.loads(env.policy.read_text())
    policy["images"]["default"]["network"] = "pasta"
    env.policy.write_text(json.dumps(policy))
    grant(env)
    assert ask(env, request())["code"] == "unpinned_image"
    assert not runs(env)


def test_ls09_audit_buffer_is_bounded_and_keeps_valid_json(env, monkeypatch):
    monkeypatch.setattr(env.m, "MAX_AUDIT_BYTES", 2048, raising=False)
    for _ in range(50):
        ask(env, b"not json")
    [path] = (env.root / "audit").glob("*.jsonl")
    assert path.stat().st_size <= 2048
    assert all(json.loads(line)["action"] == "container.refused" for line in path.read_text().splitlines())


def test_ls09_large_audit_details_remain_ingestible(env):
    env.m.audit("container.refused", detail="SYNTHETIC " * 1000)
    [path] = (env.root / "audit").glob("*.jsonl")
    entry = json.loads(path.read_text())
    assert entry["action"] == "container.refused"
    assert len(path.read_bytes()) <= 4000


def test_ls04_cleanup_timeout_is_durable_and_audited(env, monkeypatch):
    import subprocess
    grant(env)
    assert ask(env, request())["ok"]
    run = env.m.subprocess.run
    def timeout(argv, **kwargs):
        if argv[:2] == [env.m.SYSTEMCTL, "stop"]:
            raise subprocess.TimeoutExpired(argv, 30)
        return run(argv, **kwargs)
    monkeypatch.setattr(env.m.subprocess, "run", timeout)
    answer = ask(env, request(operation="stop", request_id="req-00000002"))
    assert answer["code"] == "cleanup_failed"
    assert record(env)["state"] == "cleanup_failed"
    assert any(e["action"] == "container.stop" and e["cleanup"] == "failed" for e in audit(env))


def test_ls09_refusal_flood_cannot_hide_lifecycle_events(env, monkeypatch):
    monkeypatch.setattr(env.m, "MAX_AUDIT_BYTES", 1024)
    for _ in range(30):
        ask(env, b"not json")
    env.m.audit("container.launch", account="alex-agent", session_id="s1a2b3c4d5", detail="lifecycle" * 30)
    assert any(e["action"] == "container.launch" for e in audit(env))


def test_ls09_peer_uids_cannot_multiply_refusal_streams(env):
    for uid in range(100001, 100101):
        ask(env, b"not json", uid=uid)
    assert len(list((env.root / "audit").glob("*.jsonl"))) <= 64


def test_ls01_each_generation_has_distinct_staging_sources(env):
    value = grant(env)
    entry, policy = env.m.pwd.getpwnam("alex-agent"), env.m.load_policy("default")
    first = [arg for arg in env.m.podman_argv(value, policy, entry) if arg.startswith("--mount=")]
    second = [arg for arg in env.m.podman_argv({**value, "generation": 2}, policy, entry) if arg.startswith("--mount=")]
    assert {a.split(",dst=")[0] for a in first}.isdisjoint(a.split(",dst=")[0] for a in second)


def test_ls03_user_runtime_configuration_is_not_consumed(env):
    value = grant(env)
    held, _ = env.m.check_mounts(value, env.m.pwd.getpwnam("alex-agent"), env.m.broker_settings("alex-agent"))
    try:
        held.append(env.m.open_image_rootfs(env.m.load_policy("default")))
        argv = env.m.systemd_run_argv(value, env.m.load_policy("default"), env.m.pwd.getpwnam("alex-agent"), held=held)
        assert "--setenv=CONTAINERS_CONF=/dev/null" in argv
        assert "--setenv=CONTAINERS_CONF_OVERRIDE=/dev/null" in argv
    finally:
        for fd in held:
            os.close(fd)


def test_ls03_subscription_mounts_and_storage_hooks_cannot_extend_contract(env):
    value = grant(env)
    held, _ = env.m.check_mounts(value, env.m.pwd.getpwnam("alex-agent"), env.m.broker_settings("alex-agent"))
    try:
        held.append(env.m.open_image_rootfs(env.m.load_policy("default")))
        argv = env.m.systemd_run_argv(value, env.m.load_policy("default"), env.m.pwd.getpwnam("alex-agent"), held=held)
        assert f"--setenv=CONTAINERS_STORAGE_CONF={env.tmp / 'storage.conf'}" in argv
        assert "--default-mounts-file=/dev/null" in argv
        assert "--events-backend=none" in argv
        assert "--runtime=/usr/bin/crun" in argv
    finally:
        for fd in held:
            os.close(fd)


def test_launch_inputs_are_frozen_and_guard_hashes_verified(env):
    import hashlib

    original = b'{"rules": {"force_push": true}}'
    (env.run_dir / 'guardrails.json').write_bytes(original)
    (env.run_dir / 'events.jsonl').touch()
    value = grant(env, guard_files='guardrails.json=' + hashlib.sha256(original).hexdigest())
    held, _ = env.m.check_mounts(value, env.m.pwd.getpwnam('alex-agent'), env.m.broker_settings('alex-agent'))
    try:
        env.m.freeze_inputs(value, held, env.m.pwd.getpwnam('alex-agent'))
        frozen = env.m.input_dir(value)
        (env.run_dir / 'guardrails.json').write_text('changed')
        assert (frozen / 'guardrails.json').read_bytes() == original
        assert (frozen / 'guardrails.json').stat().st_uid == env.runner_uid
        assert (frozen / 'guardrails.json').stat().st_mode & 0o222 == 0
        assert frozen.stat().st_mode & 0o222 == 0
        assert len(held) == 5
        held.append(env.m.open_image_rootfs(env.m.load_policy('default')))
        argv = env.m.systemd_run_argv(value, env.m.load_policy('default'), env.m.pwd.getpwnam('alex-agent'), held=held)
        assert f'--mount=type=bind,src={env.m.staging_dir(value)}/run,dst={env.run_dir},ro' in argv
        assert '--env=HOME=/home/acc' in argv
        assert not any(arg.startswith(f'--tmpfs={env.home}') for arg in argv)
    finally:
        for fd in held:
            os.close(fd)


def test_freeze_refuses_changed_hash_and_linked_inputs(env):
    import hashlib

    (env.run_dir / 'guardrails.json').write_text('changed')
    value = grant(env, guard_files='guardrails.json=' + hashlib.sha256(b'approved').hexdigest())
    held, _ = env.m.check_mounts(value, env.m.pwd.getpwnam('alex-agent'), env.m.broker_settings('alex-agent'))
    try:
        with pytest.raises(env.m.Refused, match='hash'):
            env.m.freeze_inputs(value, held, env.m.pwd.getpwnam('alex-agent'))
        assert not env.m.input_dir(value).exists()
        (env.run_dir / 'guardrails.json').unlink()
        (env.run_dir / 'guardrails.json').symlink_to(env.tmp / 'outside')
        with pytest.raises((env.m.Refused, OSError)):
            env.m.freeze_inputs(value, held, env.m.pwd.getpwnam('alex-agent'))
    finally:
        for fd in held:
            os.close(fd)


def test_runtime_storage_config_is_explicit_and_protected(env):
    assert env.m.storage_config() == str(env.tmp / 'storage.conf')
    value = grant(env)
    held, _ = env.m.check_mounts(value, env.m.pwd.getpwnam('alex-agent'), env.m.broker_settings('alex-agent'))
    try:
        held.append(env.m.open_image_rootfs(env.m.load_policy('default')))
        argv = env.m.systemd_run_argv(value, env.m.load_policy('default'), env.m.pwd.getpwnam('alex-agent'), held=held)
        assert f'--setenv=CONTAINERS_STORAGE_CONF={env.tmp / "storage.conf"}' in argv
    finally:
        for fd in held:
            os.close(fd)
    (env.tmp / 'storage.conf').chmod(0o666)
    with pytest.raises(env.m.Refused):
        env.m.storage_config()


def test_launcher_generates_readonly_contract_not_supplied_by_agent(env):
    value = grant(env)
    held, _ = env.m.check_mounts(value, env.m.pwd.getpwnam('alex-agent'), env.m.broker_settings('alex-agent'))
    try:
        env.m.freeze_inputs(value, held, env.m.pwd.getpwnam('alex-agent'))
        contract = env.m.input_dir(value) / '.acc-launch-contract.json'
        body = json.loads(contract.read_text())
        assert body['issuer'] == 'acc-containers' and body['sandbox'] == 'container'
        assert body['session_id'] == value['session_id'] and body['generation'] == value['generation']
        assert not contract.stat().st_mode & 0o222
        argv = env.m.podman_argv(value, env.m.load_policy('default'), env.m.pwd.getpwnam('alex-agent'))
        assert any('dst=/run/acc-container/contract.json,ro' in arg for arg in argv)
    finally:
        for fd in held: os.close(fd)


def test_agent_cannot_supply_launcher_contract(env):
    (env.run_dir / '.acc-launch-contract.json').write_text('{"sandbox":"container"}')
    value = grant(env)
    held, _ = env.m.check_mounts(value, env.m.pwd.getpwnam('alex-agent'), env.m.broker_settings('alex-agent'))
    try:
        with pytest.raises(env.m.Refused):
            env.m.freeze_inputs(value, held, env.m.pwd.getpwnam('alex-agent'))
    finally:
        for fd in held: os.close(fd)


def test_ct06_launch_audit_records_that_the_container_is_the_sandbox(env):
    from acc.server.containers import AUDIT_FIELDS

    grant(env)
    assert ask(env, request())["ok"]
    [entry] = audit(env)
    assert entry["action"] == "container.launch" and entry["sandbox"] == "container"
    assert "sandbox" in AUDIT_FIELDS  # and Rynhult's audit log keeps it


def test_ct05_rejects_account_image_layers(env):
    with pytest.raises(env.m.Refused):
        env.m.check_image_location({'Id': IMAGE, 'GraphDriver': {'Name': 'overlay', 'Data': {
            'UpperDir': str(env.home / '.local/share/containers/storage/overlay/layer/diff')}}}, IMAGE)


def test_ct05_refusal_explains_an_own_store_layer_in_the_journal_only(env, capsys):
    """Qualification attempt 11: one layer of the pinned image resolved to the account's own store. The refusal stays the
    same; the journal says which path, that it's the account's store, and how to fix it."""
    layer = 'ef' * 32
    account = env.m.pwd.getpwnam('alex-agent')
    own = str(env.home / f'.local/share/containers/storage/overlay/{layer}/diff')
    with pytest.raises(env.m.Refused) as refused:
        env.m.check_image_location({'Id': IMAGE, 'GraphDriver': {'Name': 'overlay', 'Data': {
            'UpperDir': str(env.tmp / 'images/overlay/layer/diff'), 'LowerDir': own}}}, IMAGE, account)
    assert (refused.value.code, refused.value.detail) == (
        'untrusted_image_store', 'session image must resolve exclusively to protected root-owned layers')
    [line] = [json.loads(line) for line in capsys.readouterr().err.splitlines()
              if 'image_store_diagnosis' in line]
    assert line['path'] == own and line['own_store'] is True and line['account'] == 'alex-agent'
    assert f'same ID ({layer})' in line['fix'] and f'--check-store {IMAGE}' in line['fix']
    assert not any(entry['action'] == 'container.image_store_diagnosis' for entry in audit(env))  # journal only


def test_ct05_refusal_explanation_outside_the_account_store(env, capsys):
    layer = env.tmp / 'images/overlay/layer/diff'
    layer.chmod(0o777)
    with pytest.raises(env.m.Refused):
        env.m.check_image_location({'Id': IMAGE, 'GraphDriver': {'Name': 'overlay', 'Data': {'UpperDir': str(layer)}}},
                                   IMAGE, env.m.pwd.getpwnam('alex-agent'))
    [line] = [json.loads(line) for line in capsys.readouterr().err.splitlines() if 'image_store_diagnosis' in line]
    assert line['own_store'] is False and 'not protected' in line['fix']
    with pytest.raises(env.m.Refused):
        env.m.check_image_location({'Id': 'sha256:' + 'cd' * 32, 'GraphDriver': {'Name': 'overlay', 'Data': {}}},
                                   IMAGE, env.m.pwd.getpwnam('alex-agent'))
    [line] = [json.loads(line) for line in capsys.readouterr().err.splitlines() if 'image_store_diagnosis' in line]
    assert 'path' not in line and 'pinned overlay image' in line['fix']


def test_ct05_refusal_is_unchanged_when_the_journal_write_fails_and_the_layer_id_is_capped(env, capsys, monkeypatch):
    """BS-4: the diagnosis is advisory; the account controls the inspected path, so its layer name is capped."""
    account = env.m.pwd.getpwnam('alex-agent')
    own = str(env.home / f'.local/share/containers/storage/overlay/{"ab" * 5000}/diff')
    inspect = {'Id': IMAGE, 'GraphDriver': {'Name': 'overlay', 'Data': {'UpperDir': own}}}
    with pytest.raises(env.m.Refused):
        env.m.check_image_location(inspect, IMAGE, account)
    [line] = [json.loads(line) for line in capsys.readouterr().err.splitlines() if 'image_store_diagnosis' in line]
    assert f'same ID ({"ab" * 40})' in line['fix'] and len(line['fix']) < 600 and len(line['path']) == 300

    class Broken:
        def write(self, _):
            raise OSError(5, 'journal gone')

        def flush(self):
            raise OSError(5, 'journal gone')

    monkeypatch.setattr(env.m.sys, 'stderr', Broken())
    with pytest.raises(env.m.Refused) as refused:
        env.m.check_image_location(inspect, IMAGE, account)
    assert (refused.value.code, refused.value.detail) == (
        'untrusted_image_store', 'session image must resolve exclusively to protected root-owned layers')


def test_ct05_accepts_only_protected_layers(env, monkeypatch):
    store = env.tmp / 'images'
    layer = store / 'overlay/layer/diff'
    layer.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(env.m, 'IMAGE_STORE', store)
    inspect = {'Id': IMAGE, 'GraphDriver': {'Name': 'overlay', 'Data': {'UpperDir': str(layer)}}}
    env.m.check_image_location(inspect, IMAGE)
    layer.chmod(0o777)
    with pytest.raises(env.m.Refused):
        env.m.check_image_location(inspect, IMAGE)


@pytest.mark.parametrize('change', ['id', 'lower', 'missing', 'symlink'])
def test_ct05_launch_refuses_untrusted_resolution(env, change):
    path = env.fakes / 'image'
    images = json.loads(path.read_text())
    if change == 'id':
        images[0]['Id'] = 'sha256:' + 'cd' * 32
    elif change == 'lower':
        images[0]['GraphDriver']['Data']['LowerDir'] = str(env.home)
    elif change == 'missing':
        images = []
    else:
        layer = env.tmp / 'images/overlay/layer/diff'
        layer.rmdir()
        layer.symlink_to(env.home, target_is_directory=True)
    path.write_text(json.dumps(images))
    grant(env)
    assert ask(env, request())['code'] == 'untrusted_image_store'
    assert 'systemd-run ' not in (env.fakes / 'calls').read_text()


def test_ct05_refuses_writable_guard_inside_protected_layer(env):
    guard = env.tmp / 'images/overlay/layer/diff/acc-guard'
    guard.write_text('reviewed')
    guard.chmod(0o666)
    grant(env)
    assert ask(env, request())['code'] == 'untrusted_image_store'
    assert not runs(env)


def test_ct05_refuses_store_beneath_writable_parent(env):
    env.tmp.chmod(0o777)
    grant(env)
    assert ask(env, request())['code'] == 'untrusted_image_store'
    assert not runs(env)


def test_ct05_refuses_replaceable_overlay_shortlink(env):
    links = env.tmp / 'images/overlay/l'
    links.mkdir(mode=0o777)
    links.chmod(0o777)
    (links / 'short').symlink_to('../layer/diff', target_is_directory=True)
    data = [{'Id': IMAGE, 'GraphDriver': {'Name': 'overlay', 'Data': {'UpperDir': str(links / 'short')}}}]
    (env.fakes / 'image').write_text(json.dumps(data))
    grant(env)
    assert ask(env, request())['code'] == 'untrusted_image_store'
    links.chmod(0o755)
    assert ask(env, request())['code'] == 'launched'


def test_ct05_refuses_agent_owned_readonly_guard(env, monkeypatch):
    guard = env.tmp / 'images/overlay/layer/diff/acc-guard'
    guard.write_text('reviewed')
    guard.chmod(0o444)
    original = Path.lstat

    def lstat(path, **kwargs):
        info = original(path, **kwargs)
        if path == guard:
            fields = list(info)
            fields[4] = env.runner_uid + 1
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, 'lstat', lstat)
    grant(env)
    assert ask(env, request())['code'] == 'untrusted_image_store'
    assert not runs(env)


def test_ct05_inspection_failure_returns_audited_refusal(env, monkeypatch):
    import subprocess

    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired('podman', 60)

    monkeypatch.setattr(env.m, 'as_account', unavailable)
    grant(env)
    assert ask(env, request())['code'] == 'untrusted_image_store'
    assert audit(env)[-1]['code'] == 'untrusted_image_store'
    assert not runs(env)


def failed_inspect(env, rc, stderr):
    (env.fakes / "inspect_rc").write_text(str(rc))
    (env.fakes / "inspect_err").write_text(stderr)


def test_a_failed_image_check_names_podmans_error_in_the_refusal_and_its_audit(env):
    """Attempt 17: generation 2's launch was refused here with only "protected session image is unavailable"; Podman's
    reason was discarded. Its exit code and last error line are kept, so the next failure names itself."""
    failed_inspect(env, 125, 'time="2026-10-26T11:27:09-05:00" level=warning msg="something minor"\n'
                             'time="2026-10-26T11:27:09-05:00" level=error msg="invalid internal status, try resetting '
                             'the pause process with \\"/usr/bin/podman system migrate\\": could not find any running '
                             'process: no such process"\n')
    grant(env)
    out = ask(env, request())
    assert not out["ok"] and out["code"] == "untrusted_image_store"
    expected = ('protected session image is unavailable: podman exited 125: invalid internal status, try resetting the '
                'pause process with "/usr/bin/podman system migrate": could not find any running process: no such '
                'process')
    assert out["detail"] == expected[:300]
    assert audit(env)[-1]["code"] == "untrusted_image_store" and audit(env)[-1]["detail"] == out["detail"]
    assert not runs(env)


def test_podmans_error_line_keeps_only_store_paths_and_printable_text(env):
    """Podman runs as the account, over the account's own storage: its text is the account's to shape. One line, plain
    printable characters, bounded; any path but Podman itself and the protected store is replaced."""
    store = str(env.m.IMAGE_STORE)
    failed_inspect(env, 125, f"Error: reading {env.home}/.local/share/containers/storage/db.sql and "
                             f"{store}/overlay/abc/diff and {store}/../../etc/shadow: \x1b[2Jdenied" + "x" * 400 + "\n")
    grant(env)
    detail = ask(env, request())["detail"]
    assert detail.startswith("protected session image is unavailable: podman exited 125: reading <path> and "
                             f"{store}/overlay/abc/diff and <path>: ?[2Jdenied")
    assert str(env.home) not in detail and "shadow" not in detail and "\x1b" not in detail
    assert len(detail) <= 300


@pytest.mark.parametrize("image,reason", [
    ("not json", "podman's answer is not an image list"),
    ("[]", "podman found 0 images"),
])
def test_an_unusable_answer_says_what_was_wrong(env, image, reason):
    (env.fakes / "image").write_text(image)
    grant(env)
    out = ask(env, request())
    assert out["code"] == "untrusted_image_store"
    assert out["detail"] == "protected session image is unavailable: " + reason


def test_a_podman_check_that_times_out_says_so(env, monkeypatch):
    import subprocess

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired("podman", 60)

    monkeypatch.setattr(env.m, "as_account", slow)
    grant(env)
    assert ask(env, request())["detail"] == ("protected session image is unavailable: podman image inspect took longer "
                                            "than 60 s")


def test_ct05_same_id_substitution_after_inspection_cannot_execute(env, monkeypatch):
    """A same-ID local image introduced after inspection must not supply the entrypoint."""
    grant(env)
    real_check = env.m.check_image
    real_run = env.m.subprocess.run
    executed = []
    local_entrypoint = env.home / 'modified-entrypoint'

    def inspect_then_substitute(*args):
        real_check(*args)
        local_entrypoint.write_text('modified entrypoint')

    def run(argv, **kwargs):
        if argv[0] == env.m.SYSTEMD_RUN:
            command = argv[argv.index('--') + 1:]
            source = command[command.index('--entrypoint=/usr/local/bin/acc-session') + 1]
            if '--rootfs' in command:
                binding = next(arg.removeprefix('--property=BindReadOnlyPaths=') for arg in argv
                               if arg.startswith('--property=BindReadOnlyPaths=') and f':{source}:norbind' in arg)
                held_source = Path(binding.split(':', 1)[0])
                executed.append((held_source / 'usr/local/bin/acc-session').read_text())
            else:
                executed.append(local_entrypoint.read_text())
        return real_run(argv, **kwargs)

    monkeypatch.setattr(env.m, 'check_image', inspect_then_substitute)
    monkeypatch.setattr(env.m.subprocess, 'run', run)
    assert ask(env, request())['code'] == 'launched'
    assert executed == ['reviewed entrypoint']


@pytest.mark.parametrize('change', ['missing', 'writable', 'symlink', 'ancestor-symlink', 'wrong-pin', 'missing-pin'])
def test_ct05_refuses_unprotected_execution_root(env, change):
    rootfs = env.tmp / 'images/rootfs' / IMAGE.removeprefix('sha256:')
    entrypoint = rootfs / 'usr/local/bin/acc-session'
    if change == 'missing':
        (rootfs / '.acc-image-id').unlink()
        entrypoint.unlink()
        (rootfs / 'usr/local/bin').rmdir()
        (rootfs / 'usr/local').rmdir()
        (rootfs / 'usr').rmdir()
        rootfs.rmdir()
    elif change == 'writable':
        entrypoint.chmod(0o666)
    elif change == 'symlink':
        entrypoint.unlink()
        entrypoint.symlink_to(env.home / 'entrypoint')
        rootfs.rename(rootfs.with_name('saved'))
        rootfs.symlink_to(rootfs.with_name('saved'), target_is_directory=True)
    elif change == 'ancestor-symlink':
        parent = rootfs.parent
        parent.rename(parent.with_name('saved'))
        parent.symlink_to(parent.with_name('saved'), target_is_directory=True)
    elif change == 'wrong-pin':
        (rootfs / '.acc-image-id').write_text('sha256:' + 'cd' * 32 + '\n')
    else:
        (rootfs / '.acc-image-id').unlink()
    grant(env)
    assert ask(env, request())['code'] == 'untrusted_image_store'
    assert not runs(env)


def test_ct05_podman_ignores_inherited_agent_configuration(env, monkeypatch):
    captured = []
    real_run = env.m.subprocess.run

    def run(argv, **kwargs):
        if argv[0] == env.m.RUNUSER:
            captured.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(env.m.subprocess, 'run', run)
    monkeypatch.setenv('CONTAINERS_STORAGE_CONF', str(env.home / 'evil-storage.conf'))
    monkeypatch.setenv('CONTAINER_HOST', 'unix:///tmp/evil-podman.sock')
    grant(env)
    assert ask(env, request())['ok']
    assert captured and all('-i' in argv for argv in captured)
    assert all(f'CONTAINERS_STORAGE_CONF={env.tmp}/storage.conf' in argv for argv in captured)
    command = shlex.split(runs(env)[0])
    assert f'--setenv=CONTAINERS_STORAGE_CONF={env.tmp}/storage.conf' in command


def test_ct05_hostile_pause_namespace_cannot_supply_execution_root(env, monkeypatch):
    grant(env)
    real_run = env.m.subprocess.run
    executed = []
    runtime = env.m.runtime_dir({'session_id': 's1a2b3c4d5', 'generation': 1})

    def run(argv, **kwargs):
        if argv[0] == env.m.SYSTEMD_RUN:
            protected = (
                '--uid=0' in argv
                and '--gid=0' in argv
                and '--property=PrivateUsers=no' in argv
                and '--working-directory=/' in argv
                and '--expand-environment=no' in argv
                and f'--setenv=XDG_RUNTIME_DIR={runtime}' in argv
                and f'--root={runtime}/storage' in argv
                and f'--runroot={runtime}/run' in argv
                and f'--tmpdir={runtime}/tmp' in argv
                and f'--property=TemporaryFileSystem={runtime}:rw,mode=0700,uid={AGENT_UID},gid={AGENT_UID}' in argv
            )
            executed.append('reviewed' if protected else 'hostile namespace root')
        return real_run(argv, **kwargs)

    monkeypatch.setattr(env.m.subprocess, 'run', run)
    assert ask(env, request())['ok']
    assert executed == ['reviewed']


@pytest.mark.parametrize('change', ['missing', 'writable', 'symlink', 'parent-writable', 'parent-symlink'])
def test_ct05_execution_guard_must_be_protected(env, change):
    guard = env.root / 'acc-container-exec'
    if change == 'missing':
        guard.unlink()
    elif change == 'writable':
        guard.chmod(0o666)
    elif change == 'parent-writable':
        env.root.chmod(0o777)
    elif change == 'parent-symlink':
        saved = env.root.with_name('saved-root')
        env.root.rename(saved)
        env.root.symlink_to(saved, target_is_directory=True)
    else:
        guard.unlink()
        guard.symlink_to(env.home / 'guard')
    grant(env)
    assert ask(env, request())['code'] == 'unsafe_runtime_config'
    assert not runs(env)


# ---- CT-05 on systemd 255: the root bootstrap (docs/reviews/2026-10-24-security-review-ct05-sd255.md) ---------------

def test_sd255_podman_runs_with_the_accounts_login_name(env):
    grant(env)
    assert ask(env, request())["ok"]
    [run] = runs(env)
    assert "--setenv=USER=alex-agent" in run and "--setenv=LOGNAME=alex-agent" in run


def test_sd255_launch_audit_records_the_bootstrap_refusal(env):
    (env.fakes / "run_rc").write_text("1")
    (env.fakes / "active").write_text("failed\n")
    (env.fakes / "journal").write_text('Starting Rynhult container...\n'
                                       '{"code": "unsafe_runtime_namespace", "detail": "execution namespace is not owned by root"}\n')
    grant(env)
    assert ask(env, request())["code"] == "start_failed"
    [failed] = [e for e in audit(env) if e["action"] == "container.launch_failed"]
    assert failed["reason"] == "unsafe_runtime_namespace: execution namespace is not owned by root"


@pytest.mark.parametrize('journal', [None, 'Error: {"code": "unsafe_runtime_namespace", "detail": "x"}\n',
                                     '{"code": "other", "detail": "x"}\n', '{"code": "unsafe_runtime_namespace"}\n'])
def test_sd255_audit_reason_only_from_the_guards_own_refusal_line(env, journal):
    (env.fakes / "run_rc").write_text("1")
    (env.fakes / "active").write_text("failed\n")
    if journal is not None:
        (env.fakes / "journal").write_text(journal)
    grant(env)
    assert ask(env, request())["code"] == "start_failed"
    [failed] = [e for e in audit(env) if e["action"] == "container.launch_failed"]
    assert "reason" not in failed and failed["cleanup"] == "done"


def test_sd255_unreadable_journal_still_refuses_and_cleans_up(env):
    (env.fakes / "journalctl").unlink()
    (env.fakes / "run_rc").write_text("1")
    (env.fakes / "active").write_text("failed\n")
    grant(env)
    assert ask(env, request())["code"] == "start_failed"
    assert record(env)["state"] == "failed"


@pytest.mark.parametrize('name,attack,detail', [
    ('settings.json', 'wrong-owner', 'input is not owned'),
    ('events.jsonl', 'wrong-owner', 'output is not a private regular file'),
    ('events.jsonl', 'hardlink', 'output is not a private regular file'),
    ('events.jsonl', 'fifo', 'output is not a private regular file'),
    ('settings.json', 'hardlink', 'input is linked'),
])
def test_freeze_keeps_real_ownership_and_private_file_refusals(env, name, attack, detail):
    value = grant(env)
    path = env.run_dir / name
    if attack == 'wrong-owner':
        target = path.stat()
        real_fstat = env.m.os.fstat

        def foreign_owner(fd):
            info = real_fstat(fd)
            if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
                return SimpleNamespace(**{key: getattr(info, key) for key in dir(info) if key.startswith('st_')}
                                       | {'st_uid': 0})
            return info

        env.m.os.fstat = foreign_owner
    elif attack == 'hardlink':
        os.link(path, env.tmp / 'linked-input')
    else:
        path.unlink()
        os.mkfifo(path)
    entry = env.m.pwd.getpwnam('alex-agent')
    held, _ = env.m.check_mounts(value, entry, env.m.broker_settings('alex-agent'))
    try:
        with pytest.raises(env.m.Refused, match=detail):
            env.m.freeze_inputs(value, held, entry)
        assert not env.m.input_dir(value).exists()
    finally:
        for fd in held:
            os.close(fd)


# Rename compatibility review (rc1): the root launcher must resolve broker settings exactly like the broker does.
def test_rc1_new_allowed_roots_win_in_root_launcher(env):
    narrow = env.tmp / "narrow"
    narrow.mkdir()
    env.settings.write_text(env.settings.read_text() + f"RYNHULT_ALLOWED_ROOTS={narrow}\n")
    with pytest.raises(env.m.Refused):
        env.m.check_mounts(grant(env), env.m.pwd.getpwnam("alex-agent"), env.m.broker_settings("alex-agent"))


def test_rc1_new_broker_role_wins_in_root_launcher(env):
    env.settings.write_text(env.settings.read_text() + "RYNHULT_BROKER_ROLE=services\n")
    with pytest.raises(env.m.Refused):
        env.m.broker_settings("alex-agent")
    env.settings.write_text(env.settings.read_text().replace("ACC_BROKER_ROLE=agents\n", "")
                            .replace("RYNHULT_BROKER_ROLE=services", "RYNHULT_BROKER_ROLE=agents"))
    assert env.m.broker_settings("alex-agent")["ACC_BROKER_ROLE"] == "agents"


def test_read_only_rootfs_bind_targets_land_on_image_targets_or_launcher_tmpfs(env):
    """crun can't create a missing mount point in the read-only published rootfs: every destination is a target the
    image ships (slim_rootfs.MOUNT_*), or below one of the launcher's small tmpfs mounts at the account's roots."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('slim_rootfs', Path(__file__).resolve().parents[1] / 'deploy/containers/slim_rootfs.py')
    slim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(slim)
    value = grant(env)
    argv = env.m.podman_argv(value, env.m.load_policy("default"), env.m.pwd.getpwnam("alex-agent"))
    assert "--read-only" in argv and "--rootfs" in argv and "--read-only-tmpfs=false" in argv
    tmpfs = [arg.split("=", 1)[1].split(":", 1)[0] for arg in argv if arg.startswith("--tmpfs=")]
    shipped = {"/" + name for name in (*slim.MOUNT_DIRECTORIES, *slim.MOUNT_FILES)}
    for arg in argv:
        if arg.startswith("--mount="):
            destination = dict(part.split("=", 1) for part in arg.removeprefix("--mount=").split(",") if "=" in part)["dst"]
            assert destination in shipped or any(destination.startswith(root + "/") for root in tmpfs), destination
    settings = env.m.broker_settings("alex-agent")
    assert settings.get("ACC_RUN_DIR", "/srv/acc/run").rstrip("/") in tmpfs
    assert {"/tmp", "/run/credentials"} <= set(tmpfs)
    with pytest.raises(env.m.Refused):
        env.m.podman_argv({**value, "checkout": "/elsewhere/.worktrees/demo-1a2b3c4d"}, env.m.load_policy("default"),
                          env.m.pwd.getpwnam("alex-agent"))


def _slim():
    import importlib.util
    spec = importlib.util.spec_from_file_location('slim_rootfs', Path(__file__).resolve().parents[1] / 'deploy/containers/slim_rootfs.py')
    slim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(slim)
    return slim


def test_tmpfs_mounts_only_at_the_images_empty_folders_never_over_image_content(env, monkeypatch):
    """Review of session-image-mountpoints: a root that is (or contains) image content must not get an empty tmpfs
    over it. Over /usr/local that would hide /usr/local/bin/acc-guard, and the agent's guard hook would fail open."""
    policy, entry = env.m.load_policy("default"), env.m.pwd.getpwnam("alex-agent")
    monkeypatch.setattr(env.m, "TMPFS_TARGETS", ("/srv/acc/run", "/srv/acc/workspaces"))  # the production default
    run_dir = "/srv/acc/run/s1a2b3c4d5"
    for root in ("/usr/local", "/usr", "/srv/acc", "/srv", "/etc", "/run", "/home/alex", "/opt"):
        env.settings.write_text(f"ACC_BROKER_ROLE=agents\nACC_ALLOWED_ROOTS={root}\nACC_RUN_DIR=/srv/acc/run\n")
        value = {**grant(env), "checkout": f"{root}/.worktrees/demo-1a2b3c4d", "run_dir": run_dir}
        with pytest.raises(env.m.Refused, match="folder the session image provides"):
            env.m.podman_argv(value, policy, entry)
    env.settings.write_text("ACC_BROKER_ROLE=agents\nACC_ALLOWED_ROOTS=/srv/acc\nACC_RUN_DIR=/srv/acc/run\n")
    with pytest.raises(env.m.Refused):  # a run root that contains image content is refused the same way
        env.settings.write_text("ACC_BROKER_ROLE=agents\nACC_ALLOWED_ROOTS=/srv/acc/workspaces\nACC_RUN_DIR=/srv\n")
        env.m.podman_argv({**grant(env), "checkout": "/srv/acc/workspaces/.worktrees/demo-1a2b3c4d",
                           "run_dir": "/srv/s1a2b3c4d5"}, policy, entry)
    # A nested standard root: the tmpfs goes at the image's folder above it, not at the root.
    env.settings.write_text("ACC_BROKER_ROLE=agents\nACC_ALLOWED_ROOTS=/srv/acc/workspaces/team\nACC_RUN_DIR=/srv/acc/run\n")
    argv = env.m.podman_argv({**grant(env), "checkout": "/srv/acc/workspaces/team/.worktrees/demo-1a2b3c4d",
                              "run_dir": run_dir}, policy, entry)
    tmpfs = [a.split("=", 1)[1] for a in argv if a.startswith("--tmpfs=/srv")]
    assert tmpfs == ["/srv/acc/run:rw,nosuid,nodev,noexec,size=64k,mode=0755",
                     "/srv/acc/workspaces:rw,nosuid,nodev,noexec,size=64k,mode=0755"]


def test_default_tmpfs_targets_are_the_images_empty_mount_folders():
    source = (Path(__file__).resolve().parents[1] / "deploy/containers/acc-containers").read_text()
    default = source.split('environment.get("ACC_CONTAINERS_TMPFS_TARGETS", "', 1)[1].split('"', 1)[0].split(":")
    assert default == ["/srv/acc/run", "/srv/acc/workspaces"]
    slim = _slim()
    assert all(target.lstrip("/") in slim.MOUNT_DIRECTORIES for target in default)
    for needed in ("/usr/local/bin", "/srv/acc/bin", "/run/acc-container", "/etc", "/tmp"):
        assert not any(needed == t or needed.startswith(t + "/") for t in default), needed  # nothing they'd hide


def test_every_podman_path_argument_fits_podmans_limits(env):
    """Owner containers run attempt 9: "Error: the specified runroot is longer than 50 characters" (exit 125) with
    the runroot under the staging tree. The longest session id and generation still fit."""
    policy, entry = env.m.load_policy("default"), env.m.pwd.getpwnam("alex-agent")
    value = grant(env)
    longest = {**value, "session_id": "s" * 32, "generation": 1 << 31}
    assert len(env.m.staging_dir(longest)) > env.m.PODMAN_RUNROOT_LIMIT  # why the runtime has its own short path
    runtimes = set()
    for candidate in (value, longest, {**value, "generation": 2}, {**value, "session_id": "s9a8b7c6d5"}):
        runtime = env.m.runtime_dir(candidate)
        argv = env.m.podman_argv(candidate, policy, entry)
        paths = {flag: arg.split("=", 1)[1] for arg in argv for flag in ("--root", "--runroot", "--tmpdir")
                 if arg.startswith(flag + "=")}
        assert set(paths) == {"--root", "--runroot", "--tmpdir"}
        for flag, path in paths.items():
            assert path.startswith(runtime + "/") and len(path) <= env.m.PODMAN_RUNROOT_LIMIT, (flag, path)
        assert len(runtime) <= env.m.PODMAN_RUNROOT_LIMIT and not runtime.startswith(env.m.staging_dir(candidate))
        runtimes.add(runtime)
    assert len(runtimes) == 4  # one per session and generation, never shared
    # The service gets that runtime as its own fresh account-owned tmpfs and as XDG_RUNTIME_DIR.
    held = [os.open("/dev/null", os.O_RDONLY) for _ in range(5)]
    try:
        command = env.m.systemd_run_argv(longest, policy, entry, held=held)
    finally:
        for fd in held:
            os.close(fd)
    runtime = env.m.runtime_dir(longest)
    assert f"--setenv=XDG_RUNTIME_DIR={runtime}" in command
    assert (f"--property=TemporaryFileSystem={runtime}:rw,mode=0700,uid={entry.pw_uid},gid={entry.pw_gid}"
            in command)
    assert command.index(f"--property=TemporaryFileSystem={env.m.staging_dir(longest)}:ro") < command.index("--")



@pytest.mark.parametrize('folder', ['STAGING_PARENT', 'RUNTIME_PARENT'])
@pytest.mark.parametrize('change', ['missing', 'safe', 'writable', 'symlink', 'file', 'parent-writable'])
def test_mount_parents_must_be_missing_or_protected(env, monkeypatch, folder, change):
    """Review R-1: PID 1 mounts the staging and runtime tmpfs below these folders and follows symlinks there. Only a
    missing folder (systemd creates it as root) or a root-owned real folder nobody else can write is used."""
    base = env.tmp / 'host-run'
    base.mkdir(mode=0o755)
    for name, leaf in (('STAGING_PARENT', 'acc-container-inputs'), ('RUNTIME_PARENT', 'acc-ct')):
        monkeypatch.setattr(env.m, name, base / leaf)
    target = getattr(env.m, folder)
    if change != 'missing':
        target.mkdir(mode=0o755)
    if change == 'writable':
        target.chmod(0o777)
    elif change == 'symlink':
        target.rmdir()
        (env.home / 'planted').mkdir()
        target.symlink_to(env.home / 'planted', target_is_directory=True)
    elif change == 'file':
        target.rmdir()
        target.write_text('')
    elif change == 'parent-writable':
        base.chmod(0o777)
    grant(env)
    answer = ask(env, request())
    if change in ('missing', 'safe'):
        assert answer['ok'] and len(runs(env)) == 1
        assert env.m.staging_dir(grant(env)).startswith(f"{base}/acc-container-inputs/")
        assert env.m.runtime_dir(grant(env)).startswith(f"{base}/acc-ct/")
    else:
        assert answer['code'] == 'unsafe_runtime_config' and not runs(env)



# ---- what a unit leaves in its runtime dir: rootless Podman's pause process (qualification attempt 10) --------------

def fake_process(env, pid, uid, mounts=(), comm="catatonit"):
    folder = env.m.PROC / str(pid)
    folder.mkdir()
    (folder / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (folder / "comm").write_text(comm + "\n")
    (folder / "mountinfo").write_text("".join(f"{40 + i} 35 0:{60 + i} / {point} rw,nosuid,nodev - {kind} tmpfs rw\n"
                                              for i, (point, kind) in enumerate(mounts)))
    return str(pid)


@pytest.fixture
def ended(env, monkeypatch):
    """end_process stand-in: a 'killed' fake process disappears unless the test marks it unkillable."""
    calls, unkillable = [], set()

    def end(pid, uid, runtime):
        calls.append((pid, uid, runtime))
        if pid not in unkillable:
            shutil.rmtree(env.m.PROC / pid)
    monkeypatch.setattr(env.m, "end_process", end)
    return calls, unkillable


@pytest.mark.parametrize("path", ["stop", "failed-start"])
def test_cleanup_ends_the_escaped_pause_process_then_removes_the_runtime_dir(env, ended, monkeypatch, path):
    """Attempt 10: catatonit moved to the account's podman-pause-*.scope, outside the unit, holding the runtime tmpfs.
    The unit stopping proved nothing; cleanup must end it (as podman system migrate would) and only then remove the
    empty folder."""
    calls, _ = ended
    runtime_root = env.tmp / "run-acc-ct"
    runtime_root.mkdir()
    monkeypatch.setattr(env.m, "RUNTIME_PARENT", runtime_root)
    runtime = env.m.runtime_dir({"session_id": "s1a2b3c4d5", "generation": 1})
    Path(runtime).mkdir()  # PID 1's mount point: empty on the host
    pause = fake_process(env, 2951658, AGENT_UID, [(runtime, "tmpfs")])
    fake_process(env, 3001, AGENT_UID, [("/run/user/1004", "tmpfs")], comm="bash")       # the account's own, unrelated
    fake_process(env, 3002, AGENT_UID + 1, [(runtime, "tmpfs")])                          # another account: never touched
    fake_process(env, 3003, AGENT_UID, [(runtime, "ext4")])                               # not the runtime tmpfs
    grant(env)
    if path == "failed-start":
        (env.fakes / "run_rc").write_text("1")
        assert ask(env, request())["code"] == "start_failed"
        assert record(env)["state"] == "failed"
    else:
        assert ask(env, request())["ok"]
        assert ask(env, request(operation="stop", request_id="req-00000002"))["code"] == "stopped"
    assert calls == [(pause, AGENT_UID, runtime)]
    assert not Path(runtime).exists()
    assert {p.name for p in env.m.PROC.iterdir()} == {"3001", "3002", "3003"}


def test_a_surviving_holder_fails_cleanup_and_is_named(env, ended, monkeypatch):
    calls, unkillable = ended
    runtime_root = env.tmp / "run-acc-ct"
    runtime_root.mkdir()
    monkeypatch.setattr(env.m, "RUNTIME_PARENT", runtime_root)
    runtime = env.m.runtime_dir({"session_id": "s1a2b3c4d5", "generation": 1})
    Path(runtime).mkdir()
    unkillable.add(fake_process(env, 2951658, AGENT_UID, [(runtime, "tmpfs")]))
    grant(env)
    assert ask(env, request())["ok"]
    answer = ask(env, request(operation="stop", request_id="req-00000002"))
    assert answer["code"] == "cleanup_failed" and "still running: PID 2951658 (catatonit)" in answer["detail"]
    assert record(env)["state"] == "cleanup_failed" and record(env)["leftover"] == "PID 2951658 (catatonit)"
    assert [e["detail"] for e in audit(env) if e["action"] == "container.quarantined"] == [
        "processes of the account still hold the session's runtime folder after the unit stopped: PID 2951658 (catatonit)"]
    assert Path(runtime).is_dir()  # kept while something still holds it
    unkillable.clear()  # the reconciler retries; once it has gone, the session is clean
    env.m.reconcile()
    assert record(env)["state"] == "stopped" and not Path(runtime).exists()


def test_holders_of_a_runtime_dir_another_active_container_uses_are_never_ended(env, ended, monkeypatch):
    """Runtime dirs are per session and generation; if two ever shared one (a digest collision), the other active
    container's processes would hold it legitimately: cleanup must leave them alone."""
    calls, _ = ended
    grant(env)
    assert ask(env, request())["ok"]
    mine = record(env)
    other = {**mine, "session_id": "s9a8b7c6d5", "state": "running", "unit": "acc-ct-s9a8b7c6d5-g1.service"}
    env.m.write_record(other)
    shared = env.m.runtime_dir(mine)
    monkeypatch.setattr(env.m, "runtime_dir", lambda grant: shared)
    fake_process(env, 4001, AGENT_UID, [(shared, "tmpfs")])
    assert env.m.end_runtime_holders({**mine, "state": "stopped"}, env.m.pwd.getpwnam("alex-agent")) == []
    assert calls == []
    env.m.write_record({**other, "state": "stopped"})  # once nothing else uses it, the holder is ended
    assert env.m.end_runtime_holders({**mine, "state": "stopped"}, env.m.pwd.getpwnam("alex-agent")) == []
    assert calls == [("4001", AGENT_UID, shared)]


def test_holders_are_only_looked_for_once_the_unit_has_stopped(env, ended):
    calls, _ = ended
    (env.fakes / "stuck").write_text("")
    runtime = env.m.runtime_dir({"session_id": "s1a2b3c4d5", "generation": 1})
    fake_process(env, 2951658, AGENT_UID, [(runtime, "tmpfs")])
    grant(env)
    assert ask(env, request())["ok"]
    assert ask(env, request(operation="stop", request_id="req-00000002"))["code"] == "cleanup_failed"
    assert calls == []  # a running unit's pause process is the session's own


def test_reconcile_sweeps_empty_runtime_dirs_no_active_record_uses(env, monkeypatch):
    runtime_root = env.tmp / "run-acc-ct"
    runtime_root.mkdir()
    monkeypatch.setattr(env.m, "RUNTIME_PARENT", runtime_root)
    grant(env)
    assert ask(env, request())["ok"]
    active = Path(env.m.runtime_dir({"session_id": "s1a2b3c4d5", "generation": 1}))
    active.mkdir()
    old = [runtime_root / "7a645609ae19ab5b", runtime_root / "0123456789abcdef"]
    for folder in old:
        folder.mkdir()
    (old[1] / "left").write_text("")          # not empty: stays for an owner
    (runtime_root / "not-a-runtime").mkdir()  # not ours
    env.m.reconcile()
    assert sorted(p.name for p in runtime_root.iterdir()) == sorted([active.name, "0123456789abcdef", "not-a-runtime"])


def test_a_survivors_chosen_name_never_reaches_the_audit_or_the_refusal(env, ended, monkeypatch):
    """Review 2026-10-25 (L-6): `comm` is the account's to set (prctl PR_SET_NAME); only a fixed label is shown."""
    calls, unkillable = ended
    runtime_root = env.tmp / "run-acc-ct"
    runtime_root.mkdir()
    monkeypatch.setattr(env.m, "RUNTIME_PARENT", runtime_root)
    runtime = env.m.runtime_dir({"session_id": "s1a2b3c4d5", "generation": 1})
    Path(runtime).mkdir()
    unkillable.add(fake_process(env, 4242, AGENT_UID, [(runtime, "tmpfs")], comm='ok"; owner: run'))
    grant(env)
    assert ask(env, request())["ok"]
    answer = ask(env, request(operation="stop", request_id="req-00000002"))
    assert "still running: PID 4242 (another process of the account)" in answer["detail"]
    texts = [answer["detail"], record(env)["leftover"], *(json.dumps(e) for e in audit(env))]
    assert not any("owner: run" in text for text in texts)


def test_the_sweep_keeps_runtime_folders_of_units_without_a_record(env, monkeypatch):
    """Review 2026-10-25 (L-2): reconcile leaves a unit with no record for an owner, so its runtime folder stays too."""
    runtime_root = env.tmp / "run-acc-ct"
    runtime_root.mkdir()
    monkeypatch.setattr(env.m, "RUNTIME_PARENT", runtime_root)
    orphan = Path(env.m.runtime_dir({"session_id": "s9f8e7d6c5", "generation": 3}))
    unused = runtime_root / "0123456789abcdef"
    orphan.mkdir()
    unused.mkdir()
    (env.fakes / "units").write_text("acc-ct-s9f8e7d6c5-g3.service loaded active running acc container\n")
    env.m.reconcile()
    assert orphan.is_dir() and not unused.exists()
    assert any(e["action"] == "container.unknown_unit" and e["unit"] == "acc-ct-s9f8e7d6c5-g3.service" for e in audit(env))


def test_without_a_unit_list_nothing_is_swept(env, monkeypatch):
    runtime_root = env.tmp / "run-acc-ct"
    runtime_root.mkdir()
    monkeypatch.setattr(env.m, "RUNTIME_PARENT", runtime_root)
    (runtime_root / "0123456789abcdef").mkdir()
    real = env.m.subprocess.run

    def failing(argv, *args, **kwargs):
        if "list-units" in argv:
            return env.m.subprocess.CompletedProcess(argv, 1, "", "Failed to connect to bus")
        return real(argv, *args, **kwargs)
    monkeypatch.setattr(env.m.subprocess, "run", failing)
    env.m.reconcile()
    assert (runtime_root / "0123456789abcdef").is_dir()


def test_end_process_signals_only_a_rechecked_holder_through_a_pidfd(env, monkeypatch):
    sent = []
    monkeypatch.setattr(env.m.os, "pidfd_open", lambda pid: 99, raising=False)
    monkeypatch.setattr(env.m.os, "close", lambda fd: None, raising=False)
    monkeypatch.setattr(env.m.signal, "pidfd_send_signal", lambda fd, sig: sent.append((fd, sig)))
    monkeypatch.setattr(env.m.select, "select", lambda *args: ([], [], []))
    runtime = env.m.runtime_dir({"session_id": "s1a2b3c4d5", "generation": 1})
    fake_process(env, 5001, AGENT_UID, [(runtime, "tmpfs")])
    fake_process(env, 5002, AGENT_UID, [("/elsewhere", "tmpfs")])  # its PID was reused by an unrelated process
    env.m.end_process("5001", AGENT_UID, runtime)
    env.m.end_process("5002", AGENT_UID, runtime)
    env.m.end_process("5001", AGENT_UID + 1, runtime)
    assert sent == [(99, env.m.signal.SIGKILL)]


def test_the_unit_cannot_reach_the_accounts_user_bus(env):
    """Qualification attempt 10: rootless Podman moves its new pause process into a podman-pause-*.scope through the
    account's user manager (utils.MovePauseProcessToScope -> cgroups.UserConnection ->
    godbus.SessionBusPrivateNoAutoStartup), whatever --cgroup-manager says. godbus finds the bus at
    /run/user/<uid>/bus from the uid, not from XDG_RUNTIME_DIR, so the private runtime dir didn't hide it. The unit gets
    an empty read-only /run/user and no bus address, so the move fails and catatonit stays inside the unit and its
    limits."""
    policy, entry = env.m.load_policy("default"), env.m.pwd.getpwnam("alex-agent")
    value = grant(env)
    held = [os.open("/dev/null", os.O_RDONLY) for _ in range(5)]
    try:
        command = env.m.systemd_run_argv(value, policy, entry, held=held)
    finally:
        for fd in held:
            os.close(fd)
    unit_part = command[:command.index("--")]
    assert "--property=TemporaryFileSystem=/run/user:ro" in unit_part
    assert not any(arg.startswith("--setenv=DBUS_") for arg in unit_part)
    runtime = env.m.runtime_dir(value)
    assert f"--setenv=XDG_RUNTIME_DIR={runtime}" in unit_part and not runtime.startswith("/run/user")
    assert "--cgroup-manager=cgroupfs" in command and "--cgroups=split" in command  # conmon and the container stay too


# ---- the terminal attach folder (docs/decisions/drafts/container-attach-path.md, option A) ---------------------------

def test_launch_makes_the_accounts_private_attach_folder_and_stop_removes_it(env):
    grant(env)
    root = env.tmp / "attach"
    out = ask(env, request())
    assert out["ok"], out
    folder = Path(env.m.attach_dir({"session_id": "s1a2b3c4d5", "generation": 1}))
    assert folder.parent == root and len(folder.name) == 16
    info = os.lstat(folder)
    assert stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == AGENT_UID
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert out["attach"] == str(folder / "attach.sock")
    # The guard gets the socket as its own argument, before the fixed Podman command; the container never mounts it.
    command = shlex.split(runs(env)[0])
    guard = command.index("--", command.index(str(env.root / "acc-container-exec")))
    assert command[guard - 1] == str(folder / "attach.sock")
    assert not any(str(root) in arg for arg in command[guard:])
    assert len(str(folder / "attach.sock").replace(str(root), "/run/acc-ct-attach").encode()) < 108  # sun_path
    # The broker finds the same socket on its own (it never takes a path from anything the container can write).
    from acc.broker import containers as broker_containers
    assert broker_containers.attach_socket(root, "s1a2b3c4d5", 1) == folder / "attach.sock"
    (folder / "attach.sock").write_text("the relay's socket")
    assert ask(env, request(operation="stop", request_id="req-00000002"))["ok"]
    assert not folder.exists()


def test_attach_folder_cleanup_never_follows_what_the_account_left(env):
    grant(env)
    assert ask(env, request())["ok"]
    folder = Path(env.m.attach_dir({"session_id": "s1a2b3c4d5", "generation": 1}))
    outside = env.tmp / "outside"
    outside.mkdir()
    (outside / "keep").write_text("not the launcher's")
    (folder / "link").symlink_to(outside)
    (folder / "nested").mkdir()
    assert ask(env, request(operation="stop", request_id="req-00000002"))["ok"]  # the session still stops
    assert (outside / "keep").read_text() == "not the launcher's"
    assert not (folder / "link").exists() and (folder / "nested").is_dir()
    assert any(a["action"] == "container.attach_leftover" for a in audit(env))


def test_a_replaced_attach_folder_name_is_removed_not_followed(env):
    grant(env)
    root = env.tmp / "attach"
    root.mkdir(mode=0o755)
    name = Path(env.m.attach_dir({"session_id": "s1a2b3c4d5", "generation": 1})).name
    target = env.tmp / "elsewhere"
    target.mkdir()
    (root / name).symlink_to(target)
    assert ask(env, request())["ok"]
    assert (root / name).is_dir() and not (root / name).is_symlink() and target.is_dir()


def test_launch_refuses_an_unprotected_attach_parent(env):
    grant(env)
    root = env.tmp / "attach"
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    out = ask(env, request())
    assert not out["ok"] and out["code"] == "unsafe_runtime_config", out
    assert runs(env) == []


def test_a_raced_attach_folder_removal_is_a_leftover_not_a_failed_cleanup(env, monkeypatch):
    """Review A-6: the account adds an entry between the listing and rmdir (ENOTEMPTY). That's a leftover folder to
    audit, never cleanup_failed (capacity held, the next generation refused)."""
    grant(env)
    assert ask(env, request())["ok"]

    def raced(*args, **kwargs):
        raise OSError(errno.ENOTEMPTY, "Directory not empty")
    monkeypatch.setattr(env.m.os, "rmdir", raced)
    assert ask(env, request(operation="stop", request_id="req-00000002"))["ok"]
    assert record(env)["state"] != "cleanup_failed"
    assert any(a["action"] == "container.attach_leftover" for a in audit(env))
    assert not any(a["action"] == "container.cleanup_failed" for a in audit(env))


# ---- Podman as the account from the launcher's own service: no user bus, nothing left behind (attempt 13) -------------

LAUNCHER_CGROUP = "/acc-launcher.slice/acc-containers@3-1-0.service"  # Slice=acc-launcher.slice, Accept=yes


@pytest.fixture
def own_cgroup(env, monkeypatch):
    """This request's cgroup in the fake /proc, and pidfd kills that make a fake process disappear (unless marked)."""
    (env.m.PROC / "self").mkdir()
    (env.m.PROC / "self" / "cgroup").write_text(f"0::{LAUNCHER_CGROUP}\n")
    killed, unkillable = [], set()

    def send(fd, sig):
        killed.append((str(fd - 100000), sig))
        if str(fd - 100000) not in unkillable:
            shutil.rmtree(env.m.PROC / str(fd - 100000))
    monkeypatch.setattr(env.m.os, "pidfd_open", lambda pid: 100000 + pid, raising=False)
    monkeypatch.setattr(env.m.os, "close", lambda fd: None if fd >= 100000 else os.close(fd), raising=False)
    monkeypatch.setattr(env.m.signal, "pidfd_send_signal", send)
    monkeypatch.setattr(env.m.select, "select", lambda *args: ([], [], []))
    user_runtime = env.tmp / "run-user"
    user_runtime.mkdir()
    monkeypatch.setattr(env.m, "USER_RUNTIME", str(user_runtime))
    return killed, unkillable, user_runtime


def in_cgroup(env, pid, cgroup):
    (env.m.PROC / str(pid) / "cgroup").write_text(f"0::{cgroup}\n")


def test_account_podman_gets_no_user_bus(env):
    """godbus takes DBUS_SESSION_BUS_ADDRESS as given (only "" and "autolaunch:" are special), and "disabled:" is no
    transport it knows, so Podman can't reach the user manager to move its pause process or start the session bus."""
    grant(env)
    assert ask(env, request())["ok"]
    inspect = next(line for line in (env.fakes / "calls").read_text().splitlines() if "image inspect" in line)
    assert " DBUS_SESSION_BUS_ADDRESS=disabled: " in inspect
    assert f" XDG_RUNTIME_DIR={env.m.USER_RUNTIME}/{AGENT_UID} " in inspect


def test_launch_ends_the_pause_process_its_podman_check_left_in_the_launchers_cgroup(env, own_cgroup):
    """Attempt 13: a catatonit of the account outlived the launcher's own Podman call. With no bus it stays in this
    request's cgroup; it is ended before the answer, and nothing else is touched."""
    killed, _, _ = own_cgroup
    pause = fake_process(env, 768520, AGENT_UID)
    in_cgroup(env, pause, LAUNCHER_CGROUP)
    elsewhere = fake_process(env, 768530, AGENT_UID)  # the account's own pause process, joined, never made here
    in_cgroup(env, elsewhere, "/user.slice/user-1004.slice/user@1004.service/user.slice/podman-pause-ab12.scope")
    other = fake_process(env, 768540, AGENT_UID + 1)  # another account (cannot happen; still never touched)
    in_cgroup(env, other, LAUNCHER_CGROUP)
    grant(env)
    assert ask(env, request())["ok"]
    assert killed == [(pause, env.m.signal.SIGKILL)]
    assert {p.name for p in env.m.PROC.iterdir()} == {"self", elsewhere, other}


def test_ending_leftovers_removes_only_a_pause_pid_naming_an_ended_process(env, own_cgroup):
    killed, unkillable, user_runtime = own_cgroup
    uid = env.runner_uid  # the pause.pid must be the account's own file
    folder = user_runtime / str(uid) / "libpod/tmp"
    folder.mkdir(parents=True)
    pause = fake_process(env, 768520, uid)
    in_cgroup(env, pause, LAUNCHER_CGROUP)
    (folder / "pause.pid").write_text("999\n")  # names some other process: stays
    assert env.m.end_own_leftovers(uid) == []
    assert (folder / "pause.pid").exists()
    pause = fake_process(env, 768521, uid)
    in_cgroup(env, pause, LAUNCHER_CGROUP)
    (folder / "pause.pid").write_text(f"{pause}\n")
    assert env.m.end_own_leftovers(uid) == []
    assert not (folder / "pause.pid").exists()
    # A survivor is returned; a linked folder on the way is never followed.
    stuck = fake_process(env, 768522, uid)
    in_cgroup(env, stuck, LAUNCHER_CGROUP)
    unkillable.add(stuck)
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "pause.pid").write_text(f"{stuck}\n")
    shutil.rmtree(user_runtime / str(uid) / "libpod")
    (user_runtime / str(uid) / "libpod").symlink_to(env.tmp)
    (env.tmp / "tmp").symlink_to(elsewhere)
    assert env.m.end_own_leftovers(uid) == [stuck]
    assert (elsewhere / "pause.pid").exists()


def test_without_its_own_cgroup_the_launcher_ends_nothing(env, monkeypatch):
    sent = []
    monkeypatch.setattr(env.m.signal, "pidfd_send_signal", lambda fd, sig: sent.append(fd))
    pause = fake_process(env, 768520, AGENT_UID)
    in_cgroup(env, pause, LAUNCHER_CGROUP)
    assert env.m.end_own_leftovers(AGENT_UID) == [] and not sent


@pytest.mark.parametrize("cgroup,ends", [
    ("/", False),  # the legacy (v1) hierarchy: every process's 0:: line reads "/"
    ("/user.slice/user-0.slice/session-4.scope", False),  # --reconcile by hand from a root shell
    ("/system.slice/cron.service", False),
    ("/acc-launcher.slice/acc-containers-reconcile.service", True),
    (LAUNCHER_CGROUP, True),
])
def test_leftovers_are_ended_only_from_the_launchers_own_units(env, own_cgroup, cgroup, ends):
    """Anywhere else "this cgroup" holds processes the launcher didn't start, possibly all of the account's."""
    killed, _, _ = own_cgroup
    (env.m.PROC / "self" / "cgroup").write_text(f"0::{cgroup}\n")
    process = fake_process(env, 768520, AGENT_UID)
    in_cgroup(env, process, cgroup)
    remaining = env.m.end_own_leftovers(AGENT_UID)
    assert killed == ([(process, env.m.signal.SIGKILL)] if ends else []) and remaining == []


def test_a_survivor_of_the_launchers_own_podman_check_is_audited_and_refuses_the_launch(env, own_cgroup):
    """Census review L-6: what end_own_leftovers can't end is never dropped. The launch is refused before its unit
    starts, and the audit names the PID with a fixed label (the process's chosen name is never copied)."""
    _, unkillable, _ = own_cgroup
    pause = fake_process(env, 768520, AGENT_UID, comm="evil\x1b[2J")
    in_cgroup(env, pause, LAUNCHER_CGROUP)
    unkillable.add(pause)
    grant(env)
    out = ask(env, request())
    assert not out["ok"] and out["code"] == "account_leftover"
    assert not runs(env)
    entry = next(e for e in audit(env) if e["action"] == "container.account_leftover")
    assert entry["account"] == "alex-agent" and "PID 768520 (another process of the account)" in entry["detail"]
    assert "evil" not in json.dumps(audit(env)) and "evil" not in json.dumps(out)


def test_a_survivor_of_the_cleanup_check_is_a_refusal_cleanup_records_for_a_retry(env, own_cgroup):
    """cleanup() turns a Refused into cleanup_failed, which reconcile retries."""
    _, unkillable, _ = own_cgroup
    stuck = fake_process(env, 768521, AGENT_UID)
    in_cgroup(env, stuck, LAUNCHER_CGROUP)
    unkillable.add(stuck)
    with pytest.raises(env.m.Refused) as refusal:
        env.m.as_account(env.m.pwd.getpwnam("alex-agent"), "container", "exists", "acc-ct-s1", timeout=10)
    assert refusal.value.code == "account_leftover"
    assert audit(env)[-1]["action"] == "container.account_leftover" and "PID 768521 (catatonit)" in audit(env)[-1]["detail"]


# ---- attempt 14: the launcher service's UMask=0077 and TasksMax ----------------------------------------------------

@pytest.fixture
def unit_umask():
    """acc-containers@.service runs with UMask=0077: every mode the launcher asks for loses its group/other bits."""
    previous = os.umask(0o077)
    yield
    os.umask(previous)


def test_the_attach_root_is_traversable_under_the_launchers_umask(env, unit_umask):
    """Attempt 14: mkdir(0755) under UMask=0077 left /run/acc-ct-attach 0700, so the guard (as the account) got EACCES
    on its own folder below it (unsafe_runtime_namespace, exit 126)."""
    grant(env)
    assert ask(env, request())["ok"]
    assert stat.S_IMODE((env.tmp / "attach").stat().st_mode) == 0o755


@pytest.mark.parametrize("mode", [0o700, 0o750, 0o711, 0o1755])
def test_a_root_owned_attach_root_with_another_mode_is_set_to_0755(env, mode):
    """What the earlier launcher left on a host (0700 until reboot) is repaired on the held folder, not by name."""
    root = env.tmp / "attach"
    root.mkdir()
    root.chmod(mode)
    grant(env)
    assert ask(env, request())["ok"]
    assert stat.S_IMODE(root.stat().st_mode) == 0o755


def test_a_linked_attach_root_is_refused_and_its_target_left_alone(env):
    target = env.tmp / "elsewhere"
    target.mkdir(mode=0o700)
    target.chmod(0o700)
    (env.tmp / "attach").symlink_to(target)
    grant(env)
    out = ask(env, request())
    assert not out["ok"] and out["code"] == "unsafe_runtime_config", out
    assert "attach" in out["detail"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o700 and not list(target.iterdir())
    assert runs(env) == []


def test_an_attach_root_owned_by_another_account_is_refused_not_repaired(env, monkeypatch):
    root = env.tmp / "attach"
    root.mkdir()
    root.chmod(0o700)
    inode = (root.stat().st_dev, root.stat().st_ino)
    fstat = env.m.os.fstat

    def owned_by_the_agent(fd):
        info = fstat(fd)
        if (info.st_dev, info.st_ino) != inode:
            return info
        return SimpleNamespace(**{name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                               | {"st_uid": env.runner_uid + 1})  # anyone but the launcher's root
    monkeypatch.setattr(env.m.os, "fstat", owned_by_the_agent)
    grant(env)
    out = ask(env, request())
    assert not out["ok"] and out["code"] == "unsafe_runtime_config", out
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert runs(env) == []


def test_audit_files_stay_readable_by_the_control_group_under_the_launchers_umask(env, unit_umask):
    """accd (group acc) copies these lines into Rynhult's audit log; a 0600 file is skipped there without a word."""
    grant(env)
    assert ask(env, request())["ok"]
    files = list((env.root / "audit").glob("*.jsonl"))
    assert files and all(stat.S_IMODE(path.stat().st_mode) == 0o640 for path in files)


def test_an_output_file_the_launcher_creates_is_readable_by_the_control_group(env, unit_umask):
    """accd reads statusline.json from the run folder as the acc group, like the broker's own 0640 events.jsonl."""
    grant(env)
    assert not (env.run_dir / "statusline.json").exists()
    assert ask(env, request())["ok"]
    assert stat.S_IMODE((env.run_dir / "statusline.json").stat().st_mode) == 0o640


def unit_setting(name, key):
    lines = (HELPER.parent / name).read_text().splitlines()
    return next(line.split("=", 1)[1] for line in lines if line.startswith(key + "="))


def test_the_launcher_unit_and_its_slice_leave_room_for_podmans_threads():
    """Attempt 14: TasksMax=32 rejected a fork while Podman ran as the account (image inspect doing a system refresh).
    Each Go process runs about GOMAXPROCS (one per CPU) threads plus a few, and rootless Podman is two of them (it
    re-executes itself in the user namespace): about 2 x (20 + 8) on acc-host's 20 CPUs, 2 x (64 + 8) on a 64-CPU host.
    The session units keep their own per-session limits; only the one-request launcher and its slice change."""
    unit = int(unit_setting("acc-containers@.service", "TasksMax"))
    assert unit >= 256
    assert int(unit_setting("acc-containers-reconcile.service", "TasksMax")) == 32  # runs no Podman


def test_the_launcher_slice_can_never_refuse_a_requests_fork_before_its_own_unit():
    """Review L-1: on kernels with hierarchical pids events (pids.events.local), a fork the slice's limit refuses is
    counted in the slice, not in the request's unit, and the slice's counter is shared by every request. Sized for
    every connection the socket admits at its unit's limit plus the reconcile run, the slice is never the limit that
    refuses: every rejection is the request's own unit's, counted in its own cgroup on every kernel."""
    unit = int(unit_setting("acc-containers@.service", "TasksMax"))
    connections = int(unit_setting("acc-containers.socket", "MaxConnections"))
    reconcile = int(unit_setting("acc-containers-reconcile.service", "TasksMax"))
    assert int(unit_setting("acc-launcher.slice", "TasksMax")) >= connections * unit + reconcile


@pytest.mark.parametrize("cgroup", ["/", "/user.slice/user-0.slice/session-4.scope", "/system.slice/cron.service"])
def test_rejected_forks_reads_nothing_outside_the_launchers_own_units(env, tmp_path, cgroup):
    """Review L-4: as end_own_leftovers, elsewhere the cgroup is shared with processes the launcher didn't start."""
    cgroups = tmp_path / "cgroupfs"
    (cgroups / cgroup.lstrip("/")).mkdir(parents=True, exist_ok=True)
    (cgroups / cgroup.lstrip("/") / "pids.events").write_text("max 3\n")
    (env.m.PROC / "self").mkdir()
    (env.m.PROC / "self" / "cgroup").write_text(f"0::{cgroup}\n")
    env.m.CGROUP_FS = cgroups
    assert env.m.rejected_forks() is None


def test_a_rejected_fork_and_a_leftover_are_named_together(env, own_cgroup, monkeypatch):
    """Review L-5: the leftover is likely the rejected fork's doing; one refusal and one audit name both."""
    _, unkillable, _ = own_cgroup
    stuck = fake_process(env, 768521, AGENT_UID)
    in_cgroup(env, stuck, LAUNCHER_CGROUP)
    unkillable.add(stuck)
    counts = iter([0, 1])
    monkeypatch.setattr(env.m, "rejected_forks", lambda: next(counts))
    with pytest.raises(env.m.Refused) as refusal:
        env.m.as_account(env.m.pwd.getpwnam("alex-agent"), "image", "inspect", "x")
    assert refusal.value.code == "launcher_task_limit"
    assert "1 fork(s) rejected" in refusal.value.detail and "outlived" in refusal.value.detail
    entry = audit(env)[-1]
    assert entry["action"] == "container.launcher_task_limit"
    assert "1 fork(s) rejected" in entry["detail"] and "PID 768521 (catatonit)" in entry["leftover"]


def test_rejected_forks_reads_this_requests_own_pids_counter(env, tmp_path):
    cgroups = tmp_path / "cgroupfs"
    (cgroups / LAUNCHER_CGROUP.lstrip("/")).mkdir(parents=True)
    (cgroups / LAUNCHER_CGROUP.lstrip("/") / "pids.events").write_text("max 3\n")
    (env.m.PROC / "self").mkdir()
    (env.m.PROC / "self" / "cgroup").write_text(f"0::{LAUNCHER_CGROUP}\n")
    env.m.CGROUP_FS = cgroups
    assert env.m.rejected_forks() == 3
    # Newer kernels: pids.events counts the subtree's limit hits, pids.events.local the forks rejected here.
    (cgroups / LAUNCHER_CGROUP.lstrip("/") / "pids.events.local").write_text("max 1\n")
    assert env.m.rejected_forks() == 1
    (env.m.PROC / "self" / "cgroup").write_text("0::/acc-launcher.slice/../../escape\n")
    assert env.m.rejected_forks() is None
    (env.m.PROC / "self" / "cgroup").unlink()
    assert env.m.rejected_forks() is None


@pytest.mark.parametrize("inspect_works", [True, False])
def test_a_podman_check_that_hit_the_task_limit_is_named_in_the_refusal(env, monkeypatch, inspect_works):
    """Whether or not Podman got through, a rejected fork means its state may be half made: the refusal says why."""
    counts = iter([0, 2])
    monkeypatch.setattr(env.m, "rejected_forks", lambda: next(counts))
    if not inspect_works:
        (env.fakes / "image").write_text("")
    grant(env)
    out = ask(env, request())
    assert not out["ok"] and out["code"] == "launcher_task_limit", out
    assert "TasksMax" in out["detail"] and "acc-containers@.service" in out["detail"]
    assert runs(env) == []
    entry = next(e for e in audit(env) if e["action"] == "container.launcher_task_limit")
    assert "2 fork" in entry["detail"]


def test_an_unreadable_pids_counter_never_refuses_a_launch(env, monkeypatch):
    monkeypatch.setattr(env.m, "rejected_forks", lambda: None)
    grant(env)
    assert ask(env, request())["ok"]


# ---- a pause.pid left naming a process Podman can't join (qualification attempt 18b, generation 2) ------------------
# Podman 4.9.3, from its source: without the alive file in its tmp dir, runtime setup refreshes and starts a new pause
# process (BecomeRootInUserNS), which publishes pause.pid with rename_noreplace. An existing pause.pid wins; Podman then
# joins the PID that file names, and when /proc/PID/ns/user can't be opened it fails with "Error: cannot re-exec process
# to join the existing user namespace", exit 125. TryJoinPauseProcess, which removes such a file, never runs on that path.

@pytest.fixture
def account_runtime(env, monkeypatch):
    """The account's /run/user/<uid>/libpod/tmp in the fake root, its entries reported as the account's own; Podman's
    rootless behaviour switched on in the fake runuser."""
    folder = Path(env.m.USER_RUNTIME) / str(AGENT_UID) / "libpod/tmp"
    folder.mkdir(parents=True)
    (env.fakes / "rootless").write_text("")
    inner_fstat, inner_stat = env.m.os.fstat, env.m.os.stat
    under = Path(env.m.USER_RUNTIME)

    def owned(info, uid):
        return SimpleNamespace(**{name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
                               | {"st_uid": uid})
    accounts, others = set(), set()

    def mine(info):
        key = (info.st_dev, info.st_ino)
        return owned(info, AGENT_UID + 1) if key in others else owned(info, AGENT_UID) if key in accounts else info
    monkeypatch.setattr(env.m.os, "fstat", lambda fd: mine(inner_fstat(fd)))
    monkeypatch.setattr(env.m.os, "stat", lambda *a, **k: mine(inner_stat(*a, **k)))

    def claim():  # everything now under the fake /run/user is the account's
        accounts.clear()
        for path in [under, *under.rglob("*")]:
            info = path.lstat()
            accounts.add((info.st_dev, info.st_ino))
    def disown(path):  # another uid's, whatever the runner's uid is
        info = path.lstat()
        others.add((info.st_dev, info.st_ino))
    return SimpleNamespace(folder=folder, claim=claim, disown=disown)


def pause_pid(runtime, text):
    (runtime.folder / "pause.pid").write_text(text)
    runtime.claim()


def test_a_second_generation_launches_after_its_pause_process_is_gone(env, own_cgroup, account_runtime):
    """Attempt 18b: generation 2's image check failed with Podman's exit 125 because pause.pid named a process that no
    longer existed. The launcher clears exactly that file (no process is signalled) and the check runs."""
    killed, _, _ = own_cgroup
    grant(env)
    assert ask(env, request())["ok"]
    assert ask(env, request(operation="stop", request_id="req-00000002"))["ok"]
    pause_pid(account_runtime, "4321\n")  # no such process, and no alive file: Podman's refresh path
    grant(env, generation=2)
    answer = ask(env, request(generation=2, request_id="req-00000003"))
    assert answer["ok"], answer
    assert not (account_runtime.folder / "pause.pid").exists()
    cleared = [e for e in audit(env) if e["action"] == "container.pause_state_cleared"]
    assert [(e["account"], e["detail"]) for e in cleared] == [
        ("alex-agent", "pause.pid named PID 4321, which has exited; removed so Podman starts a new pause process")]
    assert killed == []  # nothing of the account is ended: the census sees what it saw before


def test_the_fake_podman_fails_like_podman_without_the_fix(env, account_runtime):
    """The fake reproduces the refusal: kept honest by asking it directly with the stale file in place."""
    pause_pid(account_runtime, "4321\n")
    env.m.clear_stale_pause_pid = lambda entry: None  # the launcher without its fix
    grant(env)
    answer = ask(env, request())
    assert not answer["ok"] and answer["code"] == "untrusted_image_store"
    assert answer["detail"] == ("protected session image is unavailable: podman exited 125: cannot re-exec process to "
                                "join the existing user namespace")


def test_a_live_pause_process_of_the_account_is_joined_and_left_alone(env, account_runtime):
    pause = fake_process(env, 5150, AGENT_UID)
    pause_pid(account_runtime, f"{pause}\n")
    grant(env)
    assert ask(env, request())["ok"]
    assert (account_runtime.folder / "pause.pid").read_text() == f"{pause}\n"
    assert not [e for e in audit(env) if e["action"] == "container.pause_state_cleared"]


@pytest.mark.parametrize("make,reason", [
    (lambda env: fake_process(env, 5150, AGENT_UID + 1), "is not alex-agent's catatonit"),  # a reused PID
    (lambda env: fake_process(env, 5150, AGENT_UID, comm="sleep"), "is not alex-agent's catatonit"),
    (lambda env: zombie(env, fake_process(env, 5150, AGENT_UID)), "has exited"),
])
def test_a_pause_pid_naming_another_process_is_cleared(env, account_runtime, make, reason):
    pid = make(env)
    pause_pid(account_runtime, f"{pid}\n")
    grant(env)
    assert ask(env, request())["ok"]
    assert not (account_runtime.folder / "pause.pid").exists()
    assert (env.m.PROC / pid).exists()  # never signalled
    [cleared] = [e for e in audit(env) if e["action"] == "container.pause_state_cleared"]
    assert f"pause.pid named PID {pid}, which {reason}" in cleared["detail"]


def zombie(env, pid):
    status = env.m.PROC / pid / "status"
    status.write_text(status.read_text() + "State:\tZ (zombie)\n")
    return pid


def test_a_malformed_pause_pid_is_cleared(env, account_runtime):
    pause_pid(account_runtime, "not a pid\n")
    grant(env)
    assert ask(env, request())["ok"]
    assert not (account_runtime.folder / "pause.pid").exists()
    [cleared] = [e for e in audit(env) if e["action"] == "container.pause_state_cleared"]
    assert cleared["detail"] == "pause.pid named no process; removed so Podman starts a new pause process"


@pytest.mark.parametrize("change,detail", [
    ("link-file", "Podman's pause.pid in the account's runtime folder is not a regular file"),
    ("link-folder", "the account's Podman runtime folder (libpod/tmp) can't be opened without following a link"),
    ("other-owner", "Podman's pause.pid in the account's runtime folder is not the account's own file"),
])
def test_pause_state_that_cant_be_verified_refuses_the_launch(env, account_runtime, change, detail):
    elsewhere = env.tmp / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "pause.pid").write_text("4321\n")
    if change == "link-file":
        (account_runtime.folder / "pause.pid").symlink_to(elsewhere / "pause.pid")
        account_runtime.claim()
    elif change == "link-folder":
        shutil.rmtree(account_runtime.folder)
        account_runtime.folder.symlink_to(elsewhere)
        account_runtime.claim()
    else:
        (account_runtime.folder / "pause.pid").write_text("4321\n")
        account_runtime.claim()
        account_runtime.disown(account_runtime.folder / "pause.pid")
    grant(env)
    answer = ask(env, request())
    assert not answer["ok"] and answer["code"] == "stale_podman_state"
    assert answer["detail"] == f"{detail}; it can't be verified, so Podman isn't run (owner review required)"
    assert (elsewhere / "pause.pid").read_text() == "4321\n"  # nothing followed, nothing removed
    assert not (env.fakes / "calls").exists() or "image inspect" not in (env.fakes / "calls").read_text()


def test_a_pause_pid_that_cant_be_removed_refuses_the_launch(env, account_runtime, monkeypatch):
    pause_pid(account_runtime, "4321\n")
    real_unlink = env.m.os.unlink

    def unlink(name, *args, **kwargs):
        if name == "pause.pid":
            raise PermissionError(errno.EACCES, "denied")
        return real_unlink(name, *args, **kwargs)
    monkeypatch.setattr(env.m.os, "unlink", unlink)
    grant(env)
    answer = ask(env, request())
    assert not answer["ok"] and answer["code"] == "stale_podman_state"
    assert answer["detail"] == ("pause.pid named PID 4321, which has exited, and it couldn't be removed; it can't be "
                                "verified, so Podman isn't run (owner review required)")


def test_team_replies_are_live_readonly_binds_not_frozen_launch_inputs(env):
    team = env.run_dir / 'team'
    team.mkdir(mode=0o700)
    reply = team / ('a' * 32 + '.json')
    reply.write_text('{"old": true}')
    value = grant(env)
    entry = env.m.pwd.getpwnam('alex-agent')
    held, _ = env.m.check_mounts(value, entry, env.m.broker_settings('alex-agent'))
    try:
        env.m.freeze_inputs(value, held, entry)
        assert list((env.m.input_dir(value) / 'team').iterdir()) == []
        reply.write_text('{"new": true}')
        assert Path(f'/proc/self/fd/{held[4]}', reply.name).read_text() == '{"new": true}'
        argv = env.m.podman_argv(value, env.m.load_policy('default'), entry)
        assert f'--mount=type=bind,src={env.m.staging_dir(value)}/team,dst={team},ro' in argv
    finally:
        for fd in held:
            os.close(fd)


@pytest.mark.parametrize('unsafe', ['symlink', 'world-writable'])
def test_team_reply_mount_refuses_unsafe_directory(env, unsafe):
    team = env.run_dir / 'team'
    if unsafe == 'symlink':
        team.symlink_to(env.home, target_is_directory=True)
    else:
        team.mkdir(mode=0o777)
        team.chmod(0o777)
    value = grant(env)
    entry = env.m.pwd.getpwnam('alex-agent')
    held, _ = env.m.check_mounts(value, entry, env.m.broker_settings('alex-agent'))
    try:
        with pytest.raises((env.m.Refused, OSError)):
            env.m.freeze_inputs(value, held, entry)
    finally:
        for fd in held:
            os.close(fd)
