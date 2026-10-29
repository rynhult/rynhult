"""deploy/accounts/acc-accounts: creates agent accounts asked for from the dashboard, nothing else."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import stat
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "deploy/accounts/acc-accounts"


@pytest.fixture
def helper(tmp_path, monkeypatch):
    calls = tmp_path / "calls.txt"
    fake = tmp_path / "acc-agent-add"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {calls}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "accounts/requests").mkdir(parents=True)
    (tmp_path / "broker-env").mkdir()
    for key, value in {"ACC_ACCOUNTS_ROOT": tmp_path / "accounts", "ACC_BROKER_ENV_DIR": tmp_path / "broker-env",
                       "ACC_AGENT_ADD": fake, "ACC_WORKSPACES_PARENT": "/srv/acc"}.items():
        monkeypatch.setenv(key, str(value))
    loader = importlib.machinery.SourceFileLoader("acc_accounts", str(HELPER))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("acc_accounts", loader))
    loader.exec_module(module)
    module.calls, module.tmp = calls, tmp_path
    return module


def submit(helper, **request) -> dict:
    path = helper.ROOT / "requests" / f"{request['id']}.json"
    path.write_text(json.dumps(request))
    return helper.process(path)


def test_creates_a_new_agent_account_with_its_own_root(helper):
    result = submit(helper, id="acct-0001", account="zz-test-agent")
    assert result["ok"], result
    assert helper.calls.read_text().split() == ["zz-test-agent", "--roots", "/srv/acc/workspaces-zz-test-agent"]
    assert json.loads((helper.ROOT / "results/acct-0001.json").read_text())["ok"]
    assert not (helper.ROOT / "requests/acct-0001.json").exists()


@pytest.mark.parametrize("account, message", [
    ("root", "-agent"), ("casey", "-agent"), ("Blair-agent", "-agent"), ("x-agent; rm -rf /", "-agent"),
    ("../../etc-agent", "-agent"), ("a" * 30 + "-agent", "-agent"),
])
def test_refuses_bad_names(helper, account, message):
    result = submit(helper, id="acct-0002", account=account)
    assert not result["ok"] and message in result["detail"]
    assert not helper.calls.exists()


def test_existing_users_are_refused_unless_already_agent_accounts(helper, monkeypatch):
    monkeypatch.setattr(helper, "user_exists", lambda account: True)
    assert not submit(helper, id="acct-0003", account="someone-agent")["ok"]  # a real user that isn't ours
    (helper.tmp / "broker-env/known-agent.env").write_text("ACC_BROKER_ROLE=agents\n")
    again = submit(helper, id="acct-0004", account="known-agent")
    assert again["ok"] and "already exists" in again["detail"]
    assert not helper.calls.exists()


def test_bad_request_files(helper):
    path = helper.ROOT / "requests" / "acct-0005.json"
    path.write_text(json.dumps({"id": "acct-9999", "account": "x-agent"}))  # id doesn't match the file
    assert not helper.process(path)["ok"]
    path.write_text("[]")
    assert not helper.process(path)["ok"]
